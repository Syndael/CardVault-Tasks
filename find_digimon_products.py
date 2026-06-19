import concurrent.futures
import hashlib
import json
import os
import re
import sys
import threading
import time
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

PARAM_KEY_API_BASE = "sync.digimon.products.api.base"
PARAM_KEY_CAR_TYPE = "sync.digimon.products.card.type"
PARAM_KEY_MIG_LANG = "sync.digimon.products.migration.languages"
PARAM_KEY_FILES_PATH = "sync.digimon.products.img.path"
PARAM_KEY_IMG_PATH_PATTERN = "sync.digimon.products.img.path.pattern"
PARAM_KEY_FILTER_COL = "sync.digimon.products.filter.collections"
PARAM_KEY_LOG_PATH = "tasks.log.path"
SEP = "=" * 58

_URL_GLOBAL_OLD = "https://world.digimoncard.com/images/cardlist/card"
_URL_BANDAI = "https://s3.amazonaws.com/prod.bandaitcgplus.files.api/card_image/DG-EN"
_URL_JP = "https://digimoncard.com/images/cardlist/card"
_URL_DIGIMON_IO = "https://images.digimoncard.io/images/cards"
_IMG_EXT = ".png"
_stats_lock = threading.Lock()
_token: str | None = None
_token_expires_at: datetime | None = None


