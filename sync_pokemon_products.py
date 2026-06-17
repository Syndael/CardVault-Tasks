import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv

from task_logger import TaskLogger, finalize_log

load_dotenv()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_API_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "CardVault-API"))

_logger: TaskLogger | None = None

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

PARAM_KEY_API_BASE = "sync.pokemon.products.api.base"
PARAM_KEY_CAR_TYPE = "sync.pokemon.products.card.type"
PARAM_KEY_MIG_LANG = "sync.pokemon.products.migration.languages"
PARAM_KEY_FILES_PATH = "sync.pokemon.products.img.path"
PARAM_KEY_IMG_PATH_PATTERN = "sync.pokemon.products.img.path.pattern"
PARAM_KEY_LOG_PATH = "tasks.log.path"
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
        return None
    except Exception:
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


def get_param(settings_by_key, param_key):
    param = settings_by_key.get(param_key)
    if param is None:
        raise RuntimeError(f"setting '{param_key}' not found")
    (_logger or print)(f"  {param_key}: {param}")
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
        (f.get("language") or {}).get("id")
        for f in files
        if (f.get("product") or {}).get("id") == product_id
    }


def get_tcgdex_image_url(image_base_url, lang):
    lang_image_url = image_base_url.replace("/en/", f"/{lang}/") if lang != "en" else image_base_url
    return f"{lang_image_url}/high.jpg"


def url_exists(url):
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def download_file(url):
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None


