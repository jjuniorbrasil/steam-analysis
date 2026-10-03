from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

DEFAULT_RATE_PER_MINUTE = 40
DEFAULT_TARGET_APPS = 1000
MAX_TARGET_APPS = 5000
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 8
SERVER_RETRIES = 5

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://store.steampowered.com/",
}


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


def resolve_concurrency() -> int:
    return max(1, min(_env_int("CONCURRENT_REQUESTS", DEFAULT_CONCURRENCY), MAX_CONCURRENCY))


def resolve_target_apps() -> int:
    return max(1, min(_env_int("TARGET_APPS", DEFAULT_TARGET_APPS), MAX_TARGET_APPS))


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


def select_app_ids(app_ids: list[str], target: int) -> list[str]:
    if target > len(app_ids):
        logger.warning(
            f"TARGET_APPS={target} passa da lista ({len(app_ids)} ids); usando {len(app_ids)}"
        )
        return app_ids
    return app_ids[:target]


def load_saved_ids(path: Path) -> set[str]:
    saved: set[str] = set()
    if not path.exists():
        return saved
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
            saved.add(str(item["appid"]))
    return saved


def append_detail(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()


def _record(app_id: str, entry: dict, coletado_em: str) -> dict:
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


def _app_name(entry: dict) -> str:
    data = entry.get("data")
    if isinstance(data, dict) and data.get("name"):
        return str(data["name"])
    return "?"


def _retry_after(response: httpx.Response, fallback: float) -> float:
    raw = response.headers.get("Retry-After")
    try:
        seconds = float(raw) if raw else fallback
    except ValueError:
        seconds = fallback
    return max(seconds, fallback)


def _get_appdetails(client: httpx.Client, app_id: str) -> dict:
    backoff = 15.0
    rate_backoff = 60.0
    server_tries = 0

    while True:
        try:
            response = client.get(APPDETAILS_URL, params={"appids": app_id})
        except httpx.HTTPError as exc:
            server_tries += 1
            if server_tries >= SERVER_RETRIES:
                raise RuntimeError(f"Falha ao buscar appdetails de {app_id}") from exc
            logger.warning(f"{app_id} erro na requisição ({server_tries}/{SERVER_RETRIES}): {exc}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120.0)
            continue

        if response.status_code == 401:
            raise RuntimeError(
                f"Credencial recusada (401) ao buscar appdetails de {app_id}. "
                "A coleta para aqui."
            )

        if response.status_code == 429:
            sleep_for = _retry_after(response, rate_backoff)
            logger.warning(f"429 limite de taxa, aguardando {sleep_for:.0f}s")
            time.sleep(sleep_for)
            rate_backoff = min(rate_backoff * 1.5, 300.0)
            continue

        if response.status_code >= 500:
            server_tries += 1
            if server_tries >= SERVER_RETRIES:
                response.raise_for_status()
            logger.warning(
                f"{app_id} HTTP {response.status_code}, tentando de novo em {backoff:.0f}s"
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 120.0)
            continue

        response.raise_for_status()
        data = response.json()
        if data is not None:
            return data

        server_tries += 1
        if server_tries >= SERVER_RETRIES:
            raise RuntimeError(f"JSON vazio para {app_id} depois de {SERVER_RETRIES} tentativas")
        logger.warning(f"{app_id} JSON vazio, tentando de novo em {backoff:.0f}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 120.0)


def _batches(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def fetch_app_details(
    app_ids: list[str],
    path: Path,
    *,
    coletado_em: str,
    rate_per_minute: int | None = None,
) -> int:
    if rate_per_minute is None:
        rate_per_minute = resolve_rate_per_minute()
    concurrency = min(resolve_concurrency(), rate_per_minute)
    path.parent.mkdir(parents=True, exist_ok=True)

    saved = load_saved_ids(path)
    pending = [app_id for app_id in app_ids if app_id not in saved]
    logger.info(
        f"{len(app_ids)} app ids, {len(app_ids) - len(pending)} já salvos, "
        f"{len(pending)} para buscar, {concurrency} em paralelo, {rate_per_minute}/min"
    )
    if not pending:
        return len(app_ids)

    # Um lote sai junto. O próximo espera o bastante para a média ficar em rate_per_minute.
    batch_interval = concurrency * 60.0 / rate_per_minute
    write_lock = threading.Lock()
    done = 0
    next_batch_at = time.monotonic()

    with httpx.Client(headers=REQUEST_HEADERS, timeout=30.0, follow_redirects=True) as client:
        def fetch_one(app_id: str) -> None:
            nonlocal done
            payload = _get_appdetails(client, app_id)
            entry = payload.get(app_id, payload)
            entry = entry if isinstance(entry, dict) else {}
            record = _record(app_id, entry, coletado_em)
            with write_lock:
                append_detail(path, record)
                done += 1
                current = done
            status = "ok" if record.get("success") else "sem dados"
            logger.info(f"[{current}/{len(pending)}] {app_id} {_app_name(record)} ({status})")

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for batch in _batches(pending, concurrency):
                delay = next_batch_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                started = time.monotonic()
                futures = [pool.submit(fetch_one, app_id) for app_id in batch]
                for future in as_completed(futures):
                    future.result()
                next_batch_at = started + batch_interval

    logger.info(f"{done} registros novos gravados em {path}")
    return len(app_ids)


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
