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
    import nodriver as uc
except ImportError:
    print("Faltan dependencias. Ejecuta:\n  pip install nodriver")
    sys.exit(1)

_USE_CFFI = False
try:
    from curl_cffi import requests as cffi_requests
    _USE_CFFI = True
except ImportError:
    pass

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

DELAY_MIN = 8.0
DELAY_MAX = 14.0
CF_DETECTED_BACKOFF_BASE = 30.0
CF_MAX_RETRIES_PER_URL = 3
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


CF_CHALLENGE_PATTERNS = [
    r"just a moment",
    r"checking your browser",
    r"cf-browser-verification",
    r"cf-challenge",
    r"_cf_chl",
    r"cf-spinner",
    r"turnstile",
    r"challenge-platform",
    r"id=\"challenge",
    r"cookies are disabled",
]

CF_STRONG_PATTERNS = [
    r"just a moment",
    r"checking your browser",
    r"_cf_chl",
    r"cf-spinner",
    r"turnstile",
    r"challenge-platform",
]


def _detect_cloudflare(html_text):
    tl = html_text.strip().lower()
    if len(tl) > 8000:
        return False
    if len(tl) < 300 and "cloudflare" in tl:
        return True
    if len(tl) < 500 and "checking your browser" in tl:
        return True
    if len(tl) < 3000:
        for pat in CF_CHALLENGE_PATTERNS:
            if re.search(pat, tl):
                return True
    return False


async def scrape_price(tab, url):
    for cf_try in range(CF_MAX_RETRIES_PER_URL):
        if cf_try == 0:
            try:
                await tab.get(url)
            except Exception as e:
                _logger and _logger.log(f"  Error de navegacion: {e}")
                await tab.sleep(random.uniform(2, 5))
                continue

        await tab.sleep(random.uniform(1.5, 3.0))

        html = await tab.get_content()
        if _detect_cloudflare(html):
            wait = 5 + cf_try * 10 + random.uniform(0, 3)
            _logger and _logger.log(f"  Cloudflare detectado, esperando {wait:.0f}s en la pagina (intento {cf_try+1}/{CF_MAX_RETRIES_PER_URL})...")
            await tab.sleep(wait)
            continue

        price = await _extract_price_from_tab(tab, html)
        if price:
            return price

        await tab.sleep(random.uniform(1, 2))

    return None


async def _extract_price_from_tab(tab, html):
    price = None

    try:
        dts = await tab.select_all("dl dt")
        for dt in dts:
            try:
                txt = (await dt.text).strip().lower()
                if txt in ("desde", "from", "a partir de"):
                    dd = await dt.query_selector("~ dd")
                    if dd:
                        price = (await dd.text).strip()
                        break
            except Exception:
                pass
    except Exception:
        pass

    if not price:
        try:
            loc = await tab.select("div.price-container")
            if loc:
                price = (await loc.text).strip()
        except Exception:
            pass

    if not price:
        try:
            spans = await tab.select_all("span[class*='price']")
            for span in spans:
                txt = (await span.text).strip()
                if "€" in txt and re.search(r"\d", txt):
                    price = txt
                    break
        except Exception:
            pass

    if not price:
        try:
            loc = await tab.select("[itemprop='price']")
            if loc:
                price = (await loc.get_attribute("content")) or (await loc.text).strip()
        except Exception:
            pass

    if not price:
        price = extract_price_from_html(html)

    if price:
        price = normalizar_precio(price)
    return price


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
            "per_page": 500,
            "all": "1"
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

    candidate_meta = []
    for item in items_to_check:
        product = item.get("product") or {}
        prod_id = product.get("id")
        inv_id = item["id"]
        translations = product.get("translations") or []
        prod_name = (translations[0] or {}).get("name", "") if translations else ""
        for tr in product_tracking[prod_id]:
            if not tr.get("url", "").strip():
                continue
            skip, prev = _check_history_skip_sync("inventory-price-history", {
                "inventory_id": inv_id,
                "product_price_tracking_id": tr["id"],
            }, PRICE_MINUTES_THRESHOLD, skip_threshold)
            if skip:
                continue
            has_price = prev is not None
            last_price = float(prev["price"]) if has_price else 0
            last_date = prev.get("recorded_at", "") if has_price else ""
            candidate_meta.append({
                "item": item, "tr": tr, "prod_name": prod_name,
                "has_price": has_price, "last_price": last_price, "last_date": last_date
            })

    candidate_meta.sort(key=lambda c: (
        0 if not c["has_price"] else 1,
        -(c["last_price"] if c["has_price"] else 0),
        c.get("last_date") or ""
    ))
    scrape_candidates = [(c["item"], c["tr"], c["prod_name"]) for c in candidate_meta]

    _logger.log(f"  {len(scrape_candidates)} requieren scraping")

    languages = {}
    conditions = {}

    if scrape_candidates:
        _logger.log("Obteniendo idiomas...")
        languages = {lang["id"]: lang for lang in api_get_all("languages")}
        _logger.log("Obteniendo condiciones...")
        conditions = {cond["id"]: cond for cond in api_get_all("product-conditions")}

    # --- Wishlist ---
    if not languages or not conditions:
        _logger.log("Obteniendo idiomas...")
        languages = {lang["id"]: lang for lang in api_get_all("languages")}
        _logger.log("Obteniendo condiciones...")
        conditions = {cond["id"]: cond for cond in api_get_all("product-conditions")}

    _logger.log("Obteniendo wishlist items...")
    wishlist_items = api_get_all("wishlist-items")
    wishlist_active = [wi for wi in wishlist_items if wi.get("w_state", "buscando") in ("buscando", "notificado")]

    asyncio.run(_run_all(scrape_candidates, languages, conditions, product_tracking,
                         wishlist_minutes_threshold, PRICE_MINUTES_THRESHOLD, wishlist_active))

    finalize_log(_logger, "cardmarket_checker", _API_ROOT, api_request)


