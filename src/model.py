"""Leva os jogos de data/trusted/ para data/gold/, no modelo estrela.

Constrói o desenho em DuckDB, confere e grava em Parquet. Roda depois do
transform.py, sem nenhuma edição:

    uv run python src/model.py

O que ele faz, na ordem de Kimball e Ross (2013, cap. 20):

1. dimensoes()        dim_data, dim_jogo, dim_faixa_etaria e dim_qualidade_dado,
                      cada uma com a sua chave substituta (a dim_data usa a
                      própria data). A dim_data cobre do primeiro ao último
                      lançamento, inclusive os dias sem lançamento
2. conferir_chaves()  toda chave natural dos jogos existe na dimensão? Se
                      faltar alguma, para aqui, antes de existir a fato
3. fatos()            fato_jogos, com a chave natural trocada pela substituta.
                      Nenhuma chave da fato pode ficar nula
4. conferir_fatos()   contagem de linhas e soma de cada medida, gold contra
                      trusted
5. agregada()         fato_jogos_ano_grupo, derivada da fato_jogos e conferida
                      contra ela
6. gravar()           uma tabela por arquivo em data/gold/, e relê cada um

Qualquer conferência que não bate para o script com a mensagem, e nada é
gravado: as tabelas vivem na memória até o passo 6.

A troca de chave é por LEFT JOIN, nunca por JOIN. Com JOIN, a linha cuja chave
não está na dimensão some da fato sem erro nenhum; com LEFT JOIN, ela fica, com
a chave nula, e a conferência do passo 3 acusa.

Dependências: uv add duckdb
"""

from pathlib import Path

import duckdb

DIR_TRUSTED = Path("data/trusted")
DIR_GOLD = Path("data/gold")

TABELAS_TRUSTED = ("jogos",)
TABELAS_GOLD = (
    "dim_data",
    "dim_jogo",
    "dim_faixa_etaria",
    "dim_qualidade_dado",
    "fato_jogos",
    "fato_jogos_ano_grupo",
)

# A condição de junção de cada dimensão fica num lugar só: é a mesma na conferência
# das chaves (passo 2) e na troca de chave da fato (passo 3). Alias: j = jogos; {d} vira o alias da dimensão.
JUNCAO = {
    "dim_data": "{d}.data = j.release_date::DATE",
    "dim_jogo": "{d}.appid = j.appid",
    "dim_faixa_etaria": "{d}.faixa_etaria = j.required_age AND {d}.entidade = j.entidade_faixa_etaria",
    "dim_qualidade_dado": (
        "{d}.preco_convertido = j.preco_convertido AND {d}.preco_imputado = j.price_brl_imputada "
        "AND {d}.recomendacoes_imputadas = j.recommendations_imputada "
        "AND {d}.conquistas_imputadas = j.achievements_imputada"
    ),
}

# (rótulo, chave natural em jogos, dimensão, coluna da dimensão que nunca é nula)
CHAVES = [
    ("jogos.release_date::DATE", "j.release_date::DATE", "dim_data", "data"),
    ("jogos.appid", "j.appid", "dim_jogo", "appid"),
    ("jogos.required_age+entidade", "j.required_age || ' / ' || j.entidade_faixa_etaria",
     "dim_faixa_etaria", "faixa_etaria_sk"),
    ("jogos.indicadores de qualidade",
     "j.preco_convertido || ' ' || j.price_brl_imputada || ' ' || j.recommendations_imputada "
     "|| ' ' || j.achievements_imputada", "dim_qualidade_dado", "qualidade_sk"),
]

# (o que se confere, na trusted, no gold)
CONFERENCIA = [
    ("linhas de jogos", "SELECT COUNT(*) FROM jogos", "SELECT COUNT(*) FROM fato_jogos"),
    ("recomendações", "SELECT SUM(recommendations) FROM jogos", "SELECT SUM(recomendacoes) FROM fato_jogos"),
    ("conquistas", "SELECT SUM(achievements) FROM jogos", "SELECT SUM(conquistas) FROM fato_jogos"),
    ("dlcs", "SELECT SUM(dlc) FROM jogos", "SELECT SUM(dlcs) FROM fato_jogos"),
    ("pacotes", "SELECT SUM(packages) FROM jogos", "SELECT SUM(pacotes) FROM fato_jogos"),
    ("preço (soma, em R$)", "SELECT SUM(price_brl) FROM jogos", "SELECT SUM(preco_brl) FROM fato_jogos"),
    ("rank (soma)", "SELECT SUM(rank) FROM jogos", "SELECT SUM(rank) FROM fato_jogos"),
]


def parar(mensagem: str) -> None:
    raise SystemExit(f"{mensagem}\nnada foi gravado em {DIR_GOLD}/")


def abrir() -> duckdb.DuckDBPyConnection:
    """Banco em memória, com uma view por tabela da trusted."""
    con = duckdb.connect()
    for tabela in TABELAS_TRUSTED:
        caminho = DIR_TRUSTED / f"{tabela}.parquet"
        if not caminho.exists():
            raise SystemExit(f"{caminho} não encontrado: rode antes o src/transform.py")
        con.execute(f"CREATE VIEW {tabela} AS SELECT * FROM '{caminho}'")
    return con


