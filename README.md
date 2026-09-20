# steam-analysis

Coleta os jogos mais bem avaliados no [SteamDB](https://steamdb.info/stats/gameratings/?min_reviews=500) e baixa os detalhes de cada um na [API da Steam](https://store.steampowered.com/api/appdetails?appids=730).

## Setup

```bash
uv sync
cp .env.example .env
```

## Executar

```bash
uv run python main.py
```

Por padrão coleta **1000** jogos. Para pedir mais (até **5000**), edite o `.env`:

```bash
TARGET_APPS=5000
```

A execução retoma do que já estiver em `data/raw/`. Se a API responder 429, o script espera e continua.

## Saída

| Arquivo | Conteúdo |
|---|---|
| `data/raw/gameratings.html` | Tabela do SteamDB |
| `data/raw/appdetails.json` | Detalhes dos jogos |
| `data/raw/appdetails.jsonl` | Mesmos detalhes, linha a linha (retomada) |
