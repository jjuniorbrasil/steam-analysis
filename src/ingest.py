from __future__ import annotations

import csv
import json
import os
import re
import time
from pathlib import Path

import httpx
from loguru import logger


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_dotenv()

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
FONTE = "steam-store"
RAW_DIR = Path("data/raw")
IDS_GLOB = "ids-steamdb-*.csv"
IDS_DATE_RE = re.compile(r"ids-steamdb-(\d{8})\.csv$")

# A Steam responde 429 bem abaixo de 200/min; 40/min é o ritmo que aguenta.
DEFAULT_RATE_PER_MINUTE = 40
DEFAULT_TARGET_APPS = 1000
MAX_TARGET_APPS = 5000
SERVER_RETRIES = 5


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"{name} inválido ({raw!r}), usando {default}")
        return default


def resolve_rate_per_minute() -> int:
    return max(1, _env_int("RATE_PER_MINUTE", DEFAULT_RATE_PER_MINUTE))


def resolve_target_apps() -> int:
    return max(1, min(_env_int("TARGET_APPS", DEFAULT_TARGET_APPS), MAX_TARGET_APPS))


def select_app_ids(app_ids: list[str], target: int) -> list[str]:
    if target > len(app_ids):
        logger.warning(
            f"TARGET_APPS={target} passa da lista ({len(app_ids)} ids); usando {len(app_ids)}"
        )
        return app_ids
    return app_ids[:target]


def resolve_ids_path() -> Path:
    matches = sorted(RAW_DIR.glob(IDS_GLOB), key=lambda path: path.name)
    if not matches:
        raise FileNotFoundError(
            "Nenhum data/raw/ids-steamdb-AAAAMMDD.csv. "
            "Essa lista é a entrada da coleta, não uma etapa do script."
        )
    path = matches[-1]
    if not IDS_DATE_RE.search(path.name):
        raise FileNotFoundError(
            f"{path.name} não traz a data da coleta no nome (ids-steamdb-AAAAMMDD.csv)."
        )
    if len(matches) > 1:
        logger.info(f"Várias listas de IDs; usando a mais recente: {path.name}")
    return path


def collection_date(ids_path: Path) -> str:
    match = IDS_DATE_RE.search(ids_path.name)
    if match is None:
        raise FileNotFoundError(f"Data ausente no nome de {ids_path.name}")
    return match.group(1)


def load_app_ids(path: Path) -> list[str]:
    seen: set[str] = set()
    app_ids: list[str] = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "appid" not in reader.fieldnames:
            raise ValueError(f"{path} precisa ter a coluna appid")
        for row in reader:
            app_id = (row.get("appid") or "").strip()
            if not app_id or app_id in seen:
                continue
            if not app_id.isdigit():
                raise ValueError(f"appid inválido em {path}: {app_id!r}")
            seen.add(app_id)
            app_ids.append(app_id)
    if not app_ids:
        raise ValueError(f"{path} não tem nenhum appid")
    return app_ids


def _as_record(app_id: str, entry: dict, *, coletado_em: str) -> dict:
    record = {
        "fonte": FONTE,
        "fonte_url": APPDETAILS_URL,
        "coletado_em": coletado_em,
        "appid": str(app_id),
        "success": bool(entry.get("success")),
    }
    if "data" in entry:
        record["data"] = entry["data"]
    return record


def load_details(path: Path) -> dict[str, dict]:
    details: dict[str, dict] = {}
    if not path.exists():
        return details
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON inválido em {path}:{line_no}") from exc
            if not isinstance(item, dict) or "appid" not in item:
                raise ValueError(f"Registro sem appid em {path}:{line_no}")
            details[str(item["appid"])] = item
    return details


