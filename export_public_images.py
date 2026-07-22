#!/usr/bin/env python3
import json
import os
import re
import shutil
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

SETTING_KEY_PUBLIC_PATH = "export.public.images.path"
SETTING_LOG_PATH = "tasks.log.path"
TAG_CATEGORIES = ["album", "caja", "vitrina"]

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
        with urllib.request.urlopen(req, timeout=30) as resp:
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


def download_file(url):
    try:
        req = urllib.request.Request(url)
        token = _get_token()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None


def get_extension(original_name, file_path=""):
    for name in [original_name, file_path]:
        if name:
            ext = os.path.splitext(name)[1]
            if ext:
                return ext
    return ".jpg"


def resolve_url(relative_url):
    if not relative_url:
        return None
    if relative_url.startswith("http"):
        return relative_url
    base = API_BASE.rstrip("/")
    if base.endswith("/api") and relative_url.startswith("/api/"):
        base = base[:-4]
    return f"{base}{relative_url}"


def natural_pad(s, width=6):
    parts = re.split(r'(\d+)', s)
    for i, part in enumerate(parts):
        if part.isdigit():
            parts[i] = f"{int(part):0{width}d}"
    return ''.join(parts)

def safe_name(s, max_len=50):
    return re.sub(r'[^a-zA-Z0-9]', '_', s)[:max_len].strip('_')

def build_sort_prefix(coll_code, prod_number, prod_name):
    return f"{coll_code.replace('-', '_')}__{natural_pad(prod_number)}__{safe_name(prod_name)}__"

def old_card_type(item):
    pt = (item.get("product") or {}).get("product_type") or {}
    return pt.get("name") or "unknown"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def generate_data_js(public_path, sort_map=None, lang_map=None, cond_map=None, primary_set=None):
    data = {}
    root = os.path.abspath(public_path)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel == ".":
            continue
        images = []
        for f in filenames:
            if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS:
                images.append(f)
        def _sort_key(fname):
            is_primary = 0 if (primary_set and fname in primary_set) else 1
            if sort_map:
                inv_id = fname.split('-')[0].rsplit('__', 1)[-1]
                key = sort_map.get(inv_id)
                if key:
                    return (is_primary,) + key + (inv_id,)
                return (is_primary, "", "", "", "", fname)
            return (is_primary, fname)
        images.sort(key=_sort_key)
        data[rel] = {
            "directories": sorted(dirnames),
            "images": [os.path.join(rel, f).replace("\\", "/") for f in images]
        }
    if lang_map:
        data["_langMap"] = lang_map
    if cond_map:
        data["_condMap"] = cond_map
    js_path = os.path.join(root, "data.js")
    with open(js_path, "w", encoding="utf-8") as fh:
        fh.write("const DIR_DATA = ")
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write(";\n")


