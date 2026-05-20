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

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

PARAM_KEY_API_BASE = "sync.pokemon.products.api.base"
PARAM_KEY_CAR_TYPE = "sync.pokemon.products.card.type"
PARAM_KEY_MIG_LANG = "sync.pokemon.products.migration.languages"
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
        raise
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


def sync():
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
    print(f"  Languages img: {migration_languages}")
    print(SEP)

    print("\n  Getting local API data...")
    types = api_get_all("types")
    languages = api_get_all("languages")
    card_type_id = get_card_type_id(types, card_type)
    lang_by_abr = get_lang_maps(languages)
    image_tcgdex_codes = [c for c in migration_languages.split(";") if c in lang_by_abr]

    # Find file type id for "image"
    image_file_type_id = None
    for t in types:
        if t.get("type") == "file" and t.get("name") == "image":
            image_file_type_id = t["id"]
            break
    if not image_file_type_id:
        raise RuntimeError("file type 'image' not found in types")

    tcgdex_codes = [l.get("tcgdex_language_code") for l in languages if l.get("tcgdex_language_code")]
    print(f"\n  Languages trad: {', '.join(tcgdex_codes)}")
    print(f"  Languages img: {', '.join(image_tcgdex_codes)}")

    print(f"\n  Getting pending cards...")
    pending = api_get_all("product-catalog", {
        "product_type_id": card_type_id,
        "pending_sync": 1,
        "per_page": 200
    })
    print(f"  {len(pending)} pending cards\n")
    if not pending:
        return

    # Fetch all existing files for all pending product ids
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
            print(f"\n  {SEP}")
            print(f"  Set: {set_code}")
            print(f"  {SEP}")

        print(f"  [{i + 1:>4}/{len(pending)}] {tcgdex_id:<22}", end="", flush=True)

        en_data = fetch_json(f"{api_base}/en/cards/{tcgdex_id}")
        if not en_data or not en_data.get("name"):
            print("  not found")
            stats["not_found"] += 1
            continue

        en_name = en_data["name"]
        image_base = en_data.get("image", "")
        print(f" {en_name:<28}", end="", flush=True)

        translations = {}
        for tcgdex_code in image_tcgdex_codes:
            lang_id = lang_by_abr[tcgdex_code]
            if tcgdex_code == "en":
                translations["en"] = {"name": en_name, "lang_id": lang_id}
                continue
            t = fetch_json(f"{api_base}/{tcgdex_code}/cards/{tcgdex_id}")
            if t and t.get("name"):
                translations[tcgdex_code] = {"name": t["name"], "lang_id": lang_id}
            time.sleep(0.05)

        print(f" trans:[{','.join(translations.keys())}]", end="", flush=True)

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

                try:
                    result = api_request("POST", "files", {
                        "product_id": product_id,
                        "language_id": lang_id,
                        "file_type_id": image_file_type_id,
                        "original_name": original_name,
                        "stored_name": stored_name,
                        "file_path": image_url,
                        "file_size": 0
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

        print(f" img:[{','.join(img_results) or '—'}]")

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

        # force_download = 0
        api_request("PATCH", f"products/{product_id}", {"force_download": False})

        stats["cards_ok"] += 1
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