def uma_linha_por_membro(con: duckdb.DuckDBPyConnection, tabela: str, chave: str) -> None:
    """A chave substituta só se atribui se cada membro tiver uma linha só."""
    linhas, distintos = con.sql(f"SELECT COUNT(*), COUNT(DISTINCT {chave}) FROM {tabela}").fetchall()[0]
    if linhas != distintos:
        parar(f"{tabela}: {linhas} linhas para {distintos} valores de {chave}")


def dimensoes(con: duckdb.DuckDBPyConnection) -> None:
    """As quatro dimensões. ROW_NUMBER() numera pela ordem da chave natural.

    A dim_data se gera do primeiro ao último dia de lançamento, e não se tira das
    datas que aparecem nos jogos: os dias sem nenhum lançamento também são linhas.
    O generate_series inclui o último dia. O range, não: com ele, o último dia
    fica de fora da dimensão.

    A dim_jogo tem uma linha por jogo, então carrega os atributos descritivos
    (nome, desenvolvedoras, gêneros...). Gêneros, categorias e idiomas são listas
    em texto: um jogo tem vários, de outra granularidade (jogo x gênero), e ficam
    aqui como atributo de texto em vez de virar linhas da fato.

    A dim_qualidade_dado é uma dimensão lixo: reúne os quatro indicadores
    booleanos que o transform.py acrescenta, só nas combinações que existem.
    """
    uma_linha_por_membro(con, "jogos", "appid")

    con.execute("""
        CREATE TABLE dim_data AS
        SELECT dia::DATE AS data, year(dia) AS ano, quarter(dia) AS trimestre,
               month(dia) AS mes, isodow(dia) - 1 AS dia_semana   -- 0 = segunda-feira
        FROM generate_series(
            (SELECT MIN(release_date)::DATE FROM jogos),
            (SELECT MAX(release_date)::DATE FROM jogos),
            INTERVAL 1 DAY) AS t(dia)
    """)
    con.execute("""
        CREATE TABLE dim_jogo AS
        SELECT ROW_NUMBER() OVER (ORDER BY appid::BIGINT) AS jogo_sk,
               appid, name AS nome,
               CASE WHEN Indie THEN 'indie' ELSE 'não indie' END AS grupo,
               is_free AS gratuito, controller_support,
               developers AS desenvolvedoras, publishers AS publicadoras,
               genres AS generos, categories AS categorias,
               supported_languages AS idiomas
        FROM jogos
    """)
    con.execute("""
        CREATE TABLE dim_faixa_etaria AS
        SELECT ROW_NUMBER() OVER (ORDER BY required_age, entidade_faixa_etaria) AS faixa_etaria_sk,
               required_age AS faixa_etaria, entidade_faixa_etaria AS entidade
        FROM (SELECT DISTINCT required_age, entidade_faixa_etaria FROM jogos)
    """)
    con.execute("""
        CREATE TABLE dim_qualidade_dado AS
        SELECT ROW_NUMBER() OVER (
                   ORDER BY preco_convertido, price_brl_imputada,
                            recommendations_imputada, achievements_imputada) AS qualidade_sk,
               preco_convertido,
               price_brl_imputada AS preco_imputado,
               recommendations_imputada AS recomendacoes_imputadas,
               achievements_imputada AS conquistas_imputadas
        FROM (SELECT DISTINCT preco_convertido, price_brl_imputada,
                              recommendations_imputada, achievements_imputada FROM jogos)
    """)


def conferir_chaves(con: duckdb.DuckDBPyConnection) -> None:
    """Antes da troca: toda chave natural dos jogos tem linha na dimensão?

    Conta linhas, e não chaves distintas: é o que se perderia da fato. Se faltar,
    mostra até cinco chaves que não estão na dimensão.
    """
    print("linhas cuja chave não está na dimensão, antes da troca:")
    faltando = 0
    for rotulo, natural, dimensao, coluna in CHAVES:
        resto = f"FROM jogos j LEFT JOIN {dimensao} d ON {JUNCAO[dimensao].format(d="d")} WHERE d.{coluna} IS NULL"
        n = con.sql(f"SELECT COUNT(*) {resto}").fetchall()[0][0]
        print(f"  {rotulo:32} -> {dimensao:18} {n:5d}")
        if n:
            exemplos = con.sql(f"SELECT DISTINCT {natural} {resto} ORDER BY 1 LIMIT 5").fetchall()
            print(f"    chave que falta em {dimensao}: {[linha[0] for linha in exemplos]}")
        faltando += n
    if faltando:
        parar(f"{faltando} linhas sem chave na dimensão: corrija a dimensão antes de construir a fato")