async def _run_all(scrape_candidates, languages, conditions, product_tracking,
                   wishlist_minutes, price_minutes, wishlist_active):
    browser = await _init_browser()
    tab = None
    try:
        if browser:
            tab = await browser.get("about:blank")
            _logger.log("[OK] Navegador iniciado correctamente")
        else:
            _logger.log("[WARN] No se pudo iniciar navegador. Usando solo curl_cffi.")
    except Exception as e:
        _logger.log(f"[WARN] Error al abrir pestaña: {e}, usando solo curl_cffi")

    try:
        if scrape_candidates:
            browser, tab = await _process_inventory(scrape_candidates, languages, conditions, browser, tab)
        else:
            _logger.log("No hay items de inventario que requieran scraping")

        await _process_wishlist(product_tracking, wishlist_minutes, price_minutes,
                                languages, conditions, browser, tab, wishlist_active)
    finally:
        if browser:
            try:
                await tab.close()
            except Exception:
                pass
            try:
                browser.stop()
            except Exception:
                pass
            await asyncio.sleep(1)
            _logger.log("Navegador cerrado")


CHROME_PATH = os.getenv("CHROME_PATH")
if not CHROME_PATH or not os.path.isfile(CHROME_PATH):
    CHROME_PATH = None
for _base in [
    os.path.expanduser("~/.cache/ms-playwright"),
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    "/usr/bin/google-chrome-stable",
    "/usr/lib/chromium/chromium",
    "/usr/lib/chromium-browser/chromium-browser",
]:
    if os.path.isfile(_base):
        CHROME_PATH = _base
        break
    if os.path.isdir(_base):
        try:
            _versions = sorted(os.listdir(_base), reverse=True)
        except PermissionError:
            continue
        for _v in _versions:
            for _exe in ("chrome", "chrome-linux/chrome", "chrome-linux64/chrome"):
                _candidate = os.path.join(_base, _v, _exe)
                if os.path.isfile(_candidate):
                    CHROME_PATH = _candidate
                    break
            if CHROME_PATH:
                break
        if CHROME_PATH:
            break

CF_PROFILE_DIR = os.getenv("CARDVAULT_CF_PROFILE_DIR",
                            os.path.join(_SCRIPT_DIR, "chrome_cf_profile"))


async def _init_browser():
    browser_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--headless=new",
        "--disable-extensions",
        "--no-first-run",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-features=OptimizationGuideModelDownloading,OptimizationHintsFetching,TranslateUI",
        "--disable-blink-features=AutomationControlled",
        "--lang=es-ES",
    ]
    os.makedirs(CF_PROFILE_DIR, exist_ok=True)

    try:
        _logger and _logger.log(f"  CHROME_PATH: {CHROME_PATH or '(auto)'}")
        browser = await uc.start(
            headless=HEADLESS,
            browser_executable_path=CHROME_PATH,
            user_data_dir=CF_PROFILE_DIR,
            browser_args=browser_args,
        )
        return browser
    except Exception as e:
        _logger and _logger.log(f"  [WARN] No se pudo iniciar navegador: {e}")
        _logger and _logger.log(f"  [INFO] Instala chromium o chrome en el sistema.")
        return None


