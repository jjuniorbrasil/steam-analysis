from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx
from cloakbrowser import launch


def log(msg: str) -> None:
    print(msg, flush=True)


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

STEAMDB_URL = "https://steamdb.info/stats/gameratings/?min_reviews=500"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

HTML_PATH = Path("data/raw/gameratings.html")
DETAILS_PATH = Path("data/raw/appdetails.json")
DETAILS_JSONL_PATH = Path("data/raw/appdetails.jsonl")

DEFAULT_TARGET_APPS = 1000
MAX_TARGET_APPS = 5000
DT_PAGE_SIZES = (25, 50, 100, 250, 500, 1000, 5000)
# Steam appdetails 429s well below 200/min; 200 per 5 minutes is the practical cap.
DEFAULT_RATE_PER_MINUTE = 40
JSON_FLUSH_EVERY = 50
LENGTH_SELECT = ".dt-length select, .dataTables_length select, select[name$='_length']"
APP_ID_RE = re.compile(r'<tr class="app" data-appid="(\d+)"')


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log(f"Invalid {name}={raw!r}, using {default}")
        return default


def resolve_target_apps() -> int:
    return max(1, min(_env_int("TARGET_APPS", DEFAULT_TARGET_APPS), MAX_TARGET_APPS))


def resolve_rate_per_minute() -> int:
    return max(1, _env_int("RATE_PER_MINUTE", DEFAULT_RATE_PER_MINUTE))


def datatables_page_size(target: int) -> str:
    for size in DT_PAGE_SIZES:
        if size >= target:
            return str(size)
    return str(MAX_TARGET_APPS)


