import json
import logging
import os
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.mime.text import MIMEText

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
                    stream=sys.stdout)
log = logging.getLogger("wishlist_checker")

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

_smtp_config = None

NOTIFY_HOURS = 24

_token = None
_token_expires_at = None


def _login():
    global _token, _token_expires_at
    if not API_USERNAME or not API_PASSWORD:
        return False
    try:
        body = json.dumps({"username": API_USERNAME, "password": API_PASSWORD}).encode("utf-8")
        req = urllib.request.Request(
            f"{API_BASE.rstrip('/')}/auth/login",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _token = data["token"]
        _token_expires_at = datetime.fromisoformat(data["expires_at"]).replace(tzinfo=timezone.utc)
        return True
    except Exception as e:
        log.error("Login failed: %s", e)
        return False


def _get_token():
    global _token, _token_expires_at
    now = datetime.now(timezone.utc)
    if not _token or not _token_expires_at or _token_expires_at <= now:
        _login()
    return _token


def api_request(method, path, data=None, timeout=15):
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log.info("HTTP %d on %s %s (not found, skipping)", e.code, method, path)
        else:
            log.error("HTTP %d on %s %s", e.code, method, path)
        return None
    except Exception as e:
        log.error("Request error on %s %s: %s", method, path, e)
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


def _load_smtp_config():
    global _smtp_config
    host = api_get("settings/by-key/smtp.host")
    port = api_get("settings/by-key/smtp.port")
    user = api_get("settings/by-key/smtp.username")
    pwd = api_get("settings/by-key/smtp.password")
    fr = api_get("settings/by-key/smtp.from")

    _smtp_config = {
        "host": host.get("setting_value", "") if host else "",
        "port": int(port.get("setting_value", "587")) if port else 587,
        "user": user.get("setting_value", "") if user else "",
        "pass": pwd.get("setting_value", "") if pwd else "",
        "from": fr.get("setting_value", "") if fr else "",
    }


def _send_email(to_addr, subject, body):
    if _smtp_config is None:
        _load_smtp_config()

    cfg = _smtp_config
    if not cfg["host"] or not to_addr:
        log.warning("SMTP not configured or no recipient email — skipping email")
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = cfg["from"] or cfg["user"]
        msg["To"] = to_addr

        with smtplib.SMTP(cfg["host"], cfg["port"]) as server:
            server.starttls()
            if cfg["user"] and cfg["pass"]:
                server.login(cfg["user"], cfg["pass"])
            server.send_message(msg)
        log.info("Email sent to %s: %s", to_addr, subject)
        return True
    except Exception as e:
        log.error("Failed to send email: %s", e)
        return False


def check_wishlist_items():
    log.info("Checking wishlist items for price alerts...")

    items = api_get_all("wishlist-items")
    if not items:
        log.info("No wishlist items found")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=NOTIFY_HOURS)
    alerts_sent = 0

    for item in items:
        item_id = item["id"]
        target_price = item.get("target_price")
        last_price = item.get("last_price")
        email_addr = item.get("user_email")
        last_notified_str = item.get("last_notified_at")
        w_state = item.get("w_state", "buscando")
        product_number = item.get("product_number") or ""
        collection_code = item.get("collection_code") or ""
        product_name = item.get("product_name") or ""

        if w_state in ("notificado", "inactivo", "comprado"):
            log.info("Wishlist item %d skipped: state is '%s'", item_id, w_state)
            continue

        if not target_price:
            log.info("Wishlist item %d skipped: no target_price set", item_id)
            continue
        if not last_price:
            log.info("Wishlist item %d skipped: no last_price (no price history)", item_id)
            continue

        target = Decimal(str(target_price))
        current = Decimal(str(last_price))

        if current > target:
            log.info("Wishlist item %d skipped: current price %.2f > target %.2f", item_id, current, target)
            continue

        if last_notified_str:
            try:
                last_dt = datetime.fromisoformat(last_notified_str)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if last_dt >= cutoff:
                    log.debug("Already notified for item %d within last %d hours", item_id, NOTIFY_HOURS)
                    continue
            except ValueError:
                pass

        if not email_addr:
            log.info("No email for user of wishlist item %d, skipping notification", item_id)
            continue

        subject = f"[CardVault] Precio objetivo alcanzado: {product_number} ({collection_code})"
        body = (
            f"Producto: {product_name or product_number}\n"
            f"Colección: {collection_code}\n"
            f"Precio actual: {current:.2f} €\n"
            f"Precio objetivo: {target:.2f} €\n"
            f"\n"
            f"¡El precio ha caído por debajo de tu objetivo en la wishlist!"
        )

        ok = _send_email(email_addr, subject, body)
        if ok:
            api_post(f"wishlist-items/{item_id}/prices", {
                "price": str(current),
                "source": "notification",
            })
            api_request("PATCH", f"wishlist-items/{item_id}", {"w_state": "notificado"})
            alerts_sent += 1
            log.info("Alert sent for wishlist item %d (%.2f <= %.2f) — w_state set to notificado", item_id, current, target)

    log.info("Wishlist check completed — %d alert(s) sent", alerts_sent)


if __name__ == "__main__":
    if not API_BASE:
        log.error("CARDVAULT_API_BASE not set")
        sys.exit(1)

    if not _login():
        log.error("Failed to authenticate")
        sys.exit(1)

    check_wishlist_items()
