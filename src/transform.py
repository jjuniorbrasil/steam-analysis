"""Leva o appdetails de data/raw/ para data/trusted/, validando na fronteira.

A política é quarentena, não Fail-Stop. Parar o pipeline por demo, DLC ou ficha
vazia jogaria fora os jogos que passaram. Uma falha sem linha (coluna a mais,
coluna faltando) continua interrompendo: aí não há registro para isolar.

Regras de negócio, as três no nível da tabela:

preco_gratuidade_incoerente. Jogo grátis custa zero, e só jogo grátis custa zero.
A API separa is_free de price_overview; a coleta mostrou os dois desalinhados
quando o preço vinha ausente. A regra é leitura desse par, não de um manual.

preco_convertido_e_imputado. Preço imputado não foi convertido de outra moeda.
As taxas em TAXAS_PARA_BRL são suposição da equipe (valores redondos, não a
cotação de 03/10/2026). Moeda fora dessa tabela não vira real por omissão:
vai para a quarentena como moeda_sem_taxa.

faixa_etaria_zero_sem_entidade. Faixa "0" só vale se alguma entidade de
classificação a confirma. Sem entidade em ratings, "0" é ausência de dado.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import pandera.pandas as pa
from pandera.pandas import Check, Column, DataFrameSchema
from domain.taxas import TAXAS_PARA_BRL

RAW = Path("data/raw")
BRUTO = RAW / "steam-appdetails.jsonl"
TRUSTED = Path("data/trusted/jogos.parquet")
QUARENTENA = Path("data/quarentena/rejeitados.parquet")
DATA_COLETA = pd.Timestamp("2026-10-03")

# required_age da Steam vem "0" quando ninguém declarou idade; trato como ausência.
IDADES_SEM_VALOR = {"", "0"}
ENTIDADE_NAO_INFORMADA = "não informado"

CONTRATO = DataFrameSchema(
    {
        "appid": Column(str, nullable=False, unique=True),
        "name": Column(str, nullable=False),
        "type": Column(
            str,
            Check.isin(["game"], error="type_fora_dominio"),
            nullable=False,
        ),
        "required_age": Column(
            str,
            nullable=False,
        ),
        "entidade_faixa_etaria": Column(str, nullable=False),
        "is_free": Column(bool, nullable=False),
        "dlc": Column(int, Check.greater_than_or_equal_to(0, error="dlc_negativo"), nullable=False),
        "packages": Column(
            int,
            Check.greater_than_or_equal_to(0, error="packages_negativo"),
            nullable=False,
        ),
        "controller_support": Column(str, nullable=False),
        "developers": Column(str, nullable=False),
        "publishers": Column(str, nullable=False),
        "categories": Column(str, nullable=False),
        "genres": Column(str, nullable=False),
        "recommendations": Column(
            int,
            Check.greater_than_or_equal_to(0, error="recomendacoes_negativas"),
            nullable=False,
        ),
        "achievements": Column(
            int,
            Check.greater_than_or_equal_to(0, error="achievements_negativos"),
            nullable=False,
        ),
        "release_date": Column(
            "datetime64[ns]",
            Check.less_than_or_equal_to(DATA_COLETA, error="data_posterior_a_coleta"),
            nullable=False,
        ),
        "supported_languages": Column(str, nullable=False),
        "price_brl": Column(
            float,
            Check.greater_than_or_equal_to(0, error="preco_negativo"),
            nullable=False,
        ),
        "preco_convertido": Column(bool, nullable=False),
        "achievements_imputada": Column(bool, nullable=False),
        "recommendations_imputada": Column(bool, nullable=False),
        "price_brl_imputada": Column(bool, nullable=False),
        "Indie": Column(bool, nullable=False),
    },
    checks=[
        Check(
            lambda df: df["is_free"] == (df["price_brl"] == 0),
            error="preco_gratuidade_incoerente",
        ),
        Check(
            lambda df: ~(df["preco_convertido"] & df["price_brl_imputada"]),
            error="preco_convertido_e_imputado",
        ),
        Check(
            lambda df: ~(
                (df["required_age"] == "0")
                & (df["entidade_faixa_etaria"] == ENTIDADE_NAO_INFORMADA)
            ),
            error="faixa_etaria_zero_sem_entidade",
        ),
    ],
    coerce=True,
    strict=True,
)


def achar_bruto() -> Path:
    if not BRUTO.exists():
        raise FileNotFoundError("Nenhum data/raw/steam-appdetails.jsonl. Rode o ingest.py antes.")
    return BRUTO


def data_da_coleta(path: Path) -> pd.Timestamp:
    """A data fica em coletado_em, não no nome do arquivo."""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            bruto = str(item.get("coletado_em", "")) if isinstance(item, dict) else ""
            if len(bruto) == 8 and bruto.isdigit():
                return pd.Timestamp(f"{bruto[:4]}-{bruto[4:6]}-{bruto[6:8]}", tz="UTC")
            break
    raise FileNotFoundError(f"coletado_em ausente em {path.name}")


def carregar(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Lê o JSONL e separa ficha vazia. Não altera data/raw/."""
    fichas: list[dict] = []
    sem_ficha: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                continue
            appid = str(item.get("appid", ""))
            data = item.get("data")
            if item.get("success") and isinstance(data, dict):
                row = dict(data)
                row["appid"] = appid
                fichas.append(row)
            elif appid:
                sem_ficha.append(appid)

    df = pd.DataFrame(fichas)
    if not df.empty:
        antes = len(df)
        df = df.drop_duplicates(subset=["appid"]).reset_index(drop=True)
        removidas = antes - len(df)
        if removidas:
            print(f"duplicatas removidas: {removidas}")

    quarentena = pd.DataFrame({"appid": sem_ficha, "motivo": "sem_ficha"})
    return df, quarentena


