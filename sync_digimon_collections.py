from datetime import datetime, timezone
from dotenv import load_dotenv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


load_dotenv()
API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

PARAM_KEY_API_BASE = "sync.digimon.collections.api.base"
PARAM_KEY_CAR_TYPE = "sync.digimon.collections.card.type"
PARAM_KEY_MIG_LANG = "sync.digimon.collections.migration.languages"
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
        with urllib.request.urlopen(req, timeout=30) as resp:
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


def api_get(path, params=None):
    if params:
        path = f"{path}?{urllib.parse.urlencode(params)}"
    return api_request("GET", path)


def api_get_all(path):
    page = 1
    items = []

    while True:
        data = api_get(path, {"page": page, "per_page": 100})
        if not data:
            return items
        items.extend(data.get("items", []))
        pagination = data.get("pagination", {})
        if not pagination.get("has_next"):
            return items
        page += 1


def _match_set_name(set_names, code):
    norm = re.match(r"^([A-Za-z]+)(\d+)$", code)
    norm_code = norm.group(1).upper() + norm.group(2).zfill(2) if norm else None
    for entry in set_names:
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)", entry)
        if m:
            entry_code = m.group(1).replace("-", "").upper()
            if norm_code and entry_code == norm_code:
                return m.group(2)
    for entry in set_names:
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)", entry)
        if m:
            entry_code = m.group(1).replace("-", "").upper()
            if entry_code.startswith(code.upper()):
                return m.group(2)
    m = re.match(r"^[A-Za-z0-9_-]+:\s*(.*)", set_names[0]) if set_names else None
    return m.group(1) if m else (set_names[0] if set_names else code)


def _normalize_name(name):
    s = name.strip().lower()
    words = s.split()
    minor = {"of", "the", "and", "a", "an", "in", "on", "at", "to", "for", "by"}
    result = []
    for i, w in enumerate(words):
        if i == 0 or i == len(words) - 1 or w not in minor:
            result.append(w.capitalize())
        else:
            result.append(w)
    return " ".join(result)


