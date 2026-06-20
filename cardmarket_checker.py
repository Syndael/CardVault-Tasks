import sys
import re
import asyncio
import random
import json
import os
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from task_logger import TaskLogger, finalize_log

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Faltan dependencias. Ejecuta:\n  pip install playwright\n  playwright install chromium")
    sys.exit(1)

load_dotenv()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_API_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "CardVault-API"))

_logger: TaskLogger | None = None

HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "").lower() in ("1", "true", "yes")

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

_token = None
_token_expires_at = None

DELAY_MIN = 10.0
DELAY_MAX = 18.0
WAIT_LOAD_MS = 3000
CLOUDFLARE_WAIT = 15000
SETTING_KEY_SKIP_THRESHOLD = "cardmarket.checker.price.skip"
SETTING_KEY_PRICE_MINUTES = "cardmarket.checker.price.minutes"
SETTING_KEY_WISHLIST_MINUTES = "cardmarket.checker.wishlist.minutes"
SETTING_LOG_PATH = "tasks.log.path"


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


def api_post(path, data):
    return api_request("POST", path, data)


def api_get_all(path, params=None):
    page = 1
    items = []
    per_page = (params or {}).get("per_page", 100)
    merged = {**(params or {}), "page": page, "per_page": per_page}
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


def price_to_float(price_str):
    if not price_str:
        return None
    clean = price_str.replace("€", "").replace(" ", "").replace(",", ".")
    try:
        return float(clean)
    except ValueError:
        return None


def _strip_thousands(s):
    s = re.sub(r'\.(\d{3})', r'\1', s)
    s = re.sub(r',(\d{3})', r'\1', s)
    return s


def normalizar_precio(price_str):
    s = _strip_thousands(price_str.strip())
    m = re.search(r"(\d+)[.,](\d{2})", s)
    if m:
        return f"{m.group(1)},{m.group(2)} €"
    return s


def extract_price_from_html(html):
    match = re.search(r'(?:Desde|From|A partir de)[^\d]*(\d+[.,]\d{2})\s*€', html, re.IGNORECASE)
    if match:
        return _strip_thousands(match.group(1)) + " €"
    match = re.search(r'itemprop=["\']price["\'][^>]*content=["\']([^"\']+)["\']', html)
    if match:
        return _strip_thousands(match.group(1).strip()) + " €"
    match = re.search(r'(\d+[.,]\d{2})\s*€', html)
    if match:
        return _strip_thousands(match.group(1)) + " €"
    return None


async def human_scroll(page):
    steps = random.randint(3, 6)
    for _ in range(steps):
        dist = random.randint(120, 400)
        await page.mouse.wheel(0, dist)
        await page.wait_for_timeout(random.randint(300, 900))


async def human_mouse_move(page):
    for _ in range(random.randint(2, 4)):
        x = random.randint(200, 1100)
        y = random.randint(150, 600)
        await page.mouse.move(x, y, steps=random.randint(5, 15))
        await page.wait_for_timeout(random.randint(200, 600))


