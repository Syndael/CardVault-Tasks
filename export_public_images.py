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

load_dotenv()

SETTING_KEY_PUBLIC_PATH = "export.public.images.path"
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
        raise


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
    return f"{coll_code}__{natural_pad(prod_number)}__{safe_name(prod_name)}__"

def old_card_type(item):
    pt = (item.get("product") or {}).get("product_type") or {}
    return pt.get("name") or "unknown"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def generate_data_js(public_path, sort_map=None, lang_map=None, cond_map=None):
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
        if sort_map:
            def sort_key(fname):
                inv_id = fname.split("-", 1)[0]
                key = sort_map.get(inv_id)
                if key:
                    return key + (inv_id,)
                return ("", "", "", "", fname)
            images.sort(key=sort_key)
        else:
            images.sort()
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
    public_path = settings_by_key.get(SETTING_KEY_PUBLIC_PATH)
    if not public_path:
        print(f"  ERROR: Setting '{SETTING_KEY_PUBLIC_PATH}' not found.")
        print(f"  Create it via API: POST /api/settings/ with")
        print(f"    setting_key='{SETTING_KEY_PUBLIC_PATH}'")
        print(f"    setting_value='/path/to/CardVault-Web-Publica'")
        sys.exit(1)
    print(f"  Public path: {public_path}\n")

    print("  Fetching tags...")
    all_tags = api_get_all("tags")
    matching_tags = {}
    for tag in all_tags:
        name_lower = tag["name"].lower()
        for category in TAG_CATEGORIES:
            if category in name_lower:
                matching_tags.setdefault(category, []).append(tag)
                break

    if not matching_tags:
        print("  No tags found containing 'album', 'caja' or 'vitrina'")
        return

    for category, tags in matching_tags.items():
        print(f"    {category}: {', '.join(t['name'] for t in tags)}")
    print()

    seen_items = {}
    for category, tags in matching_tags.items():
        for tag in tags:
            items = api_get_all("inventory", {"tag_name": tag["name"], "all": "1", "per_page": 100})
            for item in items:
                seen_items.setdefault(item["id"], item)

    print(f"  Total unique inventory items to process: {len(seen_items)}")
    print()

    sort_map = {}
    lang_map = {}
    cond_map = {}
    for inv_id, item in seen_items.items():
        product = item.get("product") or {}
        collection = product.get("collection") or {}
        coll_code = collection.get("code") or ""
        prod_number = product.get("product_number") or ""
        prod_num_padded = natural_pad(prod_number)
        prod_name = product.get("name") or ""
        sort_map[str(inv_id)] = (coll_code, prod_num_padded, prod_name)
        language = item.get("language") or {}
        lang_map[str(inv_id)] = (language.get("abbreviation") or "")[:3]
        condition = item.get("condition") or {}
        cond_map[str(inv_id)] = (condition.get("abbreviation") or "")[:5]

    copied = 0
    skipped = 0
    errors = 0

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

        collection = product.get("collection") or {}
        coll_code = collection.get("code") or ""
        prod_number = product.get("product_number") or ""
        prod_name = product.get("name") or ""
        sort_prefix = build_sort_prefix(coll_code, prod_number, prod_name)

        for category, section_name in active_categories.items():
            target_dir = os.path.join(public_path, category, card_type, section_name)
            os.makedirs(target_dir, exist_ok=True)

            old_type_name = product_type.get("name") or "unknown"
            if old_type_name != card_type:
                old_dir = os.path.join(public_path, category, old_type_name, section_name)
                if os.path.isdir(old_dir):
                    print(f"    Migrating {old_dir} → {target_dir}")
                    for fname in os.listdir(old_dir):
                        shutil.move(os.path.join(old_dir, fname), os.path.join(target_dir, fname))
                    try:
                        os.rmdir(old_dir)
                    except OSError:
                        pass

            for f in inv_files:
                file_id = f["id"]
                ext = get_extension(f.get("original_name", ""), f.get("file_path", ""))
                dest_name = f"{sort_prefix}{inv_id}-{file_id}{ext}"
                dest_path = os.path.join(target_dir, dest_name)

                old_name = f"{inv_id}-{file_id}{ext}"
                old_path = os.path.join(target_dir, old_name)
                if os.path.exists(dest_path):
                    skipped += 1
                    continue
                if os.path.exists(old_path):
                    os.rename(old_path, dest_path)
                    skipped += 1
                    continue

                file_url = resolve_url(f"/api/product-catalog/files/{file_id}/content")
                if not file_url:
                    errors += 1
                    continue

                data = download_file(file_url)
                if data is None:
                    print(f"    ERROR downloading inv file {file_id} for inv#{inv_id}")
                    errors += 1
                    continue

                with open(dest_path, "wb") as fh:
                    fh.write(data)
                copied += 1
                print(f"    [{category}/{section_name}] inv#{inv_id} file#{file_id} -> {dest_path}")

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
                    print(f"    [{category}/{section_name}] inv#{inv_id} prod -> {dest_path}")
                else:
                    print(f"    ERROR downloading product image for inv#{inv_id}")
                    errors += 1

    print("  Generating data.js...")
    generate_data_js(public_path, sort_map=sort_map, lang_map=lang_map, cond_map=cond_map)
    print("  data.js updated\n")

    print(f"\n  {SEP}")
    print(f"  Copied: {copied}")
    print(f"  Skipped: {skipped}")
    if errors:
        print(f"  Errors: {errors}")
    print(f"  {SEP}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrupted")
        sys.exit(1)
