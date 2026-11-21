# steam-analysis

Coleta os detalhes dos jogos mais bem avaliados da Steam para a Etapa 1 de Introdução à Ciência de Dados (IFMA). A fonte da coleta é a API pública [appdetails](https://store.steampowered.com/api/appdetails) da Steam Store. Ela não exige chave nem cadastro.

## Lista de IDs

`data/raw/ids-steamdb-20261003.csv` é dado de entrada, não uma etapa do `ingest.py`. O script não abre o SteamDB.

A lista veio da página [Top Rated Steam Releases](https://steamdb.info/stats/gameratings/?min_reviews=500), aberta no navegador em 03/10/2026 e salva à mão. O recorte são os 5.000 primeiros jogos desse ranking, já filtrado para quem tem pelo menos 500 avaliações, na ordem em que a página os mostra. O arquivo tem uma coluna, `appid`.

## Comandos

Na pasta do repositório, sem nenhum passo além destes:

```bash
uv sync
uv run python src/ingest.py
uv run quarto render diagnostico.qmd
uv run python src/transform.py
uv run quarto render exploracao.qmd
uv run python src/model.py
uv run quarto render docs/relatorio-etapa2.qmd
```

O `_quarto.yml` faz todo `.qmd` rodar a partir da raiz do projeto, e é por isso que o relatório pode ficar em `docs/` e ler `data/...`. O último comando gera `docs/relatorio-etapa2.pdf`.

Não há chave para copiar. O `.env` é opcional. `TARGET_APPS` (padrão 1000, máximo 5000) define quantos IDs do início do CSV são consultados. `RATE_PER_MINUTE` (padrão 40) é o ritmo global das consultas à Steam. `CONCURRENT_REQUESTS` (padrão 4, máximo 8) manda várias ao mesmo tempo, sem passar desse ritmo.

Rodar o `ingest.py` de novo não duplica registro. O que já está em `data/raw/steam-appdetails.jsonl` é pulado. Cada linha traz a fonte (`steam-store`) e a data da coleta (`20261003`). Um 401 interrompe a coleta; um 429 espera e segue; um 5xx tenta de novo e, se persistir, falha.

## Saída

| Caminho | O que é |
|---|---|
| `data/raw/steam-appdetails.jsonl` | Resposta do appdetails, um jogo por linha. Recriado pela coleta e fora do Git. |
| `data/trusted/jogos.parquet` | Jogos que passaram no contrato, gravados por `src/transform.py`. |
| `data/quarentena/rejeitados.parquet` | Linhas fora do trusted, com o motivo, gravadas por `src/transform.py`. |
| `docs/relatorio-etapa2.pdf` | Relatório da Etapa 2, gerado pelo Quarto a partir de `docs/relatorio-etapa2.qmd`, com os números da coleta de quem renderiza. |
| `data/gold/*.parquet` | Modelo estrela, um arquivo por tabela, gravado por `src/model.py` (tabelas abaixo). Recriado a cada execução e fora do Git. |

## Gold

O `src/model.py` lê só `data/trusted/jogos.parquet`. Ele cria as tabelas em memória, confere as chaves e as somas contra a trusted e só então grava. Se alguma conferência não bater, para com a mensagem e não grava nada em `data/gold/`.

| Tabela | O que é uma linha |
|---|---|
| `fato_jogos` | um jogo da lista coletada, com o rank, as recomendações, as conquistas, os DLCs, os pacotes e o preço |
| `fato_jogos_ano_grupo` | um ano de lançamento e um grupo (indie ou não indie), agregada de `fato_jogos` para a P1 |
| `dim_data` | um dia, do primeiro ao último lançamento, inclusive os dias sem lançamento |
| `dim_jogo` | um jogo, com nome, grupo, gratuidade, suporte a controle, desenvolvedoras, publicadoras, gêneros, categorias e idiomas |
| `dim_faixa_etaria` | uma combinação de faixa etária e entidade classificadora |
| `dim_qualidade_dado` | uma combinação dos indicadores de preço convertido ou imputado e de recomendações ou conquistas imputadas |