_CFFI_SESSION = None


def _cffi_session():
    global _CFFI_SESSION
    if _CFFI_SESSION is None and _USE_CFFI:
        _CFFI_SESSION = cffi_requests.Session()
        _CFFI_SESSION.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8,de;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        })
        try:
            _CFFI_SESSION.get("https://www.cardmarket.com/en", impersonate="chrome124", timeout=15)
        except Exception:
            pass
    return _CFFI_SESSION


async def _scrape_price_cffi(url):
    if not _USE_CFFI:
        return None
    session = _cffi_session()
    browsers = ["chrome124", "chrome131", "safari17_0"]
    for attempt in range(3):
        try:
            imp = browsers[attempt % len(browsers)]
            resp = session.get(
                url,
                impersonate=imp,
                timeout=25,
                headers={"Referer": "https://www.cardmarket.com/"},
            )
            if resp.status_code == 403 or _detect_cloudflare(resp.text):
                _logger and _logger.log(f"  curl_cffi({imp}): Cloudflare detectado (intento {attempt+1})")
                session.cookies.clear()
                await asyncio.sleep(random.uniform(10, 20))
                continue
            if resp.status_code != 200 or len(resp.text) < 500:
                continue
            price = extract_price_from_html(resp.text)
            if price:
                return normalizar_precio(price)
        except Exception as e:
            _logger and _logger.log(f"  curl_cffi error: {e}")
            await asyncio.sleep(random.uniform(3, 8))
    return None


async def _process_inventory(scrape_candidates, languages, conditions, browser, tab):
    global _logger
    SEP = "=" * 58
    total = len(scrape_candidates)

    scraped_count = 0
    saved_count = 0
    error_count = 0
    consecutive_no_price = 0
    consecutive_errors = 0

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

            price_str = None
            if tab is not None and browser is not None:
                price_str = await scrape_price(tab, full_url)

            if not price_str and _USE_CFFI:
                _logger and _logger.log(f"  Intentando con curl_cffi...")
                price_str = await _scrape_price_cffi(full_url)

            result = await _save_inventory_price(inv_id, tr["id"], price_str, tab)
            if result:
                scraped_count += 1
                saved_count += 1
                consecutive_no_price = 0
                consecutive_errors = 0
                _logger.log(f"  inv#{inv_id} {prod_name[:50]} -> {price_str}")
            else:
                error_count += 1
                consecutive_no_price += 1
                _logger.log(f"  inv#{inv_id} {prod_name[:50]} -> SIN PRECIO")
                if consecutive_no_price >= 15 and browser is not None:
                    _logger.log(f"  {consecutive_no_price} productos sin precio seguidos. Bloqueo probable.")
                    _logger.log(f"  Cerrando y reiniciando navegador...")
                    try:
                        await tab.close()
                    except Exception:
                        pass
                    try:
                        browser.stop()
                    except Exception:
                        pass
                    await asyncio.sleep(random.uniform(10, 20))
                    browser = await _init_browser()
                    if browser:
                        tab = await browser.get("about:blank")
                    else:
                        tab = None
                    consecutive_no_price = 0
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        _logger.log("  Demasiados reinicios sin exito. Abortando.")
                        return browser, tab

            if idx < total:
                delay = random.uniform(DELAY_MIN, DELAY_MAX)
                await asyncio.sleep(delay)
        except Exception as e:
            error_count += 1
            consecutive_no_price += 1
            _logger.log(f"  inv#? {e}")
            if consecutive_no_price >= 15 and browser is not None:
                _logger.log(f"  {consecutive_no_price} errores seguidos. Reiniciando navegador...")
                try:
                    await tab.close()
                except Exception:
                    pass
                try:
                    browser.stop()
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(10, 20))
                browser = await _init_browser()
                if browser:
                    tab = await browser.get("about:blank")
                else:
                    tab = None
                consecutive_no_price = 0
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    _logger.log("  Demasiados reinicios. Abortando.")
                    return browser, tab
            continue

    _logger.log(f"  Procesados: {total} | OK: {scraped_count} | Errores: {error_count}")
    return browser, tab


