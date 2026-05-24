import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

SETTING_TARGETS = "sync.name.alter.lang.targets"
SETTING_SOURCES = "sync.name.alter.lang.sources"

DEFAULT_TARGETS = "JP,KR,CHT,CHS"
DEFAULT_SOURCES = "ES,EN"

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


def api_request(method, path, data=None, timeout=15):
    clean_path = path.strip("/")
    has_resource_id = "/" in clean_path
    if "?" in clean_path:
        clean_path, query_string = clean_path.split("?", 1)
        url = f"{API_BASE.rstrip('/')}/{clean_path}/?{query_string}"
    else:
        suffix = "/" if not has_resource_id else ""
        url = f"{API_BASE.rstrip('/')}/{clean_path}{suffix}"

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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            if _login():
                token = _get_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                req = urllib.request.Request(url, data=body, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else None
        print(f"  API error {e.code} on {method} {url}", file=sys.stderr)
        raise


def api_get(path):
    return api_request("GET", path)


def api_patch(path, data):
    return api_request("PATCH", path, data)


def get_all_paginated(resource, params=None):
    items = []
    page = 1
    while True:
        qs = f"{resource}?page={page}&per_page=200"
        if params:
            qs += "".join(f"&{k}={v}" for k, v in params.items())
        data = api_get(qs)
        if not data:
            break
        batch = data.get("items") or []
        if not batch:
            break
        items.extend(batch)
        pag = data.get("pagination") or {}
        if not pag.get("has_next"):
            break
        page += 1
    return items


def main():
    if not API_BASE:
        print("CARDVAULT_API_BASE not set")
        sys.exit(1)

    print("Fetching settings...")
    settings_data = api_get("settings?per_page=200")
    settings = {}
    if settings_data:
        for s in settings_data.get("items") or []:
            settings[s["setting_key"]] = s["setting_value"]

    targets_raw = settings.get(SETTING_TARGETS, DEFAULT_TARGETS)
    sources_raw = settings.get(SETTING_SOURCES, DEFAULT_SOURCES)
    target_abbr = {a.strip().upper() for a in targets_raw.split(",") if a.strip()}
    source_priority = [a.strip().upper() for a in sources_raw.split(",") if a.strip()]

    print(f"Target languages (from settings): {', '.join(sorted(target_abbr))}")
    print(f"Source languages (from settings): {', '.join(source_priority)}")

    print("Fetching languages...")
    languages = api_get("languages?per_page=200")
    if not languages:
        print("Failed to fetch languages")
        sys.exit(1)
    lang_items = languages.get("items") or []

    target_ids = []
    source_map = {}

    for lang in lang_items:
        abbr = lang.get("abbreviation", "").upper()
        if abbr in target_abbr:
            target_ids.append(lang["id"])
        for src_abbr in source_priority:
            if abbr == src_abbr and src_abbr not in source_map:
                source_map[src_abbr] = lang["id"]

    if not target_ids:
        print(f"No target languages found for: {', '.join(sorted(target_abbr))}")
        sys.exit(0)

    if not source_map:
        print(f"No source languages found for: {', '.join(source_priority)}")
        sys.exit(0)

    product_source = {}
    for src_abbr, src_id in source_map.items():
        print(f"Fetching {src_abbr} product translations...")
        trans_list = get_all_paginated("product-translations", {"language_id": src_id})
        for t in trans_list:
            pid = t.get("product").get("id")
            name = t.get("name", "").strip()
            if pid and name and pid not in product_source:
                product_source[pid] = name
        print(f"  Got {len(trans_list)} translations, lookup now has {len(product_source)} products")

    collection_source = {}
    for src_abbr, src_id in source_map.items():
        print(f"Fetching {src_abbr} collection translations...")
        trans_list = get_all_paginated("collection-translations", {"language_id": src_id})
        for t in trans_list:
            cid = t.get("collection_id")
            name = t.get("name", "").strip()
            if cid and name and cid not in collection_source:
                collection_source[cid] = name
        print(f"  Got {len(trans_list)} translations, lookup now has {len(collection_source)} collections")

    total_updated = 0

    for lang_id in target_ids:
        lang_abbr = next((l.get("abbreviation", "") for l in lang_items if l.get("id") == lang_id), str(lang_id))
        print(f"Processing product translations for language {lang_id} ({lang_abbr})...")
        target_trans = get_all_paginated("product-translations", {"language_id": lang_id, "name_alter": "__empty__"})
        for t in target_trans:
            pid = t.get("product_id") or (t.get("product") or {}).get("id")
            source_name = product_source.get(pid)
            if not source_name:
                continue
            tid = t.get("id")
            original_name = t.get("name", "")
            prod = t.get("product") or {}
            coll_code = (prod.get("collection") or {}).get("code", "")
            prod_num = prod.get("product_number", "")
            ref = f"{coll_code} #{prod_num}" if coll_code or prod_num else str(pid)
            try:
                api_patch(f"product-translations/{tid}", {"name_alter": source_name})
                total_updated += 1
                print(f"  [{lang_abbr}] product {ref}: \"{original_name}\" → \"{source_name}\"")
            except Exception as e:
                print(f"  Error updating product translation {tid}: {e}")

    for lang_id in target_ids:
        lang_abbr = next((l.get("abbreviation", "") for l in lang_items if l.get("id") == lang_id), str(lang_id))
        print(f"Processing collection translations for language {lang_id} ({lang_abbr})...")
        target_trans = get_all_paginated("collection-translations", {"language_id": lang_id, "name_alter": "__empty__"})
        for t in target_trans:
            cid = t.get("collection_id")
            source_name = collection_source.get(cid)
            if not source_name:
                continue
            tid = t.get("id")
            original_name = t.get("name", "")
            try:
                api_patch(f"collection-translations/{tid}", {"name_alter": source_name})
                total_updated += 1
                print(f"  [{lang_abbr}] collection {cid}: \"{original_name}\" → \"{source_name}\"")
            except Exception as e:
                print(f"  Error updating collection translation {tid}: {e}")

    print(f"Completed. Updated {total_updated} translations.")


if __name__ == "__main__":
    main()