async def scrape_price(page, url):
    try:
        await page.goto(url, wait_until="networkidle", timeout=40000)
    except Exception as e:
        print(f"  Error de navegacion: {e}")
        return None
    await page.wait_for_timeout(random.randint(1500, WAIT_LOAD_MS))
    await human_mouse_move(page)
    await human_scroll(page)
    await page.wait_for_timeout(random.randint(800, 2000))
    title = await page.title()
    body_text = await page.locator("body").inner_text()
    cf_keywords = ["just a moment", "checking your browser", "enable javascript and cookies"]
    if any(kw in title.lower() for kw in cf_keywords) or any(kw in body_text.lower() for kw in cf_keywords):
        print(f"  Cloudflare detectado. Esperando {CLOUDFLARE_WAIT // 1000} s...")
        await page.wait_for_timeout(CLOUDFLARE_WAIT)
        title2 = await page.title()
        if any(kw in title2.lower() for kw in cf_keywords):
            print("  Aun bloqueado. Resuelvelo manualmente y pulsa ENTER...")
            input()
            await page.wait_for_timeout(2000)
    price = None
    try:
        dts = await page.locator("dl dt").all()
        for dt in dts:
            txt = (await dt.inner_text()).strip().lower()
            if txt in ("desde", "from", "a partir de"):
                dd = dt.locator("~ dd")
                if await dd.count() > 0:
                    price = (await dd.first.inner_text()).strip()
                    break
    except Exception:
        pass
    if not price:
        try:
            loc = page.locator("div.price-container").first
            if await loc.count() > 0:
                price = (await loc.inner_text()).strip()
        except Exception:
            pass
    if not price:
        try:
            spans = await page.locator("span[class*='price']").all()
            for span in spans:
                txt = (await span.inner_text()).strip()
                if "€" in txt and re.search(r"\d", txt):
                    price = txt
                    break
        except Exception:
            pass
    if not price:
        try:
            loc = page.locator("[itemprop='price']").first
            if await loc.count() > 0:
                price = (await loc.get_attribute("content")) or (await loc.inner_text()).strip()
        except Exception:
            pass
    if not price:
        html = await page.content()
        price = extract_price_from_html(html)
    if price:
        price = normalizar_precio(price)
    return price


