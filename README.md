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
```

Não há chave para copiar. O `.env` é opcional. `TARGET_APPS` (padrão 1000, máximo 5000) define quantos IDs do início do CSV são consultados. `RATE_PER_MINUTE` (padrão 40) controla o ritmo das consultas à Steam.

Rodar o `ingest.py` de novo não duplica registro. O que já está em `data/raw/steam-appdetails-20261003.jsonl` é pulado. Cada linha traz a fonte (`steam-store`) e a data da coleta (`20261003`). Um 401 interrompe a coleta; um 429 espera e segue; um 5xx tenta de novo e, se persistir, falha.

## Saída

| Caminho | O que é |
|---|---|
| `data/raw/steam-appdetails-20261003.jsonl` | Resposta do appdetails, um jogo por linha. Recriado pela coleta e fora do Git. |
| `data/trusted/` | Registros que passaram no contrato, gravados por `src/transform.py`. |
| `data/quarentena/` | Registros separados do conjunto trusted, com o motivo, gravados por `src/transform.py`. |