def sync():
    global _logger

    print(f"\n{SEP}")
    print(f"  Searching Pokémon cards via CardVault API")
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
    log_path_setting = get_param(settings_by_key, PARAM_KEY_LOG_PATH)
    print(f"  Languages img: {migration_languages}")
    print(SEP)

    log_dir = log_path_setting if os.path.isabs(log_path_setting) else os.path.join(_API_ROOT, log_path_setting)
    _logger = TaskLogger(log_dir, "pokemon_products")
    _logger.log(SEP)
    _logger.log("  Pokémon TCG sync started")
    _logger.log(SEP)
    _logger.log(f"  API: {API_BASE}")
    _logger.log(f"  Log path: {log_dir}")

    _logger.log("\n  Getting local API data...")
    types = api_get_all("types")
    languages = api_get_all("languages")
    card_type_id = get_card_type_id(types, card_type)
    lang_by_abr = get_lang_maps(languages)
    image_tcgdex_codes = [c for c in migration_languages.split(";") if c in lang_by_abr]

    image_file_type_id = None
    for t in types:
        if t.get("type") == "file" and t.get("name") == "image":
            image_file_type_id = t["id"]
            break
    if not image_file_type_id:
        raise RuntimeError("file type 'image' not found in types")

    tcgdex_codes = [l.get("tcgdex_language_code") for l in languages if l.get("tcgdex_language_code")]
    _logger.log(f"\n  Languages trad: {', '.join(tcgdex_codes)}")
    _logger.log(f"  Languages img: {', '.join(image_tcgdex_codes)}")

    _logger.log(f"\n  Getting pending cards...")
    pending = api_get_all("product-catalog", {
        "product_type_id": card_type_id,
        "pending_sync": 1,
        "per_page": 200
    })
    _logger.log(f"  {len(pending)} pending cards\n")

    all_collections = api_get_all("collections", {"per_page": 200})
    skip_ids = {c["id"] for c in all_collections if c.get("force_url")}
    if skip_ids:
        before = len(pending)
        pending = [p for p in pending if p["collection_id"] not in skip_ids]
        _logger.log(f"  Skip {before - len(pending)} cards from force_url collections\n")

    force_dl_cols = [c for c in all_collections if c.get("force_download") and not c.get("force_url") and not c.get("is_manual")]
    if force_dl_cols:
        added = 0
        existing_ids = {p["product_id"] for p in pending}
        for col in force_dl_cols:
            col_id = col["id"]
            col_code = col["code"]
            _logger.log(f"\n  Force-download collection {col_code}...")

            set_data = fetch_json(f"{api_base}/en/sets/{col_code}")
            if not set_data or not set_data.get("cards"):
                _logger.log(f"  Cannot fetch set {col_code} from TCGDex, skipping")
                continue
            tcgdex_cards = set_data["cards"]

            existing_products = api_get_all("product-catalog", {
                "product_type_id": card_type_id,
                "collection_code": col_code,
                "per_page": 200
            })
            existing_by_number = {p["product_number"]: p for p in existing_products}

            created = 0
            for card in tcgdex_cards:
                card_number = card.get("localId", "")
                if not card_number:
                    continue
                if card_number in existing_by_number:
                    continue
                result = api_request("POST", "products", {
                    "collection_id": col_id,
                    "product_type_id": card_type_id,
                    "product_number": card_number
                })
                if result:
                    created += 1
                    new_prod = api_get("product-catalog", {
                        "product_type_id": card_type_id,
                        "collection_code": col_code,
                        "product_number": card_number,
                        "per_page": 1
                    })
                    if new_prod and new_prod.get("items"):
                        pending.append(new_prod["items"][0])
                        existing_ids.add(new_prod["items"][0]["product_id"])
                        existing_by_number[card_number] = new_prod["items"][0]
                    _logger.log(f"    Created {col_code}-{card_number} ({card.get('name', '?')})")

            col_added = 0
            for cp in existing_products:
                if cp["product_id"] not in existing_ids:
                    pending.append(cp)
                    existing_ids.add(cp["product_id"])
                    col_added += 1

            _logger.log(f"  Collection {col_code}: {created} created, {col_added} existing added")
            added += created + col_added
        if added:
            _logger.log(f"  Total added from force-download collections: {added}\n")

    pending_ids = [p["product_id"] for p in pending]
    all_files = api_get_all("files", {"per_page": 500})
    img_by_product = {pid: get_existing_images(all_files, pid) for pid in pending_ids}

    stats = {"cards_ok": 0, "not_found": 0, "trans": 0, "img_ok": 0, "img_skip": 0, "img_fail": 0}
    current_set = None

    for i, product in enumerate(pending):
        product_id = product["product_id"]
        product_number = product["product_number"]
        set_code = product["collection_code"]
        tcgdex_id = f"{set_code}-{product_number}"

        if set_code != current_set:
            current_set = set_code
            _logger.log(f"\n  {SEP}\n  Set: {set_code}\n  {SEP}")

        line = f"  [{i + 1:>4}/{len(pending)}] {tcgdex_id:<22}"

        try:
            en_data = fetch_json(f"{api_base}/en/cards/{tcgdex_id}")
        except Exception as e:
            _logger.log(f"{line}  error: {e}")
            stats["not_found"] += 1
            continue
        if not en_data or not en_data.get("name"):
            _logger.log(f"{line}  not found")
            stats["not_found"] += 1
            continue

        en_name = en_data["name"]
        image_base = en_data.get("image", "")
        line += f" {en_name:<28}"

        try:
            translations = {}
            for tcgdex_code in image_tcgdex_codes:
                lang_id = lang_by_abr[tcgdex_code]
                if tcgdex_code == "en":
                    translations["en"] = {"name": en_name, "lang_id": lang_id}
                    continue
                try:
                    t = fetch_json(f"{api_base}/{tcgdex_code}/cards/{tcgdex_id}")
                except Exception as e:
                    _logger.log(f"  trans '{tcgdex_code}' error: {e}")
                    continue
                if t and t.get("name"):
                    translations[tcgdex_code] = {"name": t["name"], "lang_id": lang_id}
                time.sleep(0.05)

            line += f" trans:[{','.join(translations.keys())}]"

            img_results = []
            existing_img_lang_ids = img_by_product.get(product_id, set())

            if image_base:
                for tcgdex_code in image_tcgdex_codes:
                    lang_id = lang_by_abr.get(tcgdex_code)
                    if lang_id and lang_id in existing_img_lang_ids:
                        img_results.append(f"{tcgdex_code}=skip")
                        stats["img_skip"] += 1
                        continue

                    image_url = get_tcgdex_image_url(image_base, tcgdex_code)
                    if not url_exists(image_url):
                        img_results.append(f"{tcgdex_code}=—")
                        continue

                    original_name = f"{tcgdex_id}_{tcgdex_code}.jpg"
                    stored_name = f"{set_code}_{product_number}_{tcgdex_code}.jpg"

                    image_data = download_file(image_url)
                    if image_data is None:
                        img_results.append(f"{tcgdex_code}=✗")
                        stats["img_fail"] += 1
                        continue

                    is_manual_val = "1" if product.get("product_is_manual") else "0"
                    sub_dir = img_path_pattern.replace("{card_type}", card_type).replace("{is_manual}", is_manual_val).replace("{collection_code}", set_code)
                    base_dir = files_path if os.path.isabs(files_path) else os.path.join(_API_ROOT, files_path)
                    target_dir = os.path.join(base_dir, sub_dir)
                    os.makedirs(target_dir, exist_ok=True)

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
                            img_results.append(f"{tcgdex_code}=✓")
                            stats["img_ok"] += 1
                        else:
                            img_results.append(f"{tcgdex_code}=✗")
                            stats["img_fail"] += 1
                    except urllib.error.HTTPError:
                        img_results.append(f"{tcgdex_code}=✗")
                        stats["img_fail"] += 1

            line += f" img:[{','.join(img_results) or '—'}]"
            _logger.log(line)

            for tcgdex_code, t in translations.items():
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

            api_request("PATCH", f"products/{product_id}", {"force_download": False, "is_manual": False})

            stats["cards_ok"] += 1
        except Exception as e:
            _logger.log(f"  error processing product: {e}")
            stats["not_found"] += 1

        time.sleep(0.1)

    if force_dl_cols:
        for col in force_dl_cols:
            api_request("PATCH", f"collections/{col['id']}", {"force_download": False})
            _logger.log(f"  Cleared force_download for collection {col['code']}")

    _logger.log(f"\n{SEP}")
    _logger.log(f"  Cards OK: {stats['cards_ok']}")
    _logger.log(f"  Trads: {stats['trans']}")
    _logger.log(f"  Imgs OK: {stats['img_ok']}")
    if stats["img_skip"]:
        _logger.log(f"  Img skip: {stats['img_skip']}")
    if stats["img_fail"]:
        _logger.log(f"  Img fail: {stats['img_fail']}")
    if stats["not_found"]:
        _logger.log(f"  Cards not found: {stats['not_found']}")
    _logger.log(SEP + "\n")

    finalize_log(_logger, "pokemon_products", _API_ROOT, api_request)


if __name__ == "__main__":
    try:
        sync()
    except KeyboardInterrupt:
        print("\n  Interrupted")
        sys.exit(1)