def append_detail(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()


def fetch_app_details(
    app_ids: list[str],
    path: Path,
    *,
    coletado_em: str,
    rate_per_minute: int | None = None,
) -> int:
    if rate_per_minute is None:
        rate_per_minute = resolve_rate_per_minute()
    path.parent.mkdir(parents=True, exist_ok=True)
    details = load_details(path)
    pending = [app_id for app_id in app_ids if app_id not in details]
    logger.info(
        f"{len(app_ids)} app ids, {len(app_ids) - len(pending)} já salvos, "
        f"{len(pending)} para buscar"
    )
    if not pending:
        return sum(1 for app_id in app_ids if app_id in details)

    min_interval = 60.0 / rate_per_minute
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://store.steampowered.com/",
    }
    last_request = 0.0
    fetched = 0

    with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as client:
        for i, app_id in enumerate(pending, start=1):
            wait = min_interval - (time.monotonic() - last_request)
            if wait > 0:
                time.sleep(wait)

            payload = _get_appdetails(client, app_id)
            last_request = time.monotonic()

            entry = payload.get(app_id, payload)
            record = _as_record(
                app_id,
                entry if isinstance(entry, dict) else {},
                coletado_em=coletado_em,
            )
            details[app_id] = record
            append_detail(path, record)
            fetched += 1

            name = _app_name(record)
            status = "ok" if record.get("success") else "sem dados"
            logger.info(f"[{i}/{len(pending)}] {app_id} {name} ({status})")

    logger.info(f"{fetched} registros novos gravados em {path}")
    return sum(1 for app_id in app_ids if app_id in details)


def _get_appdetails(client: httpx.Client, app_id: str) -> dict:
    backoff = 15.0
    rate_backoff = 60.0
    attempt = 0
    while True:
        attempt += 1
        try:
            response = client.get(APPDETAILS_URL, params={"appids": app_id})
        except httpx.HTTPError as exc:
            if attempt >= SERVER_RETRIES:
                raise RuntimeError(f"Falha ao buscar appdetails de {app_id}") from exc
            logger.warning(f"{app_id} erro na requisição ({attempt}/{SERVER_RETRIES}): {exc}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120.0)
            continue

        if response.status_code == 401:
            raise RuntimeError(
                f"Credencial recusada (401) ao buscar appdetails de {app_id}. "
                "A coleta para aqui."
            )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                sleep_for = float(retry_after) if retry_after else rate_backoff
            except ValueError:
                sleep_for = rate_backoff
            sleep_for = max(sleep_for, rate_backoff)
            logger.warning(f"429 limite de taxa, aguardando {sleep_for:.0f}s")
            time.sleep(sleep_for)
            rate_backoff = min(rate_backoff * 1.5, 300.0)
            continue

        if response.status_code >= 500:
            if attempt >= SERVER_RETRIES:
                response.raise_for_status()
            logger.warning(
                f"{app_id} HTTP {response.status_code}, tentando de novo em {backoff:.0f}s"
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 120.0)
            continue

        response.raise_for_status()
        data = response.json()
        if data is None:
            if attempt >= SERVER_RETRIES:
                raise RuntimeError(f"JSON vazio para {app_id} depois de {SERVER_RETRIES} tentativas")
            logger.warning(f"{app_id} JSON vazio, tentando de novo em {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120.0)
            continue
        return data


def _app_name(entry: object) -> str:
    if isinstance(entry, dict):
        data = entry.get("data")
        if isinstance(data, dict) and data.get("name"):
            return str(data["name"])
    return "?"


def ingest() -> None:
    ids_path = resolve_ids_path()
    coletado_em = collection_date(ids_path)
    target = resolve_target_apps()
    listed = load_app_ids(ids_path)
    app_ids = select_app_ids(listed, target)
    out_path = RAW_DIR / f"steam-appdetails-{coletado_em}.jsonl"

    logger.info(
        f"Lista de entrada: {ids_path} ({len(listed)} ids no arquivo, "
        f"TARGET_APPS={target}, {len(app_ids)} para coletar, data {coletado_em})"
    )
    total = fetch_app_details(app_ids, out_path, coletado_em=coletado_em)
    logger.info(f"Coletados {total} registros em {out_path}")


if __name__ == "__main__":
    ingest()