def _juntar(valor: object) -> str:
    if isinstance(valor, list):
        return ", ".join(str(item) for item in valor)
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return ""
    return str(valor)


def _descricoes(valor: object) -> str:
    if not isinstance(valor, list):
        return ""
    nomes = [
        item.get("description", "")
        for item in valor
        if isinstance(item, dict) and item.get("description")
    ]
    return ", ".join(nomes)


def _contar(valor: object) -> int:
    return len(valor) if isinstance(valor, list) else 0


def _limpar_idiomas(texto: object) -> str:
    if not isinstance(texto, str):
        return ""
    sem_html = re.sub(r"<[^>]*>", "", texto)
    return re.sub(r"\s*\*+", "", sem_html).strip()



def _avaliacoes(ratings: object) -> dict[str, str]:
    """{entidade: faixa} de toda entidade que avaliou, na ordem do JSON."""
    if not isinstance(ratings, dict):
        return {}
    avaliacoes: dict[str, str] = {}
    for entidade, info in ratings.items():
        if not isinstance(info, dict) or info.get("rating") is None:
            continue
        faixa = str(info["rating"]).strip().upper()
        if faixa:
            avaliacoes[str(entidade)] = faixa
    return avaliacoes


def _faixa_etaria(required_age: str, ratings: object) -> tuple[str, str]:
    """Devolve (faixa etária, entidade que a deu).

    1. dejus avaliou: vale a dejus.
    2. required_age tem valor: a entidade é a que avaliou com esse mesmo valor.
       Se nenhuma bate e o valor não é "0", fica o valor da Steam e a entidade
       é "não informado".
    3. sem valor ("" ou "0" que nenhuma entidade confirma): a primeira entidade
       que avaliou, sem prioridade entre elas.
    Sem entidade em nenhum passo, a entidade é "não informado". Faixa "0" nesse
    caso é reprovada pelo contrato (faixa_etaria_zero_sem_entidade).
    """
    avaliacoes = _avaliacoes(ratings)
    if "dejus" in avaliacoes:
        return avaliacoes["dejus"], "dejus"

    if required_age:
        for entidade, faixa in avaliacoes.items():
            if faixa == required_age:
                return required_age, entidade
        if required_age not in IDADES_SEM_VALOR:
            return required_age, ENTIDADE_NAO_INFORMADA

    for entidade, faixa in avaliacoes.items():
        return faixa, entidade
    return required_age, ENTIDADE_NAO_INFORMADA