def scrape_gameratings(path: Path = HTML_PATH, target: int | None = None) -> Path:
    if target is None:
        target = resolve_target_apps()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = extract_app_ids(path.read_text(encoding="utf-8"))
        if len(existing) >= target:
            log(f"Reusing {path} ({len(existing)} ids)")
            return path
        log(f"HTML has only {len(existing)} ids, scraping again for {target}")

    browser = launch(headless=True, humanize=True)
    try:
        page = browser.new_page()
        page.goto(STEAMDB_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_selector("table tbody tr.app[data-appid]", timeout=90_000)
        page.wait_for_selector(LENGTH_SELECT, timeout=60_000)

        page_size = datatables_page_size(target)
        page.locator(LENGTH_SELECT).first.select_option(page_size)
        log(f"Set table page length to {page_size} (target {target})")
        page.wait_for_function(
            "n => document.querySelectorAll('table tbody tr.app[data-appid]').length >= n",
            arg=target,
            timeout=180_000,
        )

        count = page.locator("table tbody tr.app[data-appid]").count()
        log(f"Table rows visible: {count}")
        path.write_text(page.content(), encoding="utf-8")
        log(f"Saved {path} ({path.stat().st_size:,} bytes)")
        log(f"Title: {page.title()}")
    finally:
        browser.close()
    return path


def extract_app_ids(html: str, limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    app_ids: list[str] = []
    for app_id in APP_ID_RE.findall(html):
        if app_id not in seen:
            seen.add(app_id)
            app_ids.append(app_id)
            if limit is not None and len(app_ids) >= limit:
                break
    return app_ids


def _as_record(app_id: str, entry: dict) -> dict:
    record = {"appid": str(app_id), "success": bool(entry.get("success"))}
    if "data" in entry:
        record["data"] = entry["data"]
    return record


def _index_payload(raw: object) -> dict[str, dict]:
    details: dict[str, dict] = {}
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        if "appid" in raw and ("success" in raw or "data" in raw):
            items = [raw]
        else:
            items = [
                _as_record(str(app_id), entry)
                for app_id, entry in raw.items()
                if isinstance(entry, dict)
            ]
    else:
        return details

    for item in items:
        if not isinstance(item, dict):
            continue
        if "appid" in item:
            rec = _as_record(str(item["appid"]), item)
        elif len(item) == 1:
            app_id, entry = next(iter(item.items()))
            rec = _as_record(str(app_id), entry if isinstance(entry, dict) else {})
        else:
            continue
        details[rec["appid"]] = rec
    return details


def load_details(path: Path = DETAILS_PATH) -> dict[str, dict]:
    details: dict[str, dict] = {}
    jsonl_path = DETAILS_JSONL_PATH
    if jsonl_path.exists():
        with jsonl_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                details.update(_index_payload(json.loads(line)))
    if path.exists():
        details.update(_index_payload(json.loads(path.read_text(encoding="utf-8"))))
    return details


def save_details_json(path: Path, details: dict[str, dict]) -> None:
    records = list(details.values())
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def append_detail(record: dict) -> None:
    DETAILS_JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DETAILS_JSONL_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def fetch_app_details(
    app_ids: list[str],
    path: Path = DETAILS_PATH,
    *,
    rate_per_minute: int | None = None,
) -> None:
    if rate_per_minute is None:
        rate_per_minute = resolve_rate_per_minute()
    path.parent.mkdir(parents=True, exist_ok=True)
    details = load_details(path)
    pending = [app_id for app_id in app_ids if app_id not in details]
    log(
        f"{len(app_ids)} app ids, {len(details)} already saved, {len(pending)} to fetch"
    )
    if not pending:
        save_details_json(path, details)
        return

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

    with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as client:
        for i, app_id in enumerate(pending, start=1):
            wait = min_interval - (time.monotonic() - last_request)
            if wait > 0:
                time.sleep(wait)

            payload = _get_appdetails(client, app_id)
            last_request = time.monotonic()

            entry = payload.get(app_id, payload)
            record = _as_record(app_id, entry if isinstance(entry, dict) else {})
            details[app_id] = record
            append_detail(record)
            if i % JSON_FLUSH_EVERY == 0 or i == len(pending):
                save_details_json(path, details)

            name = _app_name(record)
            status = "ok" if record.get("success") else "no data"
            log(f"[{i}/{len(pending)}] {app_id} {name} ({status})")

    log(f"Saved {path} ({path.stat().st_size:,} bytes, {len(details)} apps)")


def _get_appdetails(client: httpx.Client, app_id: str, retries: int = 5) -> dict:
    backoff = 15.0
    rate_backoff = 60.0
    attempt = 0
    while True:
        attempt += 1
        try:
            response = client.get(APPDETAILS_URL, params={"appids": app_id})
        except httpx.HTTPError as exc:
            if attempt >= retries:
                raise RuntimeError(f"Failed to fetch appdetails for {app_id}") from exc
            log(f"  {app_id} request error ({attempt}/{retries}): {exc}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120.0)
            continue

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            sleep_for = float(retry_after) if retry_after else rate_backoff
            sleep_for = max(sleep_for, rate_backoff)
            log(f"  429 rate limited, sleeping {sleep_for:.0f}s")
            time.sleep(sleep_for)
            rate_backoff = min(rate_backoff * 1.5, 300.0)
            continue

        if response.status_code >= 500:
            if attempt >= retries:
                response.raise_for_status()
            log(f"  {app_id} HTTP {response.status_code}, retrying in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120.0)
            continue

        response.raise_for_status()
        data = response.json()
        if data is None:
            if attempt >= retries:
                raise RuntimeError(f"Empty JSON for {app_id} after {retries} retries")
            log(f"  {app_id} empty JSON, retrying in {backoff:.0f}s")
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
    target = resolve_target_apps()
    log(f"TARGET_APPS={target} (max {MAX_TARGET_APPS})")
    html_path = scrape_gameratings(target=target)
    app_ids = extract_app_ids(html_path.read_text(encoding="utf-8"), limit=target)
    log(f"Extracted {len(app_ids)} app ids")
    fetch_app_details(app_ids)


if __name__ == "__main__":
    ingest()