async def _process_wishlist(product_tracking, wishlist_minutes, price_minutes,
                             languages, conditions, browser, tab, wishlist_active):
    global _logger
    items = wishlist_active
    _logger.log(f"  {len(items)} items en wishlist con estado 'buscando' o 'notificado'")

    if not items:
        return

    SEP = "=" * 58

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

                price_str = None
                if tab is not None and browser is not None:
                    price_str = await scrape_price(tab, full_url)

                if not price_str and _USE_CFFI:
                    _logger.log(f"  Intentando con curl_cffi...")
                    price_str = await _scrape_price_cffi(full_url)

                if price_str and price_str != "NO_ENCONTRADO":
                    scraped_count += 1
                    try:
                        new_price = float(price_str.replace(" €", "").replace(",", ".").strip())
                    except ValueError:
                        _logger.log(f"  Precio mal formado: '{price_str}', intentando extraer del HTML...")
                        if tab is not None:
                            html = await tab.get_content()
                            fallback = extract_price_from_html(html)
                            if fallback:
                                price_str = fallback
                                new_price = float(price_str.replace(" €", "").replace(",", ".").strip())
                            else:
                                _logger.log(f"  No se pudo recuperar precio del HTML")
                                error_count += 1
                                continue
                        else:
                            _logger.log(f"  No se pudo recuperar precio (sin navegador)")
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

    _logger.log(f"\n  {SEP}")
    _logger.log(f"  Wishlist items:  {total}")
    _logger.log(f"  Scrapeados:      {scraped_count}")
    _logger.log(f"  Guardados:       {saved_count}")
    _logger.log(f"  Errores:         {error_count}")
    _logger.log(f"  {SEP}\n")


def _check_history_skip_sync(history_endpoint, filter_params, price_minutes, skip_threshold=None):
    global _logger
    history_records = api_get_all(history_endpoint, filter_params)
    prev = max(history_records, key=lambda r: r.get("recorded_at", "")) if history_records else None

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=price_minutes)
    prev_date_str = (prev or {}).get("recorded_at", "")
    if prev and prev_date_str:
        try:
            prev_dt = datetime.fromisoformat(prev_date_str)
            if prev_dt.tzinfo is None:
                prev_dt = prev_dt.replace(tzinfo=timezone.utc)
            if prev_dt >= cutoff:
                return True, prev
        except ValueError:
            pass

    if prev and skip_threshold is not None:
        try:
            prev_price_val = float(prev["price"])
            if prev_price_val < skip_threshold:
                return True, prev
        except (ValueError, TypeError):
            pass

    return False, prev


async def _check_history_skip_no_inventory(wishlist_item_id, tracking_id, wishlist_minutes, price_minutes=None):
    global _logger
    prices = api_get(f"wishlist-items/{wishlist_item_id}/prices?limit=1")
    prev = None
    if prices:
        items_list = prices if isinstance(prices, list) else prices.get("items", [])
        if items_list:
            prev = items_list[0]

    minutes = wishlist_minutes if wishlist_minutes is not None else (price_minutes or 10080)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    prev_date_str = (prev or {}).get("recorded_at", "")
    if prev and prev_date_str:
        try:
            prev_dt = datetime.fromisoformat(prev_date_str)
            if prev_dt.tzinfo is None:
                prev_dt = prev_dt.replace(tzinfo=timezone.utc)
            if prev_dt >= cutoff:
                _logger.log(f"  Ya tiene precio dentro del margen de {minutes} minuto(s) ({prev_date_str}), saltando")
                return True
        except ValueError:
            pass

    return False


async def _save_inventory_price(inv_id, tracking_id, price_str, tab):
    global _logger
    if not price_str or price_str == "NO_ENCONTRADO":
        return None

    try:
        new_price = float(price_str.replace(" €", "").replace(",", ".").strip())
    except ValueError:
        if tab is not None:
            html = await tab.get_content()
            fallback = extract_price_from_html(html)
            if fallback:
                price_str = fallback
                new_price = float(price_str.replace(" €", "").replace(",", ".").strip())
            else:
                return None
        else:
            return None

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
        return result
    except Exception as e:
        return None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrumpido")
        sys.exit(1)
