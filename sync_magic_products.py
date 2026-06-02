import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_API_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "CardVault-API"))

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

PARAM_KEY_API_BASE = "sync.magic.products.api.base"
PARAM_KEY_CAR_TYPE = "sync.magic.products.card.type"
PARAM_KEY_MIG_LANG = "sync.magic.products.migration.languages"
PARAM_KEY_FILES_PATH = "sync.magic.products.img.path"
PARAM_KEY_IMG_PATH_PATTERN = "sync.magic.products.img.path.pattern"
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


def fetch_json(url):
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        return None
    except Exception:
        return None


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
        print(f"\n  [API {e.code}] {method} {path}")
        return None
    except Exception as e:
        print(f"\n  [API error] {method} {path}: {e}")
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


def get_param(settings_by_key, param_key):
    param = settings_by_key.get(param_key)
    if param is None:
        raise RuntimeError(f"setting '{param_key}' not found")
    print(f"  {param_key}: {param}")
    return param


def get_card_type_id(types, card_type):
    for item in types:
        if item.get("type") == "card" and item.get("short_name") == card_type:
            return item["id"]
    raise RuntimeError(f"card_type '{card_type}' not found")


def get_lang_maps(languages):
    by_abr = {}
    for lang in languages:
        code = lang.get("tcgdex_language_code") or ""
        if code:
            by_abr[code] = lang["id"]
    return by_abr


def get_existing_images(files, product_id):
    return {
        f["language_id"]
        for f in files
        if f.get("product_id") == product_id
    }


def get_scryfall_image_url(card_data):
    uris = card_data.get("image_uris") or {}
    if uris:
        return uris.get("normal") or uris.get("large") or uris.get("small")
    faces = card_data.get("card_faces") or []
    for face in faces:
        furis = face.get("image_uris") or {}
        url = furis.get("normal") or furis.get("large") or furis.get("small")
        if url:
            return url
    return None


def download_file(url):
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None


