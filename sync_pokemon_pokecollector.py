import asyncio
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from task_logger import TaskLogger, finalize_log

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Faltan dependencias. Ejecuta:\n  pip install playwright\n  playwright install chromium")
    sys.exit(1)

load_dotenv()

HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "").lower() in ("1", "true", "yes")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_API_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "CardVault-API"))

_logger: TaskLogger | None = None

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

SETTING_CARD_TYPE = "sync.pokemon.products.card.type"
SETTING_IMG_PATH = "sync.pokemon.products.img.path"
SETTING_IMG_PATH_PATTERN = "sync.pokemon.products.img.path.pattern"
SETTING_LOG_PATH = "tasks.log.path"

DELAY_MIN = 2.0
DELAY_MAX = 5.0
SEP = "=" * 58

_token: str | None = None
_token_expires_at: datetime | None = None


def _login() -> bool:
    global _token, _token_expires_at
    if not API_USERNAME or not API_PASSWORD:
        return False
    try:
        body = json.dumps({"username": API_USERNAME, "password": API_PASSWORD}).encode("utf-8")
        req = urllib.request.Request(
            f"{API_BASE.rstrip('/')}/auth/login",
            data=body,
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


def api_request(method, path, data=None):
    clean_path = path.strip("/")
    if "?" in clean_path:
        clean_path, qs = clean_path.split("?", 1)
        url = f"{API_BASE.rstrip('/')}/{clean_path}/?{qs}"
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
        err_body = e.read().decode("utf-8", errors="replace")
        if e.code == 401:
            if _login():
                token = _get_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                req = urllib.request.Request(url, data=body, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else None
        (_logger or print)(f"    API {method} {path} -> HTTP {e.code}: {err_body[:300]}")
        return None
    except Exception as ex:
        (_logger or print)(f"    API {method} {path} -> ERROR: {ex}")
        return None


def api_get(path, params=None):
    if params:
        path = f"{path}?{urllib.parse.urlencode(params)}"
    return api_request("GET", path)


def api_get_all(path, params=None):
    page = 1
    items = []
    merged = {**(params or {}), "page": page, "per_page": 100}
    while True:
        data = api_get(path, merged)
        if not data:
            return items
        items.extend(data.get("items", []))
        pagination = data.get("pagination", {})
        if not pagination.get("has_next"):
            return items
        page += 1
        merged["page"] = page


def find_browser_profile():
    import platform
    system = platform.system()
    if system == "Windows":
        candidates = [
            (os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data"),
             r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
            (os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"), None),
        ]
    elif system == "Darwin":
        candidates = [
            (os.path.expanduser("~/Library/Application Support/BraveSoftware/Brave-Browser"),
             "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
            (os.path.expanduser("~/Library/Application Support/Google/Chrome"), None),
        ]
    else:
        candidates = [
            (os.path.expanduser("~/.config/BraveSoftware/Brave-Browser"),
             "/usr/bin/brave-browser"),
            (os.path.expanduser("~/.config/google-chrome"), None),
        ]
    for profile_dir, exe in candidates:
        if os.path.isdir(profile_dir):
            if exe and not os.path.isfile(exe):
                exe = None
            return profile_dir, exe
    return None, None


def download_file(url):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None


async def human_scroll(page):
    import random
    steps = random.randint(2, 4)
    for _ in range(steps):
        dist = random.randint(100, 300)
        await page.mouse.wheel(0, dist)
        await page.wait_for_timeout(random.randint(300, 700))


async def scrape_collection_cards(page, url):
    """Scrape a PokeCollector collection page and return list of card dicts."""
    print(f"  Navigando a {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"  Error de navegacion: {e}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e2:
            print(f"  Error fatal: {e2}")
            return None
    await page.wait_for_timeout(2000)

    body_text = await page.locator("body").inner_text()
    if any(kw in body_text.lower() for kw in ["just a moment", "checking your browser"]):
        print("  Cloudflare detectado!")
        return None

    html = await page.content()

    links = re.findall(r'<a\s+href="(/[^"]+)"', html)
    # Filter to only card-like links (ending with -N)
    links = [l for l in links if re.search(r'-\d+$', l)]

    cards = []
    seen = set()
    for href in links:
        m = re.search(r'-(\d+)$', href)
        if not m:
            continue
        card_num = m.group(1)
        if card_num in seen:
            continue
        seen.add(card_num)

        full_url = f"https://jp.pokellector.com{href}"
        # Derive English name from URL: /expansion/Card-Name-Card-N -> "Card Name"
        name_match = re.search(r'/([^/]+?)-Card-\d+$', href)
        if not name_match:
            parts = href.rstrip('/').split('/')
            last_seg = parts[-1] if parts else ""
            name_match = re.match(r'^(.+?)-[A-Za-z]+-\d+$', last_seg) if last_seg else None
        en_name = name_match.group(1).replace("-", " ").title() if name_match else ""

        cards.append({
            "number": card_num,
            "url": full_url,
            "href": href,
            "en_name_guess": en_name,
        })

    return cards


async def scrape_card_page(page, url):
    """Scrape a single card page. Returns (en_name, jp_name, image_url) or None."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"    Error navegacion: {e}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            return None
    await page.wait_for_timeout(2000)
    await human_scroll(page)
    await page.wait_for_timeout(500)

    body_text = await page.locator("body").inner_text()
    if any(kw in body_text.lower() for kw in ["just a moment", "checking your browser"]):
        print("    Cloudflare!")
        return None

    title = await page.title()
    html = await page.content()

    en_name = None
    title_match = re.search(r'^(.+?)\s*-\s*[\w\s]+\s+#\d+\s+Pokemon Card', title)
    if title_match:
        en_name = title_match.group(1).strip()
    if not en_name:
        h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
        if h1_match:
            h1_text = re.sub(r'<[^>]+>', '', h1_match.group(1)).strip()
            h1_text = h1_text.split(" #")[0].strip()
            if h1_text:
                en_name = h1_text
    if not en_name:
        return None

    jp_name = None
    jpn_match = re.search(r'JPN:</strong>\s*<a[^>]*>([^<]+)</a>', html)
    if jpn_match:
        jp_name = jpn_match.group(1).strip()

    image_url = None
    for m in re.finditer(r'<img[^>]*src="(https://den-cards\.pokellector\.com/\d+/[^"]+\.(?:png|jpg))"', html):
        url = m.group(1)
        if ".thumb." not in url:
            image_url = url
            break

    return {
        "en_name": en_name,
        "jp_name": jp_name,
        "image_url": image_url,
        "title": title,
    }


async def main():
    global _logger

    if not API_BASE or not API_USERNAME or not API_PASSWORD:
        print("Faltan CARDVAULT_API_* env vars")
        sys.exit(1)

    if not _login():
        print("Login fallido")
        sys.exit(1)
    print("Login OK\n")

    print("Obteniendo settings...")
    settings_list = api_get_all("settings")
    settings_by_key = {item["setting_key"]: item.get("setting_value") for item in settings_list}
    card_type_short = settings_by_key.get(SETTING_CARD_TYPE)
    if not card_type_short:
        print(f"  ERROR: setting '{SETTING_CARD_TYPE}' no encontrada")
        sys.exit(1)
    print(f"  Card type: {card_type_short}")

    files_path = settings_by_key.get(SETTING_IMG_PATH)
    img_path_pattern = settings_by_key.get(SETTING_IMG_PATH_PATTERN, "{card_type}/{is_manual}/{collection_code}")
    if files_path:
        print(f"  Files path: {files_path}")
    else:
        files_path = "./../.files/products_images"
        print(f"  Files path (default): {files_path}")

    print(f"  Img path pattern: {img_path_pattern}")
    log_path_setting = settings_by_key.get(SETTING_LOG_PATH, "./logs")
    log_dir = log_path_setting if os.path.isabs(log_path_setting) else os.path.join(_API_ROOT, log_path_setting)
    _logger = TaskLogger(log_dir, "pokemon_pokecollector")
    _logger.log(f"  Log path: {log_dir}")
    _logger.log(f"  Card type: {card_type_short}")
    _logger.log(f"  Files path: {files_path}")
    _logger.log(f"  Img path pattern: {img_path_pattern}")

    _logger.log("\nObteniendo tipos...")
    types_list = api_get_all("types")
    card_type_id = None
    image_file_type_id = None
    for t in types_list:
        if t.get("type") == "card" and t.get("short_name") == card_type_short:
            card_type_id = t["id"]
        if t.get("type") == "file" and t.get("name") == "image":
            image_file_type_id = t["id"]
    if not card_type_id:
        _logger.log(f"  ERROR: card type '{card_type_short}' no encontrado en types")
        finalize_log(_logger, "pokemon_pokecollector", _API_ROOT, api_request)
        sys.exit(1)
    if not image_file_type_id:
        _logger.log("  ERROR: file type 'image' no encontrado")
        finalize_log(_logger, "pokemon_pokecollector", _API_ROOT, api_request)
        sys.exit(1)
    _logger.log(f"  Card type ID: {card_type_id}")
    _logger.log(f"  Image file type ID: {image_file_type_id}")

    product_format_id = None
    for t in types_list:
        if t.get("type") == "product_format" and t.get("name") == "carta":
            product_format_id = t["id"]
            break
    if not product_format_id:
        _logger.log("  ERROR: product format 'carta' not found in types")
        finalize_log(_logger, "pokemon_pokecollector", _API_ROOT, api_request)
        sys.exit(1)
    _logger.log(f"  Product format ID (carta): {product_format_id}")

    _logger.log("\nObteniendo idiomas...")
    languages_list = api_get_all("languages")
    lang_en_id = None
    lang_ja_id = None
    for l in languages_list:
        if l.get("name") == "Inglés":
            lang_en_id = l["id"]
        if l.get("name") == "Japonés":
            lang_ja_id = l["id"]
    _logger.log(f"  English ID: {lang_en_id}")
    _logger.log(f"  Japanese ID: {lang_ja_id}")

    _logger.log(f"\nObteniendo colecciones con force_url y force_download...")
    collections = api_get_all("collections", {"force_download": 1, "per_page": 200})
    target_collections = [c for c in collections if c.get("force_url")]
    _logger.log(f"  {len(target_collections)} colecciones para procesar\n")

    if not target_collections:
        _logger.log("No hay colecciones pendientes. Asigna force_url y force_download=true a una coleccion.\n")
        finalize_log(_logger, "pokemon_pokecollector", _API_ROOT, api_request)
        return

    profile_dir, exe_path = find_browser_profile()

    async with async_playwright() as pw:
        if profile_dir:
            browser_name = "Brave" if "Brave" in profile_dir or (exe_path and "brave" in exe_path.lower()) else "Chrome"
            print(f"  Usando perfil real de {browser_name}: {profile_dir}")
            print(f"  Asegurate de tener {browser_name} CERRADO.\n")
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=HEADLESS,
                slow_mo=80,
                args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
                no_viewport=True,
                locale="es-ES",
                executable_path=exe_path if exe_path else None,
            )
            page = await context.new_page()
        else:
            print("  Usando Chromium headless.\n")
            browser = await pw.chromium.launch(
                headless=HEADLESS,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = await browser.new_context(
                viewport={"width": 1366, "height": 768},
                locale="es-ES",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        stats = {"collections": 0, "cards": 0, "created": 0, "existed": 0, "trans_ok": 0, "img_ok": 0, "img_fail": 0, "errors": 0}

        for col in target_collections:
            col_id = col["id"]
            col_code = col["code"]
            col_name = col["name"]
            col_url = col["force_url"]
            stats["collections"] += 1

            _logger.log(f"\n{'='*70}")
            _logger.log(f"  Coleccion: {col_name} ({col_code})")
            _logger.log(f"  URL: {col_url}")
            _logger.log(f"{'='*70}")

            cards_data = await scrape_collection_cards(page, col_url)
            if cards_data is None:
                _logger.log("  Error obteniendo lista de cartas, saltando coleccion")
                stats["errors"] += 1
                continue

            _logger.log(f"  Cartas encontradas: {len(cards_data)}")

            _logger.log("  Pre-calentando API (evita cold-start)...")
            api_get("product-catalog", {"per_page": 1, "collection_code": col_code})

            total = len(cards_data)
            for idx, card_info in enumerate(cards_data, 1):
                card_num = card_info["number"]
                en_name_guess = card_info["en_name_guess"]
                _logger.log(f"\n  [{idx}/{total}] Card #{card_num} ({en_name_guess})")
                _logger.log(f"    URL: {card_info['url']}")

                # Check if product already exists — evita abrir el navegador
                try:
                    existing = api_get("product-catalog", {
                        "collection_code": col_code,
                        "product_number": card_num,
                        "product_type_id": card_type_id,
                        "per_page": 500
                    })
                    existing_items = (existing or {}).get("items", [])
                except Exception:
                    existing_items = []

                # Filtro exacto: product_number LIKE devuelve falsos (#1 encuentra #10)
                # Solo saltar si ya existe un producto MANUAL con ese numero
                def _is_manual(item):
                    val = item.get("is_manual")
                    return val is True or val == 1 or val == "1" or val == "true" or val == "True"

                existing_items = [
                    item for item in existing_items
                    if item.get("product_number") == card_num
                    and _is_manual(item)
                ]

                if existing_items:
                    stats["existed"] += 1
                    product_id = existing_items[0]["product_id"]
                    _logger.log(f"    Producto existente: id={product_id}, saltando")
                    continue

                # Solo abrimos la web si es producto nuevo
                card_scrape = await scrape_card_page(page, card_info["url"])
                if card_scrape is None:
                    _logger.log("    Error scaneando carta, saltando")
                    stats["errors"] += 1
                    continue

                en_name = card_scrape["en_name"]
                jp_name = card_scrape["jp_name"]
                image_url = card_scrape["image_url"]
                _logger.log(f"    EN: {en_name}")
                _logger.log(f"    JP: {jp_name or '—'}")
                _logger.log(f"    Img: {image_url or '—'}")

                stats["cards"] += 1

                created = False
                for attempt in range(3):
                    try:
                        result = api_request("POST", "products", {
                            "collection_id": col_id,
                            "product_type_id": card_type_id,
                            "product_format_id": product_format_id,
                            "product_number": card_num,
                            "force_download": True,
                            "is_manual": True,
                        })
                        if result and result.get("id"):
                            product_id = result["id"]
                            stats["created"] += 1
                            _logger.log(f"    Producto creado: id={product_id}")
                            created = True
                            break
                        _logger.log(f"    Error creando producto (intento {attempt + 1})")
                    except Exception as e:
                        _logger.log(f"    Error creando producto: {e} (intento {attempt + 1})")
                    if attempt < 2:
                        await asyncio.sleep(1)
                if not created:
                    stats["errors"] += 1
                    continue

                if jp_name:
                    try:
                        existing_trans = api_get("product-translations", {
                            "product_id": product_id,
                            "language_id": lang_ja_id,
                            "per_page": 1
                        })
                        existing_trans_items = (existing_trans or {}).get("items", [])
                        if existing_trans_items:
                            api_request("PATCH", f"product-translations/{existing_trans_items[0]['id']}", {
                                "name": jp_name
                            })
                        else:
                            api_request("POST", "product-translations", {
                                "product_id": product_id,
                                "language_id": lang_ja_id,
                                "name": jp_name
                            })
                        stats["trans_ok"] += 1
                    except Exception as e:
                        _logger.log(f"    Error creando traduccion JP: {e}")

                if en_name:
                    try:
                        existing_en = api_get("product-translations", {
                            "product_id": product_id,
                            "language_id": lang_en_id,
                            "per_page": 1
                        })
                        existing_en_items = (existing_en or {}).get("items", [])
                        if not existing_en_items:
                            api_request("POST", "product-translations", {
                                "product_id": product_id,
                                "language_id": lang_en_id,
                                "name": en_name
                            })
                    except Exception:
                        pass

                if image_url:
                    image_data = download_file(image_url)
                    if image_data:
                        ext = os.path.splitext(image_url.split("/")[-1])[1] or ".png"
                        original_name = image_url.split("/")[-1]
                        stored_name = f"{col_code}_{card_num}_ja{ext}"
                        is_manual_val = "1"
                        pattern = (img_path_pattern or "").replace("collection_code}", "{collection_code}")
                        sub_dir = pattern.replace("{card_type}", card_type_short).replace("{is_manual}", is_manual_val).replace("{collection_code}", col_code) if pattern else f"{card_type_short}/{is_manual_val}/{col_code}"
                        base_dir = files_path if os.path.isabs(files_path) else os.path.join(_API_ROOT, files_path)
                        target_dir = os.path.join(base_dir, sub_dir)
                        os.makedirs(target_dir, exist_ok=True)

                        local_rel = os.path.join(files_path, sub_dir, stored_name)
                        local_abs = os.path.join(base_dir, sub_dir, stored_name)
                        with open(local_abs, "wb") as f:
                            f.write(image_data)
                        file_size = len(image_data)

                        try:
                            api_request("POST", "files", {
                                "product_id": product_id,
                                "language_id": lang_ja_id,
                                "file_type_id": image_file_type_id,
                                "original_name": original_name,
                                "stored_name": stored_name,
                                "file_path": local_rel,
                                "file_size": file_size,
                            })
                            stats["img_ok"] += 1
                            _logger.log(f"    Imagen guardada: {stored_name} ({file_size} bytes)")
                        except Exception as e:
                            stats["img_fail"] += 1
                            _logger.log(f"    Error guardando imagen: {e}")
                    else:
                        stats["img_fail"] += 1
                        _logger.log(f"    Error descargando imagen")

                api_request("PATCH", f"products/{product_id}", {"force_download": False})

                if idx < total:
                    delay = random.uniform(DELAY_MIN, DELAY_MAX)
                    _logger.log(f"    Pausa {delay:.1f} s...")
                    await asyncio.sleep(delay)

            api_request("PATCH", f"collections/{col_id}", {"force_download": False})
            _logger.log(f"\n  Coleccion {col_name} marcada como descargada\n")

        # ── Post-processing: fix missing images ──
        _logger.log(f"\n{'='*70}")
        _logger.log(f"  Fixing products missing images in pokecollector collections...")
        _logger.log(f"{'='*70}")

        fix_stats = {"checked": 0, "img_ok": 0, "img_fail": 0}

        all_cols = api_get_all("collections", {"per_page": 500})
        cols_with_url = [c for c in all_cols if c.get("force_url")]

        for col in cols_with_url:
            col_code = col["code"]
            col_name = col["name"]
            col_url = col["force_url"]

            products = api_get_all("product-catalog", {
                "collection_code": col_code,
                "product_type_id": card_type_id,
                "per_page": 200
            })
            if not products:
                continue

            missing = []
            for p in products:
                pid = p.get("product_id") or p.get("id")
                if not pid:
                    continue
                files = api_get("files", {"product_id": pid, "per_page": 1})
                has = files and isinstance(files, dict) and files.get("items") and len(files["items"]) > 0
                if not has:
                    missing.append(p)

            if not missing:
                continue

            fix_stats["checked"] += len(missing)
            _logger.log(f"\n  {col_name} ({col_code}): {len(missing)} products missing images")

            cards_data = await scrape_collection_cards(page, col_url)
            if not cards_data:
                _logger.log(f"    Error scraping collection page, skipping")
                continue

            card_map = {c["number"]: c for c in cards_data}

            for p in missing:
                num = p.get("product_number")
                pid = p.get("product_id") or p.get("id")
                if num not in card_map:
                    _logger.log(f"    Card #{num}: URL not found, skipping")
                    continue

                card_info = card_map[num]
                _logger.log(f"\n    [#{num}] {card_info['en_name_guess']}")
                _logger.log(f"    URL: {card_info['url']}")

                card_scrape = await scrape_card_page(page, card_info["url"])
                if not card_scrape or not card_scrape["image_url"]:
                    _logger.log(f"    No image URL found")
                    continue

                image_url = card_scrape["image_url"]
                _logger.log(f"    Img: {image_url}")

                image_data = download_file(image_url)
                if not image_data:
                    fix_stats["img_fail"] += 1
                    _logger.log(f"    Error downloading image")
                    continue

                ext = os.path.splitext(image_url.split("/")[-1])[1] or ".png"
                original_name = image_url.split("/")[-1]
                stored_name = f"{col_code}_{num}_ja{ext}"
                is_manual_val = "1"
                pattern = (img_path_pattern or "").replace("collection_code}", "{collection_code}")
                sub_dir = pattern.replace("{card_type}", card_type_short).replace("{is_manual}", is_manual_val).replace("{collection_code}", col_code) if pattern else f"{card_type_short}/{is_manual_val}/{col_code}"
                base_dir = files_path if os.path.isabs(files_path) else os.path.join(_API_ROOT, files_path)
                target_dir = os.path.join(base_dir, sub_dir)
                os.makedirs(target_dir, exist_ok=True)

                local_rel = os.path.join(files_path, sub_dir, stored_name)
                local_abs = os.path.join(base_dir, sub_dir, stored_name)
                with open(local_abs, "wb") as f:
                    f.write(image_data)
                file_size = len(image_data)

                try:
                    api_request("POST", "files", {
                        "product_id": pid,
                        "language_id": lang_ja_id,
                        "file_type_id": image_file_type_id,
                        "original_name": original_name,
                        "stored_name": stored_name,
                        "file_path": local_rel,
                        "file_size": file_size,
                    })
                    fix_stats["img_ok"] += 1
                    _logger.log(f"    Image saved: {stored_name} ({file_size} bytes)")
                except Exception as e:
                    fix_stats["img_fail"] += 1
                    _logger.log(f"    Error saving image: {e}")

                await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

        if fix_stats["checked"]:
            _logger.log(f"\n  Missing images fix:")
            _logger.log(f"    Products checked: {fix_stats['checked']}")
            _logger.log(f"    Images downloaded: {fix_stats['img_ok']}")
            if fix_stats["img_fail"]:
                _logger.log(f"    Images failed: {fix_stats['img_fail']}")

        await context.close()

    _logger.log(f"\n  {SEP}")
    _logger.log(f"  Colecciones:     {stats['collections']}")
    _logger.log(f"  Cartas totales:  {stats['cards']}")
    _logger.log(f"  Creadas:         {stats['created']}")
    _logger.log(f"  Existentes:      {stats['existed']}")
    _logger.log(f"  Traducciones JP: {stats['trans_ok']}")
    _logger.log(f"  Imagenes OK:     {stats['img_ok']}")
    if stats["img_fail"]:
        _logger.log(f"  Imagenes FAIL:   {stats['img_fail']}")
    if stats["errors"]:
        _logger.log(f"  Errores:         {stats['errors']}")
    _logger.log(f"  {SEP}\n")
    finalize_log(_logger, "pokemon_pokecollector", _API_ROOT, api_request)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Interrumpido")
        sys.exit(1)