def _login() -> bool:
    global _token, _token_expires_at
    if not API_USERNAME or not API_PASSWORD:
        return False
    try:
        body = json.dumps({"username": API_USERNAME, "password": API_PASSWORD}).encode("utf-8")
        req = urllib.request.Request(
            f"{API_BASE.rstrip('/')}/auth/login", data=body,
            headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
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
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "CardVault/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
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
        (_logger or print)(f"\n  [API {e.code}] {method} {path}")
        return None
    except Exception as e:
        (_logger or print)(f"\n  [API error] {method} {path}: {e}")
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


def api_post(path, data):
    return api_request("POST", path, data)


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


# ── URL builders ──────────────────────────────────────

def _build_en_urls(card_id, set_code):
    urls = []
    for fmt in (
        card_id,
        f"{card_id}_dummy",
        f"{card_id}_D",
        f"e_{card_id}",
        f"e_{card_id}_dummy",
        f"e_{card_id}_D",
        f"e_{card_id}_D_sam",
    ):
        urls.append(f"{_URL_BANDAI}/{set_code}/{fmt}{_IMG_EXT}")
    if "_P" in card_id:
        std_id = card_id.rsplit("_P", 1)[0]
        alt_suffix = card_id[len(std_id):]  # e.g. "_P1"
        if alt_suffix == "_P":
            for fmt in (
                f"e_{std_id}p_D",
                f"e_{std_id}P_D_sam",
                f"{std_id}P_dummy",
                f"{std_id}P",
                f"{std_id}_P",
                f"e_{std_id}_P",
                f"e_{std_id}_P_D",
            ):
                urls.append(f"{_URL_BANDAI}/{set_code}/{fmt}{_IMG_EXT}")
        for fmt in (
            f"{std_id}{alt_suffix}",
            f"e_{std_id}{alt_suffix}",
            f"e_{std_id}{alt_suffix}_D",
        ):
            urls.append(f"{_URL_BANDAI}/{set_code}/{fmt}{_IMG_EXT}")
    urls.append(f"{_URL_GLOBAL_OLD}/{card_id}{_IMG_EXT}")
    urls.append(f"{_URL_DIGIMON_IO}/{urllib.parse.quote(card_id)}.jpg")
    return urls


def _build_jp_urls(card_id):
    return [f"{_URL_JP}/{card_id}{_IMG_EXT}"]


def download_file(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CardVault/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None


def try_download_first(urls):
    seen_urls = set()
    for url in urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        data = download_file(url)
        if data:
            return data, url
    return None, None


def try_download_image_en(card_id, set_code):
    return try_download_first(_build_en_urls(card_id, set_code))


def try_download_image_jp(card_id, _=None):
    return try_download_first(_build_jp_urls(card_id))


def check_url_exists(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "CardVault/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def check_image_exists_jp(card_id):
    for url in _build_jp_urls(card_id):
        if check_url_exists(url):
            return True
    return False


def image_digest(image_data):
    return hashlib.sha256(image_data).hexdigest() if image_data else None


def is_alt_card(cardnumber):
    return "_P" in cardnumber


def is_standard_card(cardnumber):
    parts = cardnumber.split("-", 1)
    if len(parts) < 2:
        return False
    num_part = parts[1]
    return bool(re.match(r"^\d+$", num_part))


def get_standard_number(cardnumber):
    parts = cardnumber.split("-", 1)
    return parts[1] if len(parts) >= 2 else cardnumber


def get_collection_code(cardnumber):
    m = re.match(r"^([A-Za-z0-9_-]+)-", cardnumber)
    return m.group(1) if m else None


def download_and_register_image(product_id, card_id, set_code, lang_id,
                                image_file_type_id, files_path, img_path_pattern, card_type,
                                try_download_fn, lang_suffix="en"):
    image_data, source_url = try_download_fn(card_id, set_code)
    if image_data is None:
        return False

    ext = os.path.splitext(urllib.parse.urlparse(source_url).path)[1] or ".png"
    original_name = f"{card_id}_{lang_suffix}{ext}"
    stored_name = original_name
    sub_dir = img_path_pattern.replace("{card_type}", card_type).replace("{is_manual}", "0").replace("{collection_code}", set_code)
    base_dir = files_path if os.path.isabs(files_path) else os.path.join(_API_ROOT, files_path)
    target_dir = os.path.join(base_dir, sub_dir)
    os.makedirs(target_dir, exist_ok=True)

    local_rel = os.path.join(files_path, sub_dir, stored_name)
    local_abs = os.path.join(base_dir, sub_dir, stored_name)
    with open(local_abs, "wb") as f:
        f.write(image_data)
    file_size = len(image_data)

    result = api_post("files", {
        "product_id": product_id,
        "language_id": lang_id,
        "file_type_id": image_file_type_id,
        "original_name": original_name,
        "stored_name": stored_name,
        "file_path": local_rel,
        "file_size": file_size,
    })
    return result is not None


def _alt_sort_key(alt_num):
    m = re.match(r"^(.+)_P(\d*)$", alt_num)
    if not m:
        return alt_num, -1
    suffix = m.group(2)
    return m.group(1), int(suffix) if suffix else 0


def _standard_from_alt(alt_num):
    return alt_num.split("_P", 1)[0]


def _download_unique_alt_digest(set_code, alt_num, seen_digests):
    image_data, _ = try_download_image_en(f"{set_code}-{alt_num}", set_code)
    digest = image_digest(image_data)
    if not digest or digest in seen_digests:
        return None
    seen_digests.add(digest)
    return digest


def _base_seen_digests(set_code, std_num):
    image_data, _ = try_download_image_en(f"{set_code}-{std_num}", set_code)
    digest = image_digest(image_data)
    return {digest} if digest else set()


def _check_std_num_alts(args):
    set_code, std_num, existing_numbers, found_from_api = args
    alt_exists = set()
    seen_digests = _base_seen_digests(set_code, std_num)

    alt_p = f"{std_num}_P"
    if alt_p not in found_from_api and alt_p not in existing_numbers:
        if _download_unique_alt_digest(set_code, alt_p, seen_digests):
            alt_exists.add(alt_p)

    for i in range(1, 11):
        alt_num = f"{std_num}_P{i}"
        if alt_num in found_from_api or alt_num in existing_numbers:
            continue
        if _download_unique_alt_digest(set_code, alt_num, seen_digests):
            alt_exists.add(alt_num)
        else:
            break

    return alt_exists


def _validate_api_alt_arts(set_code, api_alt_nums, standard_numbers):
    standard_set = set(standard_numbers)
    by_standard = {}
    for alt_num in api_alt_nums:
        std_num = _standard_from_alt(alt_num)
        if std_num in standard_set:
            by_standard.setdefault(std_num, []).append(alt_num)

    valid = set()
    for std_num, alt_nums in by_standard.items():
        seen_digests = _base_seen_digests(set_code, std_num)
        for alt_num in sorted(alt_nums, key=_alt_sort_key):
            if _download_unique_alt_digest(set_code, alt_num, seen_digests):
                valid.add(alt_num)
    return valid


def find_missing_alt_arts(set_code, standard_numbers, existing_numbers, card_numbers=None):
    found_from_api = set()

    if card_numbers:
        for cn in card_numbers:
            if not is_alt_card(cn):
                continue
            parts = cn.split("-", 1)
            if len(parts) < 2:
                continue
            alt_num = parts[1]
            if alt_num not in existing_numbers:
                found_from_api.add(alt_num)

    found = _validate_api_alt_arts(set_code, found_from_api, standard_numbers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [
            executor.submit(
                _check_std_num_alts,
                (set_code, std_num, existing_numbers, found_from_api)
            )
            for std_num in standard_numbers
        ]
        for future in concurrent.futures.as_completed(futures):
            found.update(future.result())

    return sorted(found)


def _create_product(set_code, num, collection, card_type_id, product_format_id):
    result = api_post("products", {
        "collection_id": collection["id"],
        "product_type_id": card_type_id,
        "product_format_id": product_format_id,
        "product_number": num,
    })
    if not result:
        return None
    return result.get("id")


def _try_register_image(product_id, card_id, set_code, lang_id,
                        image_file_type_id, files_path, img_path_pattern, card_type,
                        try_download_fn, lang_suffix, existing_lang_ids):
    if lang_id and lang_id not in existing_lang_ids:
        ok = download_and_register_image(
            product_id, card_id, set_code, lang_id,
            image_file_type_id, files_path, img_path_pattern, card_type,
            try_download_fn, lang_suffix
        )
        return "ok" if ok else "fail"
    return "skip"


def _process_standard_card(params):
    (set_code, num, collection, card_type_id, product_format_id, en_lang_id, jp_lang_id,
     image_file_type_id, files_path, img_path_pattern, card_type,
     api_base, existing_nums, existing_images_by_product, stats) = params

    search_result = fetch_json(
        f"{api_base.rstrip('/')}/search?card={urllib.parse.quote(f'{set_code}-{num}')}"
    )
    if not search_result or not isinstance(search_result, list) or not search_result:
        return None

    card_id = f"{set_code}-{num}"
    product_id = _create_product(set_code, num, collection, card_type_id, product_format_id)
    if not product_id:
        return None

    existing_lang_ids = existing_images_by_product.get(product_id, set())
    en_st = _try_register_image(
        product_id, card_id, set_code, en_lang_id,
        image_file_type_id, files_path, img_path_pattern, card_type,
        try_download_image_en, "en", existing_lang_ids
    )
    jp_st = _try_register_image(
        product_id, card_id, set_code, jp_lang_id,
        image_file_type_id, files_path, img_path_pattern, card_type,
        try_download_image_jp, "jp", existing_lang_ids
    )

    with _stats_lock:
        existing_nums.add(num)
        stats["new_standard"] += 1
        stats["images_downloaded"] += (1 if en_st == "ok" else 0) + (1 if jp_st == "ok" else 0)
        stats["images_skipped"] += (1 if en_st != "ok" else 0) + (1 if jp_st != "ok" else 0)

    return f"    + {card_id:<26} created (id={product_id}) en={en_st} jp={jp_st}"


def _process_jp_image(params):
    (set_code, num, product_id, jp_lang_id, image_file_type_id,
     files_path, img_path_pattern, card_type, existing_images_by_product, stats) = params

    existing_lang_ids = existing_images_by_product.get(product_id, set())
    if not jp_lang_id or jp_lang_id in existing_lang_ids:
        return None

    card_id = f"{set_code}-{num}"
    jp_st = _try_register_image(
        product_id, card_id, set_code, jp_lang_id,
        image_file_type_id, files_path, img_path_pattern, card_type,
        try_download_image_jp, "jp", existing_lang_ids
    )

    with _stats_lock:
        existing_images_by_product.setdefault(product_id, set()).add(jp_lang_id)
        if jp_st == "ok":
            stats["images_downloaded"] += 1
            stats["jp_images_added"] += 1
        else:
            stats["images_skipped"] += 1

    return f"    ~ {card_id:<26} existing (id={product_id}) jp={jp_st}"


def _process_alt_art(params):
    (set_code, alt_num, collection, card_type_id, product_format_id, en_lang_id, jp_lang_id,
     image_file_type_id, files_path, img_path_pattern, card_type,
     existing_nums, existing_images_by_product, stats) = params

    card_id = f"{set_code}-{alt_num}"
    results = []

    en_pid = _create_product(set_code, alt_num, collection, card_type_id, product_format_id)
    if en_pid:
        with _stats_lock:
            existing_nums.add(alt_num)
            stats["alt_arts_created"] += 1

        existing_lang_ids = existing_images_by_product.get(en_pid, set())
        en_st = _try_register_image(
            en_pid, card_id, set_code, en_lang_id,
            image_file_type_id, files_path, img_path_pattern, card_type,
            try_download_image_en, "en", existing_lang_ids
        )

        with _stats_lock:
            if en_st == "ok":
                stats["images_downloaded"] += 1
            else:
                stats["images_skipped"] += 1

        results.append(f"    + {card_id:<26} created (id={en_pid}) en={en_st}")

        if jp_lang_id:
            jp_num = f"{alt_num}JP"
            if jp_num not in existing_nums and check_image_exists_jp(card_id):
                jp_pid = _create_product(set_code, jp_num, collection, card_type_id, product_format_id)
                if jp_pid:
                    with _stats_lock:
                        existing_nums.add(jp_num)
                        stats["alt_arts_created"] += 1

                    ok = download_and_register_image(
                        jp_pid, card_id, set_code, jp_lang_id,
                        image_file_type_id, files_path, img_path_pattern, card_type,
                        try_download_image_jp, "jp"
                    )

                    with _stats_lock:
                        stats["images_downloaded"] += 1 if ok else 0
                        stats["images_skipped"] += 0 if ok else 1

                    results.append(f"    + {card_id:<26}JP created (id={jp_pid}) jp={'ok' if ok else 'fail'}")
    else:
        results.append(f"    ! {card_id:<26} EN failed to create")

    return "\n".join(results)


def sync():
    global _logger

    print(f"\n{SEP}")
    print("  Find Digimon products (new cards + alt arts)")
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
    filter_collections_raw = settings_by_key.get(PARAM_KEY_FILTER_COL, "")
    filter_collections = {c.strip() for c in filter_collections_raw.split(";") if c.strip()} if filter_collections_raw else None
    if filter_collections:
        print(f"  Filter collections: {', '.join(sorted(filter_collections))}")
    print(f"  Languages: {migration_languages}")
    log_path_setting = get_param(settings_by_key, PARAM_KEY_LOG_PATH)
    print(SEP)

    log_dir = log_path_setting if os.path.isabs(log_path_setting) else os.path.join(_API_ROOT, log_path_setting)
    _logger = TaskLogger(log_dir, "find_digimon_products")
    _logger.log(SEP)
    _logger.log("  Find Digimon products started")
    _logger.log(SEP)
    _logger.log(f"  API: {API_BASE}")
    _logger.log(f"  Log path: {log_dir}")

    _logger.log("\n  Getting local API data...")
    types = api_get_all("types")
    card_type_id = get_card_type_id(types, card_type)
    languages = api_get_all("languages")
    lang_by_abr = get_lang_maps(languages)
    en_lang_id = lang_by_abr.get("en")
    jp_lang_id = lang_by_abr.get("ja")
    image_codes = [c for c in migration_languages.split(";") if c in lang_by_abr]

    image_file_type_id = None
    for t in types:
        if t.get("type") == "file" and t.get("name") == "image":
            image_file_type_id = t["id"]
            break
    if not image_file_type_id:
        raise RuntimeError("file type 'image' not found in types")

    product_format_id = None
    for t in types:
        if t.get("type") == "product_format" and t.get("name") == "carta":
            product_format_id = t["id"]
            break
    if not product_format_id:
        raise RuntimeError("product format 'carta' not found in types")

    collections = api_get_all("collections")
    digimon_collections = {}
    for col in collections:
        ct = col.get("card_type") or {}
        if ct.get("id") == card_type_id:
            digimon_collections[col["code"]] = col
    _logger.log(f"  {len(digimon_collections)} digimon collections found")

    existing_products = api_get_all("product-catalog", {
        "product_type_id": card_type_id, "per_page": 200
    })
    existing_by_collection = {}
    existing_product_map = {}
    for p in existing_products:
        code = p.get("collection_code", "")
        num = p.get("product_number", "")
        existing_by_collection.setdefault(code, set()).add(num)
        pid = p.get("product_id") or p.get("id")
        if pid:
            existing_product_map[(code, num)] = pid
    total_existing = len(existing_products)
    _logger.log(f"  {total_existing} existing products")

    all_files = api_get_all("files", {"per_page": 500})
    existing_images_by_product = {}
    for f in all_files:
        pid = f.get("product_id")
        if pid is None:
            pid_obj = f.get("product")
            pid = pid_obj.get("id") if isinstance(pid_obj, dict) else None
        if pid:
            lang_id = f.get("language_id")
            if lang_id is None:
                lang_obj = f.get("language")
                lang_id = lang_obj.get("id") if isinstance(lang_obj, dict) else None
            if lang_id is not None:
                existing_images_by_product.setdefault(pid, set()).add(lang_id)

    _logger.log(f"\n  Fetching all cards from Digimon TCG API...")
    all_cards = fetch_json(f"{api_base.rstrip('/')}/getAllCards")
    if not all_cards:
        _logger.log("  Failed to fetch cards from API")
        finalize_log(_logger, "find_digimon_products", _API_ROOT, api_request)
        sys.exit(1)
    _logger.log(f"  {len(all_cards)} cards in API")

    _logger.log("\n  Grouping cards by collection...")
    groups = {}
    for card in all_cards:
        cn = card.get("cardnumber", "")
        code = get_collection_code(cn)
        if code:
            groups.setdefault(code, []).append(cn)

    stats = {
        "new_standard": 0,
        "existing_standard": 0,
        "alt_arts_created": 0,
        "jp_images_added": 0,
        "images_downloaded": 0,
        "images_skipped": 0,
        "errors": 0,
    }

    known_collections = set(digimon_collections.keys())
    all_set_codes = sorted(
        groups.keys(),
        key=lambda x: (
            re.sub(r"\d+", "", x),
            int(re.search(r"\d+", x).group()) if re.search(r"\d+", x) else 0
        )
    )
    if filter_collections:
        all_set_codes = [c for c in all_set_codes if c in filter_collections]
        _logger.log(f"  Filtered to {len(all_set_codes)} collections: {', '.join(all_set_codes)}")

    # ── Phase 1: Create missing standard cards + download images ──
    _logger.log(f"\n{'=' * 58}")
    _logger.log(f"  Phase 1: Create missing standard cards + download images")
    _logger.log(f"{'=' * 58}")

    for set_code in all_set_codes:
        if set_code not in known_collections:
            continue

        card_numbers = groups[set_code]
        existing_nums = existing_by_collection.get(set_code, set())
        collection = digimon_collections[set_code]

        standard_nums = sorted({
            get_standard_number(cn)
            for cn in card_numbers
            if is_standard_card(cn)
        })

        new_cards = [num for num in standard_nums if num not in existing_nums]
        stats["existing_standard"] += len(standard_nums) - len(new_cards)

        if new_cards:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                futures = {
                    executor.submit(
                        _process_standard_card,
                        (set_code, num, collection, card_type_id, product_format_id, en_lang_id, jp_lang_id,
                         image_file_type_id, files_path, img_path_pattern, card_type,
                         api_base, existing_nums, existing_images_by_product, stats)
                    ): num
                for num in new_cards
            }
            for future in concurrent.futures.as_completed(futures):
                msg = future.result()
                if msg:
                    _logger.log(msg)

            created = sum(1 for num in new_cards if num in existing_nums)
            if created:
                _logger.log(f"  {set_code}: +{created} new standard cards")

    # ── Phase 1.5: Add JP images to existing standard products ──
    _logger.log(f"\n{'=' * 58}")
    _logger.log(f"  Phase 1.5: Add JP images to existing standard products")
    _logger.log(f"{'=' * 58}")

    jp_candidates = []
    for set_code in all_set_codes:
        if set_code not in known_collections:
            continue

        existing_nums = existing_by_collection.get(set_code, set())
        standard_nums = sorted({
            get_standard_number(cn)
            for cn in groups[set_code]
            if is_standard_card(cn)
        })

        for num in standard_nums:
            if num not in existing_nums:
                continue
            product_id = existing_product_map.get((set_code, num))
            if not product_id:
                continue
            existing_lang_ids = existing_images_by_product.get(product_id, set())
            if not jp_lang_id or jp_lang_id in existing_lang_ids:
                continue
            jp_candidates.append((set_code, num, product_id))

    if jp_candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(
                    _process_jp_image,
                    (set_code, num, product_id, jp_lang_id, image_file_type_id,
                     files_path, img_path_pattern, card_type, existing_images_by_product, stats)
                ): (set_code, num)
                for set_code, num, product_id in jp_candidates
            }
            for future in concurrent.futures.as_completed(futures):
                msg = future.result()
                if msg:
                    _logger.log(msg)

    with _stats_lock:
        added = stats["jp_images_added"]
    if added:
        _logger.log(f"\n  JP images added to {added} existing products")

    # ── Phase 2: Detect and create alt arts + download images ──
    _logger.log(f"\n{'=' * 58}")
    _logger.log(f"  Phase 2: Detect and create alt arts + download images")
    _logger.log(f"{'=' * 58}")

    for set_code in all_set_codes:
        if set_code not in known_collections:
            continue

        card_numbers = groups[set_code]
        existing_nums = existing_by_collection.get(set_code, set())
        collection = digimon_collections[set_code]

        standard_nums = sorted({
            get_standard_number(cn)
            for cn in card_numbers
            if is_standard_card(cn)
        })

        alt_nums = find_missing_alt_arts(set_code, standard_nums, existing_nums, card_numbers)
        total_alts = len(alt_nums)

        if alt_nums:
            _logger.log(f"  {set_code}: processing {total_alts} alt arts...")

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                futures = {
                    executor.submit(
                        _process_alt_art,
                        (set_code, alt_num, collection, card_type_id, product_format_id, en_lang_id, jp_lang_id,
                         image_file_type_id, files_path, img_path_pattern, card_type,
                         existing_nums, existing_images_by_product, stats)
                    ): alt_num
                    for alt_num in alt_nums
                }
                for future in concurrent.futures.as_completed(futures):
                    msg = future.result()
                    if msg:
                        _logger.log(msg)

            created = sum(1 for alt_num in alt_nums if alt_num in existing_nums)
            if created:
                _logger.log(f"  {set_code}: +{created} alt arts")

    # ── Phase 3: Fill gaps in standard card numbering ──
    _logger.log(f"\n{'=' * 58}")
    _logger.log(f"  Phase 3: Fill gaps in standard card numbering (1..max)")
    _logger.log(f"{'=' * 58}")

    for set_code in all_set_codes:
        if set_code not in known_collections:
            continue

        card_numbers = groups[set_code]
        existing_nums = existing_by_collection.get(set_code, set())
        collection = digimon_collections[set_code]

        standard_nums = sorted({
            get_standard_number(cn)
            for cn in card_numbers
            if is_standard_card(cn)
        })
        if not standard_nums:
            continue

        int_nums = [int(n) for n in standard_nums]
        max_num = max(int_nums)
        pad = len(standard_nums[0])

        missing = []
        for i in range(1, max_num + 1):
            padded = str(i).zfill(pad)
            if padded not in existing_nums:
                missing.append(padded)

        if not missing:
            continue

        _logger.log(f"  {set_code}: checking {len(missing)} gaps in range 1..{max_num}...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                    executor.submit(
                        _process_standard_card,
                        (set_code, num, collection, card_type_id, product_format_id, en_lang_id, jp_lang_id,
                         image_file_type_id, files_path, img_path_pattern, card_type,
                         api_base, existing_nums, existing_images_by_product, stats)
                    ): num
                for num in missing
            }
            for future in concurrent.futures.as_completed(futures):
                msg = future.result()
                if msg:
                    _logger.log(msg)

        filled = sum(1 for num in missing if num in existing_nums)
        if filled:
            _logger.log(f"  {set_code}: +{filled} gaps filled")

    _logger.log(f"\n{SEP}")
    _logger.log(f"  New standard:       {stats['new_standard']}")
    _logger.log(f"  Existing standard:  {stats['existing_standard']}")
    _logger.log(f"  Alt arts created:   {stats['alt_arts_created']}")
    _logger.log(f"  JP images added:    {stats['jp_images_added']}")
    _logger.log(f"  Images downloaded:  {stats['images_downloaded']}")
    _logger.log(f"  Images skipped:     {stats['images_skipped']}")
    _logger.log(f"  Errors:             {stats['errors']}")
    _logger.log(SEP)

    total_new = stats["new_standard"] + stats["alt_arts_created"]
    if total_new > 0:
        _logger.log(f"\n  {total_new} new products created with images.")
        _logger.log(f"  Run sync_digimon_products.py to add translations.\n")

    finalize_log(_logger, "find_digimon_products", _API_ROOT, api_request)


if __name__ == "__main__":
    try:
        sync()
    except KeyboardInterrupt:
        print("\n  Interrupted")
        sys.exit(1)