def sync():
    print(f"\n{SEP}")
    print(f"  Searching Magic: The Gathering cards via CardVault API")
    print(SEP)
    print(f"  API: {API_BASE}")

    print("\n  Getting params...")
    settings = api_get_all("settings")
    settings_by_key = {item["setting_key"]: item.get("setting_value") for item in settings}
    api_base = get_param(settings_by_key, PARAM_KEY_API_BASE)
    card_type = get_param(settings_by_key, PARAM_KEY_CAR_TYPE)
    migration_languages = get_param(settings_by_key, PARAM_KEY_MIG_LANG)
    files_path = get_param(settings_by_key, PARAM_KEY_FILES_PATH)
    img_path_pattern = get_param(settings_by_key, PARAM_KEY_IMG_PATH_PATTERN)
    print(f"  Languages: {migration_languages}")
    print(SEP)

    print("\n  Getting local API data...")
    types = api_get_all("types")
    languages = api_get_all("languages")
    card_type_id = get_card_type_id(types, card_type)
    lang_by_abr = get_lang_maps(languages)
    image_scryfall_codes = [c for c in migration_languages.split(";") if c in lang_by_abr]

    image_file_type_id = None
    for t in types:
        if t.get("type") == "file" and t.get("name") == "image":
            image_file_type_id = t["id"]
            break
    if not image_file_type_id:
        raise RuntimeError("file type 'image' not found in types")

    scryfall_codes = [l.get("tcgdex_language_code") for l in languages if l.get("tcgdex_language_code")]
    print(f"\n  Languages trad: {', '.join(scryfall_codes)}")
    print(f"  Languages img: {', '.join(image_scryfall_codes)}")

    print(f"\n  Getting pending cards...")
    pending = api_get_all("product-catalog", {
        "product_type_id": card_type_id,
        "pending_sync": 1,
        "per_page": 200
    })
    print(f"  {len(pending)} pending cards\n")
    if not pending:
        return

    pending_ids = [p["product_id"] for p in pending]
    all_files = api_get_all("files", {"per_page": 500})
    img_by_product = {pid: get_existing_images(all_files, pid) for pid in pending_ids}

    stats = {"cards_ok": 0, "not_found": 0, "trans": 0, "img_ok": 0, "img_skip": 0, "img_fail": 0}
    current_set = None

    for i, product in enumerate(pending):
        product_id = product["product_id"]
        product_number = product["product_number"]
        set_code = product["collection_code"]
        scryfall_id = f"{set_code}/{product_number}"

        if set_code != current_set:
            current_set = set_code
            print(f"\n  {SEP}")
            print(f"  Set: {set_code}")
            print(f"  {SEP}")

        print(f"  [{i + 1:>4}/{len(pending)}] {scryfall_id:<22}", end="", flush=True)

        try:
            en_data = fetch_json(f"{api_base}/cards/{scryfall_id}")
        except Exception as e:
            print(f"  error: {e}")
            stats["not_found"] += 1
            continue
        if not en_data or not en_data.get("name"):
            print("  not found")
            stats["not_found"] += 1
            continue

        en_name = en_data["name"]
        en_image_url = get_scryfall_image_url(en_data)
        print(f" {en_name:<36}", end="", flush=True)

        try:
            translations = {}
            per_lang_image_url = {}
            for scryfall_code in image_scryfall_codes:
                lang_id = lang_by_abr[scryfall_code]
                if scryfall_code == "en":
                    translations["en"] = {"name": en_name, "lang_id": lang_id}
                    if en_image_url:
                        per_lang_image_url["en"] = en_image_url
                    continue
                try:
                    t = fetch_json(f"{api_base}/cards/{scryfall_id}?locale={scryfall_code}")
                except Exception as e:
                    print(f"  trans '{scryfall_code}' error: {e}")
                    continue
                if t:
                    if t.get("name"):
                        translations[scryfall_code] = {"name": t["name"], "lang_id": lang_id}
                    img_url = get_scryfall_image_url(t)
                    if img_url and img_url != en_image_url:
                        per_lang_image_url[scryfall_code] = img_url
                time.sleep(0.05)

            print(f" trans:[{','.join(translations.keys())}]", end="", flush=True)

            img_results = []
            existing_img_lang_ids = img_by_product.get(product_id, set())

            is_manual_val = "1" if product.get("product_is_manual") else "0"
            sub_dir = img_path_pattern.replace("{card_type}", card_type).replace("{is_manual}", is_manual_val).replace("{collection_code}", set_code)
            base_dir = files_path if os.path.isabs(files_path) else os.path.join(_API_ROOT, files_path)
            target_dir = os.path.join(base_dir, sub_dir)
            os.makedirs(target_dir, exist_ok=True)

            for scryfall_code in image_scryfall_codes:
                lang_id = lang_by_abr.get(scryfall_code)
                if lang_id and lang_id in existing_img_lang_ids:
                    img_results.append(f"{scryfall_code}=skip")
                    stats["img_skip"] += 1
                    continue

                img_url_to_dl = per_lang_image_url.get(scryfall_code)
                if not img_url_to_dl:
                    img_results.append(f"{scryfall_code}=—")
                    continue

                image_data = download_file(img_url_to_dl)
                if image_data is None:
                    img_results.append(f"{scryfall_code}=✗")
                    stats["img_fail"] += 1
                    continue

                original_name = f"{set_code}_{product_number}_{scryfall_code}.jpg"
                stored_name = original_name
                local_rel = os.path.join(files_path, sub_dir, stored_name)
                local_abs = os.path.join(base_dir, sub_dir, stored_name)
                with open(local_abs, "wb") as f:
                    f.write(image_data)
                file_size = len(image_data)

                try:
                    result = api_request("POST", "files", {
                        "product_id": product_id,
                        "language_id": lang_id,
                        "file_type_id": image_file_type_id,
                        "original_name": original_name,
                        "stored_name": stored_name,
                        "file_path": local_rel,
                        "file_size": file_size
                    })
                    if result:
                        img_results.append(f"{scryfall_code}=✓")
                        stats["img_ok"] += 1
                    else:
                        img_results.append(f"{scryfall_code}=✗")
                        stats["img_fail"] += 1
                except urllib.error.HTTPError:
                    img_results.append(f"{scryfall_code}=✗")
                    stats["img_fail"] += 1

            print(f" img:[{','.join(img_results) or '—'}]")

            for scryfall_code, t in translations.items():
                existing = api_get("product-translations", {
                    "product_id": product_id,
                    "language_id": t["lang_id"],
                    "per_page": 1
                })
                existing_items = (existing or {}).get("items", [])
                if existing_items:
                    api_request("PATCH", f"product-translations/{existing_items[0]['id']}", {
                        "name": t["name"]
                    })
                else:
                    api_request("POST", "product-translations", {
                        "product_id": product_id,
                        "language_id": t["lang_id"],
                        "name": t["name"]
                    })
                stats["trans"] += 1

            img_ok_count = sum(1 for r in img_results if r.endswith("✓") or r.endswith("skip"))
            if img_results and img_ok_count == 0:
                print("  no image saved, retry pending")
                stats["not_found"] += 1
            else:
                api_request("PATCH", f"products/{product_id}", {"force_download": False, "is_manual": False})
                stats["cards_ok"] += 1
        except Exception as e:
            print(f"  error processing product: {e}")
            stats["not_found"] += 1

        time.sleep(0.1)

    print(f"\n{SEP}")
    print(f"  Cards OK: {stats['cards_ok']}")
    print(f"  Trads: {stats['trans']}")
    print(f"  Imgs OK: {stats['img_ok']}")
    if stats["img_skip"]:
        print(f"  Img skip: {stats['img_skip']}")
    if stats["img_fail"]:
        print(f"  Img fail: {stats['img_fail']}")
    if stats["not_found"]:
        print(f"  Cards not found: {stats['not_found']}")
    print(SEP + "\n")


if __name__ == "__main__":
    try:
        sync()
    except KeyboardInterrupt:
        print("\n  Interrupted")
        sys.exit(1)