def _preco_brl(overview: object) -> tuple[float | None, bool, bool]:
    """Devolve (preço em BRL, foi convertido, moeda sem taxa)."""
    if not isinstance(overview, dict):
        return None, False, False
    moeda = str(overview.get("currency") or "BRL")
    bruto = overview.get("initial")
    if bruto is None:
        return None, False, False
    taxa = TAXAS_PARA_BRL.get(moeda)
    if taxa is None:
        return None, False, True
    return round((float(bruto) / 100) * taxa, 2), moeda != "BRL", False


def higienizar(df: pd.DataFrame) -> pd.DataFrame:
    """Listas viram texto, data vira datetime, preço de tabela vira real."""
    colunas = [
        "appid",
        "type",
        "name",
        "required_age",
        "is_free",
        "dlc",
        "packages",
        "controller_support",
        "developers",
        "publishers",
        "price_overview",
        "categories",
        "genres",
        "recommendations",
        "achievements",
        "release_date",
        "ratings",
        "supported_languages",
    ]
    fora = df.reindex(columns=colunas).copy()
    fora["appid"] = fora["appid"].astype(str)
    fora["name"] = fora["name"].map(lambda valor: "" if pd.isna(valor) else str(valor))
    fora["type"] = fora["type"].map(lambda valor: "" if pd.isna(valor) else str(valor))
    fora["developers"] = fora["developers"].map(_juntar)
    fora["publishers"] = fora["publishers"].map(_juntar)
    fora["genres"] = fora["genres"].map(_descricoes)
    fora["categories"] = fora["categories"].map(_descricoes)
    fora["supported_languages"] = fora["supported_languages"].map(_limpar_idiomas)
    fora["dlc"] = fora["dlc"].map(_contar)
    fora["packages"] = fora["packages"].map(_contar)
    fora["recommendations"] = fora["recommendations"].map(
        lambda valor: valor.get("total") if isinstance(valor, dict) else None
    )
    fora["achievements"] = fora["achievements"].map(
        lambda valor: valor.get("total") if isinstance(valor, dict) else None
    )
    fora["release_date"] = pd.to_datetime(
        fora["release_date"].map(lambda valor: valor.get("date") if isinstance(valor, dict) else valor),
        format="mixed",
        errors="coerce",
    )

    fora["required_age"] = fora["required_age"].map(
        lambda valor: "" if pd.isna(valor) else str(valor).strip().upper()
    )
    faixas = [_faixa_etaria(idade, ratings) for idade, ratings in zip(fora["required_age"], fora["ratings"])]
    fora["required_age"] = [faixa for faixa, _ in faixas]
    fora["entidade_faixa_etaria"] = [entidade for _, entidade in faixas]

    precos = fora["price_overview"].map(_preco_brl)
    fora["price_brl"] = precos.map(lambda item: item[0])
    fora["preco_convertido"] = precos.map(lambda item: item[1])
    fora["moeda_sem_taxa"] = precos.map(lambda item: item[2])
    gratis = fora["is_free"] == True  # noqa: E712
    fora.loc[gratis, "price_brl"] = 0.0
    fora.loc[gratis, "preco_convertido"] = False
    fora.loc[gratis, "moeda_sem_taxa"] = False

    return fora.drop(columns=["price_overview", "ratings"])


def _mediana_dentro_do_iqr(serie: pd.Series) -> float:
    base = serie.dropna()
    q1 = base.quantile(0.25)
    q3 = base.quantile(0.75)
    iqr = q3 - q1
    dentro = base.between(q1 - 1.5 * iqr, q3 + 1.5 * iqr)
    return float(base.loc[dentro].median())