def _sample_cardnumbers(cardnumbers):
    n = len(cardnumbers)
    if n <= 5:
        return cardnumbers[:]
    indices = {0, n // 4, n // 2, 3 * n // 4, n - 1}
    return [cardnumbers[i] for i in sorted(indices)]


def get_digimon_sets(api_base):
    cards = fetch_json(f"{api_base.rstrip('/')}/getAllCards")
    if not cards:
        return []

    groups = {}
    for card in cards:
        cn = card.get("cardnumber", "")
        m = re.match(r"^([A-Za-z0-9_-]+)-", cn)
        prefix = m.group(1) if m else (cn if cn else None)
        if prefix:
            groups.setdefault(prefix, []).append(cn)

    result = []
    for prefix in sorted(groups):
        cardnumbers = sorted(groups[prefix])
        sampled = None
        for cn in _sample_cardnumbers(cardnumbers):
            time.sleep(0.1)
            rep = fetch_json(
                f"{api_base.rstrip('/')}/search?card={urllib.parse.quote(cn)}"
            )
            if rep and isinstance(rep, list) and len(rep) > 0:
                card = rep[0]
                set_names = card.get("set_name") or []
                name = _normalize_name(_match_set_name(set_names, prefix))
                if name and name != prefix:
                    sampled = {
                        "set_code": prefix,
                        "set_name": name,
                        "release_date": (card.get("date_added") or "")[:10] or None
                    }
                    break
        if not sampled:
            rep2 = fetch_json(
                f"{api_base.rstrip('/')}/search?card={urllib.parse.quote(cardnumbers[0])}"
            )
            if rep2 and isinstance(rep2, list) and len(rep2) > 0:
                card = rep2[0]
                set_names = card.get("set_name") or []
                m = re.match(r"^[A-Za-z0-9_-]+:\s*(.*)", set_names[0]) if set_names else None
                name = _normalize_name(m.group(1) if m else (set_names[0] if set_names else prefix))
                sampled = {
                    "set_code": prefix,
                    "set_name": name,
                    "release_date": (card.get("date_added") or "")[:10] or None
                }
            else:
                sampled = {"set_code": prefix, "set_name": prefix, "release_date": None}
        result.append(sampled)
    return result


def get_param(settings_by_key, param_key):
    param = settings_by_key.get(param_key)
    if param is None:
        raise RuntimeError(f"setting '{param_key}' not found")
    print(f"  {param_key}: {param}")
    return param


def parse_migration_languages(migration_languages):
    api_lang = []
    db_lang = {}

    for item in migration_languages.strip(";").split(";"):
        if not item:
            continue
        lang, country = item.split("-")
        api_lang.append(lang)
        db_lang[lang] = country

    return api_lang, db_lang


def get_card_type_id(types, card_type):
    for item in types:
        if item.get("type") == "card" and item.get("short_name") == card_type:
            return item["id"]
    raise RuntimeError(f"card_type '{card_type}' not found")


def get_existing_collections(collections, card_type_id):
    by_code = {}
    for item in collections:
        card_type = item.get("card_type") or {}
        if card_type.get("id") == card_type_id:
            by_code[item["code"]] = item
    return by_code


def get_existing_translations(translations, collection_ids):
    by_collection_lang = {}
    for item in translations:
        collection_id = item.get("collection_id")
        language_id = item.get("language_id")
        if collection_id in collection_ids and language_id:
            by_collection_lang[(collection_id, language_id)] = item
    return by_collection_lang


def create_collection(card_type_id, set_id, release_date):
    return api_request("POST", "collections", {
        "card_type_id": card_type_id,
        "code": set_id,
        "release_date": release_date or None,
        "is_manual": False
    })


def upsert_translation(existing_translation, collection_id, lang_id, name):
    payload = {
        "collection_id": collection_id,
        "language_id": lang_id,
        "name": name
    }
    if existing_translation:
        return api_request("PATCH", f"collection-translations/{existing_translation['id']}", payload), False
    return api_request("POST", "collection-translations", payload), True


def sync():
    print(f"\n{SEP}")
    print("  Searching Digimon TCG sets via CardVault API")
    print(SEP)
    print(f"  API: {API_BASE}")

    print("\n  Getting params...")
    settings = api_get_all("settings")
    settings_by_key = {item["setting_key"]: item.get("setting_value") for item in settings}
    api_base = get_param(settings_by_key, PARAM_KEY_API_BASE)
    card_type = get_param(settings_by_key, PARAM_KEY_CAR_TYPE)
    migration_languages = get_param(settings_by_key, PARAM_KEY_MIG_LANG)
    api_lang, db_lang = parse_migration_languages(migration_languages)

    print("\n  Getting local API data...")
    types = api_get_all("types")
    languages = api_get_all("languages")
    collections = api_get_all("collections")
    translations = api_get_all("collection-translations")

    lang_ids = {item["abbreviation"]: item["id"] for item in languages}
    card_type_id = get_card_type_id(types, card_type)
    collections_by_code = get_existing_collections(collections, card_type_id)
    collection_ids = {item["id"] for item in collections_by_code.values()}
    translations_by_collection_lang = get_existing_translations(translations, collection_ids)

    print("\n  Getting sets from Digimon TCG API...")
    print("  Fetching all cards to extract set list...")
    all_sets = get_digimon_sets(api_base)
    print(f"  {len(all_sets)} sets found\n")

    if not all_sets:
        sys.exit(1)

    stats = {"new_cols": 0, "existing_cols": 0, "new_trans": 0, "updated_trans": 0}

    for i, item in enumerate(all_sets):
        set_id = item["set_code"]
        en_name = item.get("set_name", "")
        print(f"  [{i + 1:>4}/{len(all_sets)}] {set_id:<12} {en_name:<40}", end="", flush=True)

        collection = collections_by_code.get(set_id)
        if collection:
            stats["existing_cols"] += 1
            tag = "Exists"
            is_new_collection = False
        else:
            collection = create_collection(card_type_id, set_id, item.get("release_date"))
            collections_by_code[set_id] = collection
            stats["new_cols"] += 1
            tag = "New"
            is_new_collection = True

        translations_added = []
        collection_id = collection["id"]

        for lang in api_lang:
            db_abr = db_lang.get(lang)
            if not db_abr:
                continue
            lang_id = lang_ids.get(db_abr)
            if not lang_id:
                continue
            name = en_name if lang == "en" else en_name
            if not name:
                continue

            existing_translation = translations_by_collection_lang.get((collection_id, lang_id))
            if existing_translation and not is_new_collection:
                continue

            translation, is_new = upsert_translation(existing_translation, collection_id, lang_id, name)
            translations_by_collection_lang[(collection_id, lang_id)] = translation
            translations_added.append(lang)
            if is_new:
                stats["new_trans"] += 1
            else:
                stats["updated_trans"] += 1

        print(f" [{tag}] trans({','.join(translations_added) or '-'})")

    print(f"\n{SEP}")
    print(f"  New cols:       {stats['new_cols']}")
    print(f"  Existing cols:  {stats['existing_cols']}")
    print(f"  New trans:      {stats['new_trans']}")
    print(f"  Updated trans:  {stats['updated_trans']}")
    print(SEP + "\n")


if __name__ == "__main__":
    sync()