def fatos(con: duckdb.DuckDBPyConnection) -> None:
    """A fato, com a chave substituta no lugar da natural.

    O rank fica na fato como posição na lista (ordinal, não se soma). A data é a
    própria chave da dim_data. O appid mora na dim_jogo.
    """
    con.execute(f"""
        CREATE TABLE fato_jogos AS
        SELECT j.rank AS rank, dd.data, dj.jogo_sk, df.faixa_etaria_sk, dq.qualidade_sk,
               j.recommendations AS recomendacoes,
               j.achievements AS conquistas,
               j.dlc AS dlcs,
               j.packages AS pacotes,
               j.price_brl AS preco_brl
        FROM jogos j
        LEFT JOIN dim_data dd ON {JUNCAO["dim_data"].format(d="dd")}
        LEFT JOIN dim_jogo dj ON {JUNCAO["dim_jogo"].format(d="dj")}
        LEFT JOIN dim_faixa_etaria df ON {JUNCAO["dim_faixa_etaria"].format(d="df")}
        LEFT JOIN dim_qualidade_dado dq ON {JUNCAO["dim_qualidade_dado"].format(d="dq")}
        ORDER BY j.rank
    """)
    nulas = con.sql("""
        SELECT COUNT(*) FROM fato_jogos
        WHERE data IS NULL OR jogo_sk IS NULL OR faixa_etaria_sk IS NULL OR qualidade_sk IS NULL
    """).fetchall()[0][0]
    if nulas:
        parar(f"{nulas} linhas da fato com chave nula")


def conferir_fatos(con: duckdb.DuckDBPyConnection) -> None:
    """Contagem e soma de cada medida, gold contra trusted. Tem de bater no centavo."""
    print("\nconferência da fato contra a trusted:")
    print(f"  {'':22} {'trusted':>15} {'gold':>15}")
    diferentes = []
    for medida, na_trusted, no_gold in CONFERENCIA:
        trusted, gold = con.sql(f"SELECT ({na_trusted}), ({no_gold})").fetchall()[0]
        formato = "15d" if isinstance(trusted, int) else "15.2f"
        print(f"  {medida:22} {trusted:{formato}} {gold:{formato}}")
        if round(trusted, 2) != round(gold, 2):
            diferentes.append(medida)
    if diferentes:
        parar(f"o gold não bate com a trusted em: {', '.join(diferentes)}")


def agregada(con: duckdb.DuckDBPyConnection) -> None:
    """Uma linha por ano de lançamento e grupo (indie ou não), da fato_jogos.

    Atende a P1: qual a proporção de indies e como os preços deles se comparam.
    O preço não se soma entre jogos para virar "preço do ano": a agregada guarda a
    soma dos preços dos jogos pagos e a contagem deles, e a média sai da divisão.
    """
    con.execute("""
        CREATE TABLE fato_jogos_ano_grupo AS
        SELECT d.ano AS ano_lancamento, j.grupo,
               COUNT(*) AS jogos,
               COUNT(*) FILTER (WHERE j.gratuito) AS jogos_gratis,
               COUNT(*) FILTER (WHERE NOT j.gratuito) AS jogos_pagos,
               SUM(f.preco_brl) FILTER (WHERE NOT j.gratuito) AS soma_preco_pagos,
               SUM(f.recomendacoes)::BIGINT AS recomendacoes   -- a soma sai HUGEINT, e o Parquet a gravaria como DOUBLE
        FROM fato_jogos f
        LEFT JOIN dim_jogo j USING (jogo_sk)
        LEFT JOIN dim_data d USING (data)
        GROUP BY ALL ORDER BY ano_lancamento, grupo
    """)
    atomica = con.sql("""
        SELECT COUNT(*), COUNT(*) FILTER (WHERE j.gratuito), SUM(f.preco_brl), SUM(f.recomendacoes)
        FROM fato_jogos f LEFT JOIN dim_jogo j USING (jogo_sk)
    """).fetchall()[0]
    agregada = con.sql("""
        SELECT SUM(jogos), SUM(jogos_gratis), SUM(soma_preco_pagos), SUM(recomendacoes)
        FROM fato_jogos_ano_grupo
    """).fetchall()[0]
    if [round(v, 2) for v in atomica] != [round(v, 2) for v in agregada]:
        parar(f"a agregada não bate com a fato_jogos: {agregada} contra {atomica}")
    print(f"\nagregada confere com a fato_jogos: {agregada[0]} jogos, {agregada[1]} gratuitos")


def gravar(con: duckdb.DuckDBPyConnection) -> None:
    """Uma tabela por arquivo Parquet, e a releitura de cada um."""
    DIR_GOLD.mkdir(parents=True, exist_ok=True)
    print(f"\ngold  ->  {DIR_GOLD}/")
    for tabela in TABELAS_GOLD:
        caminho = DIR_GOLD / f"{tabela}.parquet"
        con.execute(f"COPY {tabela} TO '{caminho}' (FORMAT parquet)")
        relidas = con.sql(f"SELECT COUNT(*) FROM '{caminho}'").fetchall()[0][0]
        print(f"  {tabela:24} {relidas:5d} linhas")


def main() -> None:
    con = abrir()
    dimensoes(con)
    conferir_chaves(con)
    fatos(con)
    conferir_fatos(con)
    agregada(con)
    gravar(con)


if __name__ == "__main__":
    main()