def tratar(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Imputa ausência com indicador. Moeda sem taxa sai antes da imputação."""
    sem_taxa = df[df["moeda_sem_taxa"]].copy()
    resto = df[~df["moeda_sem_taxa"]].drop(columns=["moeda_sem_taxa"]).copy()

    resto["controller_support"] = resto["controller_support"].fillna("sem suporte")
    resto.loc[resto["controller_support"].astype(str).str.strip().eq(""), "controller_support"] = (
        "sem suporte"
    )
    resto["supported_languages"] = resto["supported_languages"].replace("", pd.NA).fillna(
        "Não informado"
    )
    resto["preco_convertido"] = resto["preco_convertido"].fillna(False).astype(bool)

    for coluna in ("achievements", "recommendations", "price_brl"):
        resto[f"{coluna}_imputada"] = resto[coluna].isna()
        referencia = resto.loc[resto["type"].eq("game"), coluna]
        resto[coluna] = resto[coluna].fillna(_mediana_dentro_do_iqr(referencia))

    resto["achievements"] = resto["achievements"].round().astype(int)
    resto["recommendations"] = resto["recommendations"].round().astype(int)
    resto["price_brl"] = resto["price_brl"].astype(float)
    resto["Indie"] = resto["genres"].str.lower().str.contains("indie", regex=False)
    resto["is_free"] = resto["is_free"].fillna(False).astype(bool)

    if sem_taxa.empty:
        quarentena = sem_taxa.drop(columns=["moeda_sem_taxa"], errors="ignore")
    else:
        quarentena = sem_taxa.drop(columns=["moeda_sem_taxa"]).assign(motivo="moeda_sem_taxa")
    return resto, quarentena


def validar(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Devolve (aprovado, reprovado com motivo). Não grava nada."""
    df = df.reset_index(drop=True)
    try:
        aprovado = CONTRATO.validate(df, lazy=True)
        vazio = df.iloc[0:0].assign(motivo=pd.Series(dtype=str))
        return aprovado, vazio
    except pa.errors.SchemaErrors as erro:
        falhas = erro.failure_cases
        if falhas["index"].isna().any():
            raise SystemExit(
                "contrato quebrado na estrutura, nada foi gravado:\n"
                f"{falhas[falhas['index'].isna()]}"
            )
        motivos = falhas.groupby("index")["check"].agg(
            lambda serie: ", ".join(sorted(set(serie.astype(str))))
        )
        motivos.index = motivos.index.astype(int)
        rejeitado = df.loc[motivos.index].assign(motivo=motivos.to_numpy())
        aprovado = CONTRATO.validate(df.drop(index=motivos.index))
        return aprovado, rejeitado


def main() -> None:
    bruto = achar_bruto()
    coletado_em = data_da_coleta(bruto)
    fichas, sem_ficha = carregar(bruto)
    tratado, sem_taxa = tratar(higienizar(fichas))
    aprovado, reprovado = validar(tratado)

    rejeitado = pd.concat([sem_ficha, sem_taxa, reprovado], ignore_index=True)
    if len(rejeitado):
        rejeitado["detectado_em"] = coletado_em
        rejeitado = rejeitado.sort_values(
            [coluna for coluna in ("appid", "motivo") if coluna in rejeitado.columns]
        ).reset_index(drop=True)

    aprovado = aprovado.sort_values("appid").reset_index(drop=True)
    TRUSTED.parent.mkdir(parents=True, exist_ok=True)
    aprovado.to_parquet(TRUSTED, index=False)
    if len(rejeitado):
        QUARENTENA.parent.mkdir(parents=True, exist_ok=True)
        rejeitado.to_parquet(QUARENTENA, index=False)

    print(f"trusted:    {len(aprovado):4d} linhas -> {TRUSTED}")
    print(f"quarentena: {len(rejeitado):4d} linhas -> {QUARENTENA}")
    if len(rejeitado):
        for motivo, quantidade in rejeitado["motivo"].value_counts().items():
            print(f"  {motivo:36} {quantidade}")


if __name__ == "__main__":
    main()