def main():
    global _logger

    if not API_BASE or not API_USERNAME or not API_PASSWORD:
        print("  Missing CARDVAULT_API_* env vars")
        sys.exit(1)

    print(f"\n  {SEP}")
    print("  Export inventory images to public web directory")
    print(f"  {SEP}")
    print(f"  API: {API_BASE}")

    if not _login():
        print("  Login failed")
        sys.exit(1)
    print("  Login OK\n")

    print("  Fetching settings...")
    settings = api_get_all("settings")
    settings_by_key = {s["setting_key"]: s.get("setting_value") for s in settings}
    log_path_setting = settings_by_key.get(SETTING_LOG_PATH, "./logs")
    log_dir = log_path_setting if os.path.isabs(log_path_setting) else os.path.join(_API_ROOT, log_path_setting)
    _logger = TaskLogger(log_dir, "export_public_images")
    _logger.log(f"  API: {API_BASE}")
    _logger.log(f"  Log path: {log_dir}")

    public_path = settings_by_key.get(SETTING_KEY_PUBLIC_PATH)
    if not public_path:
        _logger.log(f"  ERROR: Setting '{SETTING_KEY_PUBLIC_PATH}' not found.")
        _logger.log(f"  Create it via API: POST /api/settings/ with setting_key='{SETTING_KEY_PUBLIC_PATH}' setting_value='/path/to/CardVault-Web-Publica'")
        finalize_log(_logger, "export_public_images", _API_ROOT, api_request)
        sys.exit(1)
    _logger.log(f"  Public path: {public_path}\n")

    _logger.log("  Fetching tags...")
    all_tags = api_get_all("tags")
    matching_tags = {}
    for tag in all_tags:
        name_lower = tag["name"].lower()
        for category in TAG_CATEGORIES:
            if category in name_lower:
                matching_tags.setdefault(category, []).append(tag)
                break

    if not matching_tags:
        _logger.log("  No tags found containing 'album', 'caja' or 'vitrina'")
        finalize_log(_logger, "export_public_images", _API_ROOT, api_request)
        return

    for category, tags in matching_tags.items():
        _logger.log(f"    {category}: {', '.join(t['name'] for t in tags)}")
    _logger.log()

    seen_items = {}
    for category, tags in matching_tags.items():
        for tag in tags:
            items = api_get_all("inventory", {"tag_name": tag["name"], "all": "1", "per_page": 100})
            for item in items:
                seen_items.setdefault(item["id"], item)

    _logger.log(f"  Total unique inventory items to process: {len(seen_items)}")
    _logger.log()

    sort_map = {}
    lang_map = {}
    cond_map = {}
    for inv_id, item in seen_items.items():
        product = item.get("product") or {}
        collection = item.get("collection") or {}
        coll_code = collection.get("code") or ""
        prod_number = product.get("product_number") or ""
        prod_num_padded = natural_pad(prod_number)
        translations = product.get("translations") or []
        prod_name = (translations[0] or {}).get("name", "") if translations else ""
        sort_map[str(inv_id)] = (coll_code, prod_num_padded, prod_name)
        language = item.get("language") or {}
        lang_map[str(inv_id)] = (language.get("abbreviation") or "")[:3]
        condition = item.get("condition") or {}
        cond_map[str(inv_id)] = (condition.get("abbreviation") or "")[:5]

    _logger.log("  Cleaning old-style duplicates and empty directories...")
    root = os.path.abspath(public_path)
    for dirpath, dirnames, filenames in os.walk(root):
        for f in filenames:
            if f.startswith("__") and os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS:
                fp = os.path.join(dirpath, f)
                os.remove(fp)
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if not os.listdir(dirpath):
            os.rmdir(dirpath)

    copied = 0
    skipped = 0
    errors = 0
    primary_filenames = set()

    for inv_id, item in seen_items.items():
        product = item.get("product") or {}
        product_type = product.get("product_type") or {}
        card_type = product_type.get("name") or "unknown"

        item_tags = item.get("tags") or []
        active_categories = {}
        for tag in item_tags:
            tag_name_lower = tag["name"].lower()
            for category in TAG_CATEGORIES:
                if category in tag_name_lower:
                    active_categories[category] = tag["name"]
                    break

        if not active_categories:
            continue

        inv_files_raw = api_get(f"files/by-inventory/{inv_id}")
        inv_files = inv_files_raw if isinstance(inv_files_raw, list) else []

        collection = item.get("collection") or {}
        coll_code = collection.get("code") or ""
        prod_number = product.get("product_number") or ""
        translations = product.get("translations") or []
        prod_name = (translations[0] or {}).get("name", "") if translations else ""
        sort_prefix = build_sort_prefix(coll_code, prod_number, prod_name)

        for category, section_name in active_categories.items():
            target_dir = os.path.join(public_path, category, card_type, section_name)
            os.makedirs(target_dir, exist_ok=True)

            old_type_name = product_type.get("name") or "unknown"
            if old_type_name != card_type:
                old_dir = os.path.join(public_path, category, old_type_name, section_name)
                if os.path.isdir(old_dir):
                    _logger.log(f"    Migrating {old_dir} → {target_dir}")
                    for fname in os.listdir(old_dir):
                        shutil.move(os.path.join(old_dir, fname), os.path.join(target_dir, fname))
                    try:
                        os.rmdir(old_dir)
                    except OSError:
                        pass
                    parent = os.path.dirname(old_dir)
                    while parent != public_path and os.path.isdir(parent):
                        try:
                            os.rmdir(parent)
                        except OSError:
                            break
                        parent = os.path.dirname(parent)

            for f in inv_files:
                file_id = f["id"]
                ext = get_extension(f.get("original_name", ""), f.get("file_path", ""))
                is_primary = f.get("is_primary", False)

                file_tag = "000" if is_primary else str(file_id)
                dest_name = f"{sort_prefix}{inv_id}-{file_tag}{ext}"
                dest_path = os.path.join(target_dir, dest_name)

                old_name = f"{inv_id}-{file_id}{ext}"
                old_path = os.path.join(target_dir, old_name)

                alt_name = f"{sort_prefix}{inv_id}-{file_id}{ext}"
                alt_path = os.path.join(target_dir, alt_name)

                if os.path.exists(dest_path):
                    skipped += 1
                    if is_primary:
                        primary_filenames.add(dest_name)
                    continue
                if os.path.exists(old_path):
                    os.rename(old_path, dest_path)
                    skipped += 1
                    if is_primary:
                        primary_filenames.add(dest_name)
                    continue
                if os.path.exists(alt_path):
                    os.rename(alt_path, dest_path)
                    skipped += 1
                    if is_primary:
                        primary_filenames.add(dest_name)
                    continue

                file_url = resolve_url(f"/api/product-catalog/files/{file_id}/content")
                if not file_url:
                    errors += 1
                    continue

                data = download_file(file_url)
                if data is None:
                    _logger.log(f"    ERROR downloading inv file {file_id} for inv#{inv_id}")
                    errors += 1
                    continue

                with open(dest_path, "wb") as fh:
                    fh.write(data)
                copied += 1
                if is_primary:
                    primary_filenames.add(dest_name)
                _logger.log(f"    [{category}/{section_name}] inv#{inv_id} file#{file_id} -> {dest_path}")

        product_image_url = item.get("product_image_url")
        if product_image_url:
            m = re.search(r'/files/(\d+)/content', product_image_url)
            prod_file_id = int(m.group(1)) if m else None

            if prod_file_id:
                prod_meta = api_get(f"files/{prod_file_id}")
                prod_ext = get_extension(
                    (prod_meta or {}).get("original_name", ""),
                    (prod_meta or {}).get("file_path", "")
                )
            else:
                prod_ext = ".jpg"

            dest_name = f"{sort_prefix}{inv_id}-prod{prod_ext}"
            dest_path = os.path.join(target_dir, dest_name)

            old_name = f"{inv_id}-prod{prod_ext}"
            old_path = os.path.join(target_dir, old_name)
            if os.path.exists(dest_path):
                skipped += 1
                continue
            if os.path.exists(old_path):
                os.rename(old_path, dest_path)
                skipped += 1
                continue

            full_url = resolve_url(product_image_url)
            if full_url:
                data = download_file(full_url)
                if data is not None:
                    with open(dest_path, "wb") as fh:
                        fh.write(data)
                    copied += 1
                    _logger.log(f"    [{category}/{section_name}] inv#{inv_id} prod -> {dest_path}")
                else:
                    _logger.log(f"    ERROR downloading product image for inv#{inv_id}")
                    errors += 1

    _logger.log("  Generating data.js...")
    generate_data_js(public_path, sort_map=sort_map, lang_map=lang_map, cond_map=cond_map, primary_set=primary_filenames)
    _logger.log(f"  data.js updated ({len(primary_filenames)} primary images)\n")

    _logger.log(f"\n  {SEP}")
    _logger.log(f"  Copied: {copied}")
    _logger.log(f"  Skipped: {skipped}")
    if errors:
        _logger.log(f"  Errors: {errors}")
    _logger.log(f"  {SEP}\n")
    finalize_log(_logger, "export_public_images", _API_ROOT, api_request)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrupted")
        sys.exit(1)