def find_browser_profile():
    import platform
    system = platform.system()
    if system == "Windows":
        candidates = [
            (os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data"),
             r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
            (os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"), None),
            (os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome Beta\User Data"), None),
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


def main():
    global _logger

    if not API_BASE or not API_USERNAME or not API_PASSWORD:
        print("Faltan CARDVAULT_API_* env vars")
        sys.exit(1)

    if not _login():
        print("Login fallido")
        sys.exit(1)
    print("Login OK")

    print("Obteniendo settings...")
    settings_list = api_get_all("settings")
    settings_by_key = {item["setting_key"]: item.get("setting_value") for item in settings_list}
    skip_threshold_str = settings_by_key.get(SETTING_KEY_SKIP_THRESHOLD)
    skip_threshold = float(skip_threshold_str) if skip_threshold_str else None
    if skip_threshold is not None:
        print(f"  Umbral de salto: {skip_threshold:.2f} €")

    price_minutes_str = settings_by_key.get(SETTING_KEY_PRICE_MINUTES)
    if price_minutes_str:
        try:
            PRICE_MINUTES_THRESHOLD = int(price_minutes_str)
        except ValueError:
            PRICE_MINUTES_THRESHOLD = 10080
    else:
        PRICE_MINUTES_THRESHOLD = 10080
    print(f"  Minutos umbral de precio: {PRICE_MINUTES_THRESHOLD}")

    log_path_setting = settings_by_key.get(SETTING_LOG_PATH, "./logs")
    log_dir = log_path_setting if os.path.isabs(log_path_setting) else os.path.join(_API_ROOT, log_path_setting)
    _logger = TaskLogger(log_dir, "cardmarket_checker")
    _logger.log(f"  Log path: {log_dir}")

    _logger.log("Obteniendo registros de product_price_tracking...")
    tracking_records = api_get_all("product-price-tracking")

    product_tracking = {}
    for tr in tracking_records:
        ps = tr.get("price_source") or {}
        if "cardmarket" not in ps.get("name", "").lower():
            continue
        pid = (tr.get("product") or {}).get("id")
        if pid is None:
            continue
        product_tracking.setdefault(pid, []).append(tr)

    if not product_tracking:
        _logger.log("No hay productos con tracking de CardMarket")
        finalize_log(_logger, "cardmarket_checker", _API_ROOT, api_request)
        return
    _logger.log(f"  {len(product_tracking)} productos con tracking de CardMarket")

    wishlist_minutes_str = settings_by_key.get(SETTING_KEY_WISHLIST_MINUTES)
    wishlist_minutes_threshold = int(wishlist_minutes_str) if wishlist_minutes_str else None
    if wishlist_minutes_threshold is not None:
        _logger.log(f"  Minutos salto wishlist: {wishlist_minutes_threshold}")

    # --- Inventory ---
    _logger.log("Obteniendo inventario (solo productos con tracking)...")
    inventory_items = []
    all_pids = list(product_tracking.keys())
    BATCH_SIZE = 200
    for i in range(0, len(all_pids), BATCH_SIZE):
        batch_pids = all_pids[i:i + BATCH_SIZE]
        batch = api_get_all("inventory", {
            "product_ids": ",".join(str(pid) for pid in batch_pids),
            "per_page": 500
        })
        if batch:
            inventory_items.extend(batch)

    items_to_check = []
    for item in inventory_items:
        product = item.get("product") or {}
        prod_id = product.get("id")
        if prod_id and prod_id in product_tracking:
            items_to_check.append(item)

    _logger.log(f"  {len(items_to_check)} items de inventario con tracking de CardMarket")

    # Pre-filter: descartar items con precio reciente o por debajo del umbral
    scrape_candidates = []
    for item in items_to_check:
        product = item.get("product") or {}
        prod_id = product.get("id")
        inv_id = item["id"]
        translations = product.get("translations") or []
        prod_name = (translations[0] or {}).get("name", "") if translations else ""
        for tr in product_tracking[prod_id]:
            if not tr.get("url", "").strip():
                continue
            skip = _check_history_skip_sync("inventory-price-history", {
                "inventory_id": inv_id,
                "product_price_tracking_id": tr["id"],
            }, skip_threshold, PRICE_MINUTES_THRESHOLD)
            if skip:
                _logger.log(f"  inv#{inv_id} {prod_name[:50]}: saltando (precio reciente o bajo umbral)")
                continue
            scrape_candidates.append((item, tr, prod_name))

    _logger.log(f"  {len(scrape_candidates)} requieren scraping")

    languages = {}
    conditions = {}

    if scrape_candidates:
        _logger.log("Obteniendo idiomas...")
        languages = {lang["id"]: lang for lang in api_get_all("languages")}
        _logger.log("Obteniendo condiciones...")
        conditions = {cond["id"]: cond for cond in api_get_all("product-conditions")}
        asyncio.run(process_inventory(scrape_candidates, languages, conditions))
    else:
        _logger.log("No hay items de inventario que requieran scraping")

    # --- Wishlist ---
    if not languages or not conditions:
        _logger.log("Obteniendo idiomas...")
        languages = {lang["id"]: lang for lang in api_get_all("languages")}
        _logger.log("Obteniendo condiciones...")
        conditions = {cond["id"]: cond for cond in api_get_all("product-conditions")}

    _logger.log("Obteniendo wishlist items...")
    asyncio.run(process_wishlist(product_tracking, wishlist_minutes_threshold, PRICE_MINUTES_THRESHOLD, languages, conditions))

    finalize_log(_logger, "cardmarket_checker", _API_ROOT, api_request)


async def process_inventory(scrape_candidates, languages, conditions):
    global _logger
    profile_dir, exe_path = find_browser_profile()
    SEP = "=" * 58
    total = len(scrape_candidates)

    async with async_playwright() as pw:
        context, page = await _setup_browser(pw, profile_dir, exe_path)
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        scraped_count = 0
        saved_count = 0
        error_count = 0

        for idx, (item, tr, prod_name) in enumerate(scrape_candidates, 1):
            try:
                inv_id = item["id"]
                inv_lang = item.get("language") or {}
                inv_cond = item.get("condition") or {}

                base_url = tr.get("url", "").strip()
                if not base_url:
                    continue

                price_source = tr.get("price_source") or {}
                lang_param = price_source.get("language_param")
                cond_param = price_source.get("condition_param")

                lang_code = None
                lang_id = inv_lang.get("id")
                if lang_id and lang_id in languages:
                    lang_code = languages[lang_id].get("cardmarket_code")

                cond_code = None
                cond_id = inv_cond.get("id")
                if cond_id and cond_id in conditions:
                    cond_code = conditions[cond_id].get("cardmarket_code")

                params = {}
                if lang_param and lang_code:
                    params[lang_param] = lang_code
                if cond_param and cond_code:
                    params[cond_param] = cond_code

                full_url = base_url
                if params:
                    full_url += "?" + urllib.parse.urlencode(params)

                _logger.log(f"\n[{idx}/{total}] inv#{inv_id} {prod_name[:50]}")
                _logger.log(f"  URL: {full_url}")

                price_str = await scrape_price(page, full_url)
                result = await _save_inventory_price(inv_id, tr["id"], price_str, page)
                if result:
                    scraped_count += 1
                    saved_count += 1
                else:
                    error_count += 1

                if idx < total:
                    delay = random.uniform(DELAY_MIN, DELAY_MAX)
                    _logger.log(f"  Pausa {delay:.1f} s...")
                    await asyncio.sleep(delay)
            except Exception as e:
                error_count += 1
                _logger.log(f"  Error en enlace: {e}")
                continue

        await context.close()

    _logger.log(f"\n  {SEP}")
    _logger.log(f"  Procesados:     {total}")
    _logger.log(f"  Scrapeados:     {scraped_count}")
    _logger.log(f"  Guardados:      {saved_count}")
    _logger.log(f"  Errores:        {error_count}")
    _logger.log(f"  {SEP}\n")


async def process_wishlist(product_tracking, wishlist_minutes, price_minutes, languages, conditions):
    global _logger
    _logger.log("Obteniendo wishlist items activos...")
    wishlist_items = api_get_all("wishlist-items")
    items = [wi for wi in wishlist_items if wi.get("w_state", "buscando") in ("buscando", "notificado")]
    _logger.log(f"  {len(items)} items en wishlist con estado 'buscando' o 'notificado'")

    if not items:
        return

    profile_dir, exe_path = find_browser_profile()
    SEP = "=" * 58

    async with async_playwright() as pw:
        context, page = await _setup_browser(pw, profile_dir, exe_path)
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        scraped_count = 0
        saved_count = 0
        error_count = 0
        total = len(items)

        for idx, wi in enumerate(items, 1):
            prod_id = wi.get("product_id")
            item_id = wi["id"]
            product_number = wi.get("product_number") or ""
            collection_code = wi.get("collection_code") or ""
            product_name = wi.get("product_name") or ""

            tracking_list = product_tracking.get(prod_id, [])
            if not tracking_list:
                _logger.log(f"\n[{idx}/{total}] wish#{item_id} {product_name or product_number}: sin tracking URL, saltando")
                continue

            wi_lang_id = wi.get("language_id")
            wi_cond_id = wi.get("condition_id")

            for tr in tracking_list:
                try:
                    base_url = tr.get("url", "").strip()
                    if not base_url:
                        continue

                    price_source = tr.get("price_source") or {}
                    lang_param = price_source.get("language_param")
                    cond_param = price_source.get("condition_param")

                    lang_code = None
                    if lang_param and wi_lang_id and wi_lang_id in languages:
                        lang_code = languages[wi_lang_id].get("cardmarket_code")

                    cond_code = None
                    if cond_param and wi_cond_id and wi_cond_id in conditions:
                        cond_code = conditions[wi_cond_id].get("cardmarket_code")

                    params = {}
                    if lang_param and lang_code:
                        params[lang_param] = lang_code
                    if cond_param and cond_code:
                        params[cond_param] = cond_code

                    full_url = base_url
                    if params:
                        full_url += "?" + urllib.parse.urlencode(params)

                    product_info = f"wish#{item_id} {product_name or product_number[:50]}"
                    _logger.log(f"\n[{idx}/{total}] {product_info}")
                    _logger.log(f"  URL: {full_url}")

                    skip = await _check_history_skip_no_inventory(item_id, tr["id"], wishlist_minutes, price_minutes)
                    if skip:
                        continue

                    price_str = await scrape_price(page, full_url)

                    if price_str and price_str != "NO_ENCONTRADO":
                        scraped_count += 1
                        try:
                            new_price = float(price_str.replace(" €", "").replace(",", ".").strip())
                        except ValueError:
                            _logger.log(f"  Precio mal formado: '{price_str}', intentando extraer 'From' del HTML...")
                            html = await page.content()
                            fallback = extract_price_from_html(html)
                            if fallback:
                                price_str = fallback
                            else:
                                _logger.log(f"  No se pudo recuperar precio del HTML")
                                error_count += 1
                                continue

                        _logger.log(f"  Precio encontrado: {price_str}")

                        wl_data = {
                            "price": f"{new_price:.2f}",
                            "source": "cardmarket",
                        }
                        try:
                            r = api_post(f"wishlist-items/{item_id}/prices", wl_data)
                            if r:
                                saved_count += 1
                                _logger.log(f"  Guardado en wishlist item {item_id}")
                        except Exception as e:
                            error_count += 1
                            _logger.log(f"  Error al guardar en wishlist: {e}")
                    else:
                        error_count += 1
                        _logger.log(f"  Precio no encontrado")

                    if idx < total:
                        delay = random.uniform(DELAY_MIN, DELAY_MAX)
                        _logger.log(f"  Pausa {delay:.1f} s...")
                        await asyncio.sleep(delay)
                except Exception as e:
                    error_count += 1
                    _logger.log(f"  Error en enlace: {e}")
                    continue

        await context.close()

    _logger.log(f"\n  {SEP}")
    _logger.log(f"  Wishlist items:  {total}")
    _logger.log(f"  Scrapeados:      {scraped_count}")
    _logger.log(f"  Guardados:       {saved_count}")
    _logger.log(f"  Errores:         {error_count}")
    _logger.log(f"  {SEP}\n")


async def _setup_browser(pw, profile_dir, exe_path):
    if profile_dir:
        browser_name = "Brave" if "Brave" in profile_dir or (exe_path and "brave" in exe_path.lower()) else "Chrome"
        _logger.log(f"\n  Usando perfil real de {browser_name}: {profile_dir}")
        _logger.log(f"  Asegurate de tener {browser_name} CERRADO antes de continuar.\n")
        launch_kwargs = dict(
            user_data_dir=profile_dir,
            headless=HEADLESS,
            slow_mo=80,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            no_viewport=True,
            locale="es-ES",
        )
        if exe_path:
            launch_kwargs["executable_path"] = exe_path
        context = await pw.chromium.launch_persistent_context(**launch_kwargs)
        page = await context.new_page()
    else:
        _logger.log("\n  Brave/Chrome no encontrado. Usando Chromium sin perfil.\n")
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            slow_mo=80,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
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
    return context, page


def _check_history_skip_sync(history_endpoint, filter_params, skip_threshold, price_minutes):
    global _logger
    history_records = api_get_all(history_endpoint, filter_params)
    prev = max(history_records, key=lambda r: r.get("recorded_at", "")) if history_records else None

    cutoff = datetime.now() - timedelta(minutes=price_minutes)
    prev_date_str = (prev or {}).get("recorded_at", "")
    if prev and prev_date_str:
        try:
            prev_dt = datetime.fromisoformat(prev_date_str)
            if prev_dt.tzinfo is not None:
                prev_dt = prev_dt.replace(tzinfo=None)
            if prev_dt >= cutoff:
                return True
        except ValueError:
            pass

    if prev and skip_threshold is not None:
        try:
            prev_price_val = float(prev["price"])
            if prev_price_val < skip_threshold:
                return True
        except (ValueError, TypeError):
            pass

    return False


async def _check_history_skip_no_inventory(wishlist_item_id, tracking_id, wishlist_minutes, price_minutes=None):
    global _logger
    prices = api_get(f"wishlist-items/{wishlist_item_id}/prices?limit=1")
    prev = None
    if prices:
        items_list = prices if isinstance(prices, list) else prices.get("items", [])
        if items_list:
            prev = items_list[0]

    minutes = wishlist_minutes if wishlist_minutes is not None else (price_minutes or 10080)
    cutoff = datetime.now() - timedelta(minutes=minutes)
    prev_date_str = (prev or {}).get("recorded_at", "")
    if prev and prev_date_str:
        try:
            prev_dt = datetime.fromisoformat(prev_date_str)
            if prev_dt.tzinfo is not None:
                prev_dt = prev_dt.replace(tzinfo=None)
            if prev_dt >= cutoff:
                _logger.log(f"  Ya tiene precio dentro del margen de {minutes} minuto(s) ({prev_date_str}), saltando")
                return True
        except ValueError:
            pass

    return False


async def _save_inventory_price(inv_id, tracking_id, price_str, page):
    global _logger
    if not price_str or price_str == "NO_ENCONTRADO":
        _logger.log(f"  Precio no encontrado")
        return None

    try:
        new_price = float(price_str.replace(" €", "").replace(",", ".").strip())
    except ValueError:
        _logger.log(f"  Precio mal formado: '{price_str}', intentando extraer 'From' del HTML...")
        html = await page.content()
        fallback = extract_price_from_html(html)
        if fallback:
            price_str = fallback
            new_price = float(price_str.replace(" €", "").replace(",", ".").strip())
            _logger.log(f"  Precio recuperado del HTML: {price_str}")
        else:
            _logger.log(f"  No se pudo recuperar precio del HTML")
            return None

    _logger.log(f"  Precio encontrado: {price_str}")

    history_records = api_get_all("inventory-price-history", {
        "inventory_id": inv_id,
        "product_price_tracking_id": tracking_id,
    })
    prev = max(history_records, key=lambda r: r.get("recorded_at", "")) if history_records else None

    if prev is not None:
        prev_price = float(prev["price"])
        if abs(new_price - prev_price) < 0.001:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            try:
                result = api_request("PATCH", f"inventory-price-history/{prev['id']}", {"recorded_at": now_str})
                if result:
                    _logger.log(f"  Precio sin cambios, actualizado timestamp (id={prev['id']})")
                    return result
            except Exception as e:
                _logger.log(f"  Error al actualizar timestamp: {e}")
            return prev

    if prev is None:
        min_price = new_price
        max_price = new_price
        min_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        max_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    else:
        prev_min = float(prev["min_price"]) if prev.get("min_price") else prev_price
        prev_max = float(prev["max_price"]) if prev.get("max_price") else prev_price

        min_price = min(new_price, prev_min, prev_price)
        max_price = max(new_price, prev_max, prev_price)

        min_date = (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                    if min_price != prev_min
                    else prev.get("min_price_recorded_at"))
        max_date = (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                    if max_price != prev_max
                    else prev.get("max_price_recorded_at"))

    post_data = {
        "inventory_id": inv_id,
        "product_price_tracking_id": tracking_id,
        "price": f"{new_price:.2f}",
        "min_price": f"{min_price:.2f}",
        "max_price": f"{max_price:.2f}",
        "min_price_recorded_at": min_date,
        "max_price_recorded_at": max_date,
    }
    try:
        result = api_post("inventory-price-history", post_data)
        _logger.log(f"  Guardado en inventory_price_history (id={result.get('id', '?')})")
        return result
    except Exception as e:
        _logger.log(f"  Error al guardar: {e}")
        return None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrumpido")
        sys.exit(1)
