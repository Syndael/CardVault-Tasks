import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv
from task_logger import TaskLogger, finalize_log

load_dotenv()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_API_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "CardVault-API"))

_logger: TaskLogger | None = None

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

SEP = "=" * 58

_token: str | None = None
_token_expires_at: datetime | None = None


def _login() -> bool:
    global _token, _token_expires_at
    if not API_USERNAME or not API_PASSWORD:
        return False
    try:
        body = json.dumps({
            "username": API_USERNAME,
            "password": API_PASSWORD
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{API_BASE.rstrip('/')}/auth/login",
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _token = data["token"]
        _token_expires_at = datetime.fromisoformat(data["expires_at"]).replace(tzinfo=timezone.utc)
        return True
    except Exception:
        return False


def _get_token() -> str | None:
    global _token, _token_expires_at
    now = datetime.now(timezone.utc)
    if not _token or not _token_expires_at or _token_expires_at <= now:
        _login()
    return _token


def api_request(method, path, data=None):
    clean_path = path.strip("/")
    if "?" in clean_path:
        clean_path, query_string = clean_path.split("?", 1)
        url = f"{API_BASE.rstrip('/')}/{clean_path}/?{query_string}"
    else:
        url = f"{API_BASE.rstrip('/')}/{clean_path}/"

    body = None
    headers = {"Accept": "application/json"}

    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    token = _get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=body, method=method, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            if _login():
                token = _get_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                req = urllib.request.Request(url, data=body, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else None
        return None
    except Exception:
        return None


def api_get(path, params=None):
    if params:
        path = f"{path}?{urllib.parse.urlencode(params)}"
    return api_request("GET", path)


def api_get_all(path, params=None):
    page = 1
    items = []
    merged_params = {**(params or {}), "page": page, "per_page": 100}

    while True:
        data = api_get(path, merged_params)
        if not data:
            return items
        items.extend(data.get("items", []))
        pagination = data.get("pagination", {})
        if not pagination.get("has_next"):
            return items
        page += 1
        merged_params["page"] = page


def main():
    global _logger

    if not API_BASE or not API_USERNAME or not API_PASSWORD:
        print("  Missing CARDVAULT_API_* env vars")
        sys.exit(1)

    print(f"\n  {SEP}")
    print("  Mark force_download for products without images")
    print(f"  {SEP}")
    print(f"  API: {API_BASE}")

    if not _login():
        print("  Login failed")
        sys.exit(1)
    print("  Login OK\n")

    print("  Fetching settings...")
    settings_list = api_get_all("settings")
    settings_by_key = {item["setting_key"]: item.get("setting_value") for item in settings_list}
    log_path_setting = settings_by_key.get("tasks.log.path", "./logs")
    log_dir = log_path_setting if os.path.isabs(log_path_setting) else os.path.join(_API_ROOT, log_path_setting)
    _logger = TaskLogger(log_dir, "mark_force_download")
    _logger.log(f"  Log path: {log_dir}")

    _logger.log("  Fetching products from non-manual collections...")
    all_products = api_get_all("product-catalog", {
        "is_manual": 0,
        "per_page": 200
    })
    products = [p for p in all_products if not p.get("product_is_manual")]
    _logger.log(f"  Found {len(all_products)} products ({len(all_products) - len(products)} excluded as manual)\n")

    _logger.log("  Fetching existing files...")
    all_files = api_get_all("files", {"per_page": 500})
    product_ids_with_files = set()
    for f in all_files:
        pid = f.get("product_id")
        if pid is None:
            pid_obj = f.get("product")
            pid = pid_obj.get("id") if isinstance(pid_obj, dict) else None
        if pid is not None:
            product_ids_with_files.add(pid)
    _logger.log(f"  Products with at least one file: {len(product_ids_with_files)}\n")

    updated = 0
    skipped = 0

    for i, p in enumerate(products):
        pid = p["product_id"]
        code = p.get("collection_code", "?")
        num = p.get("product_number", "?")
        name = p.get("product_name", "")
        label = f"{code}-{num} {name}"

        if pid in product_ids_with_files:
            skipped += 1
            continue

        try:
            api_request("PATCH", f"products/{pid}", {"force_download": True})
            _logger.log(f"  [{i + 1:>4}/{len(products)}] {label:<42} force_download=1")
            updated += 1
        except Exception as e:
            _logger.log(f"  [{i + 1:>4}/{len(products)}] {label:<42} error: {e}")

    _logger.log(f"\n  {SEP}")
    _logger.log(f"  Updated: {updated}")
    _logger.log(f"  Skipped (already have images): {skipped}")
    _logger.log(f"  {SEP}\n")
    finalize_log(_logger, "mark_force_download", _API_ROOT, api_request)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrupted")
        sys.exit(1)
