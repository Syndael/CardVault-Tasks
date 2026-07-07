#!/usr/bin/env python3
"""
Production Instagram Publisher for CardVault.

Fetches pending publications from the CardVault API and publishes them
to Instagram via the Meta Graph API Content Publishing endpoint.

Steps per publication:
  1. Load inventory item and its files (ordered, primary first)
  2. Build caption from product/inventory info
  3. Upload photo(s):
     - Single photo -> simple media container -> publish
     - Multiple photos -> carousel (children) -> publish
  4. Update publication status, save Instagram media ID/permalink
  5. Mark inventory as posted_instagram = True

Usage:
  python instagram_publisher.py                        # publish all pending
  python instagram_publisher.py --publication-id 42    # publish specific
  python instagram_publisher.py --dry-run              # only list pending

Environment (tasks .env):
  CARDVAULT_API_BASE, CARDVAULT_API_USERNAME, CARDVAULT_API_PASSWORD
  IG_APP_ID, IG_APP_SECRET (optional, for token refresh)
"""

import argparse
import json
import os
import shutil
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText

from dotenv import load_dotenv
from task_logger import TaskLogger, finalize_log

load_dotenv()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_API_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "CardVault-API"))

_logger: TaskLogger | None = None

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

_TOKEN: str | None = None
_TOKEN_EXPIRES: datetime | None = None

GRAPH_API_BASE = "https://graph.facebook.com"
GRAPH_API_VERSION = "v22.0"

_IG_TOKEN: str | None = None
_IG_USER_ID: str | None = None
_PUBLIC_URL_BASE: str | None = None
_EXPORT_PUBLIC_PATH: str | None = None
_MAX_CAROUSEL_ITEMS = 10


def _login() -> bool:
    global _TOKEN, _TOKEN_EXPIRES
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
        _TOKEN = data["token"]
        _TOKEN_EXPIRES = datetime.fromisoformat(data["expires_at"]).replace(tzinfo=timezone.utc)
        return True
    except Exception as e:
        _logger and _logger.log(f"[ERROR] Login failed: {e}")
        return False


def _get_token() -> str | None:
    global _TOKEN, _TOKEN_EXPIRES
    now = datetime.now(timezone.utc)
    if not _TOKEN or not _TOKEN_EXPIRES or _TOKEN_EXPIRES <= now:
        _login()
    return _TOKEN


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
        if e.code == 401 and _login():
            token = _get_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        return None
    except Exception:
        return None


def api_get(path):
    return api_request("GET", path)


def api_post(path, data):
    return api_request("POST", path, data)


def api_patch(path, data):
    return api_request("PATCH", path, data)


def get_setting(key):
    data = api_get(f"settings/by-key/{key}")
    if data and "setting_value" in data:
        return data["setting_value"]
    return None


def update_setting(key, value):
    return api_patch(f"settings/by-key/{key}", {"setting_value": value}) is not None


def ig_get(path, params=None):
    if params is None:
        params = {}
    if "access_token" not in params:
        params["access_token"] = _IG_TOKEN
    qs = urllib.parse.urlencode(params)
    url = f"{GRAPH_API_BASE}/{GRAPH_API_VERSION}/{path.lstrip('/')}?{qs}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        _logger and _logger.log(f"[IG HTTP {e.code}] GET {path}: {raw[:300]}")
        return None
    except Exception as e:
        _logger and _logger.log(f"[IG ERROR] GET {path}: {e}")
        return None


def ig_post(path, data):
    params = {"access_token": _IG_TOKEN}
    params.update(data)
    body = urllib.parse.urlencode(params).encode("utf-8")
    url = f"{GRAPH_API_BASE}/{GRAPH_API_VERSION}/{path.lstrip('/')}"
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        _logger and _logger.log(f"[IG HTTP {e.code}] POST {path}: {raw[:500]}")
        return {"error": True, "status": e.code, "body": raw[:500]}
    except Exception as e:
        _logger and _logger.log(f"[IG ERROR] POST {path}: {e}")
        return {"error": str(e)}


def try_refresh_token(token):
    app_id = get_setting("instagram.app.id") or os.getenv("IG_APP_ID")
    app_secret = get_setting("instagram.app.secret") or os.getenv("IG_APP_SECRET")

    if not app_id or not app_secret:
        _logger and _logger.log("[SKIP] No IG_APP_ID / IG_APP_SECRET configured, cannot refresh token.")
        return token

    _logger and _logger.log("Refreshing token...")
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": token,
    }
    qs = urllib.parse.urlencode(params)
    url = f"{GRAPH_API_BASE}/{GRAPH_API_VERSION}/oauth/access_token?{qs}"

    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        _logger and _logger.log(f"[WARN] Token refresh failed: {e}")
        return token

    new_token = data.get("access_token")
    if not new_token:
        _logger and _logger.log("[WARN] Token refresh returned no new token")
        return token

    expires_in = data.get("expires_in", 0)
    _logger and _logger.log(f"[OK] Token refreshed! Expires in {expires_in // 86400} days")

    if update_setting("instagram.api.token", new_token):
        _logger and _logger.log("[OK] New token saved to CardVault settings")
    else:
        _logger and _logger.log("[WARN] Could not save token")

    return new_token


def validate_ig_account():
    _logger and _logger.log("Validating Instagram token...")
    result = ig_get("me", params={"fields": "id,name,accounts"})
    if not result or "error" in result or "id" not in result:
        _logger and _logger.log("[FAIL] Token invalid")
        return False

    ig_user_id = get_setting("instagram.user.id") or os.getenv("IG_USER_ID")
    if ig_user_id:
        check = ig_get(f"{ig_user_id}", params={"fields": "id,username,name"})
        if check and "id" in check:
            _logger and _logger.log(f"[OK] IG Account: @{check.get('username', '?')} ({check.get('name', '?')})")
            global _IG_USER_ID
            _IG_USER_ID = ig_user_id
            return True

    _logger and _logger.log("[FAIL] No valid Instagram User ID configured. Set 'instagram.user.id'")
    return False


_IG_TMP_PREFIX = "ig_tmp"


def build_image_url(file_id, pub_id):
    if not _PUBLIC_URL_BASE:
        return None
    return f"{_PUBLIC_URL_BASE.rstrip('/')}/{_IG_TMP_PREFIX}/pub_{pub_id}/{file_id}.jpg"


def export_images_to_public(pub_id, file_ids):
    if not _EXPORT_PUBLIC_PATH:
        _logger and _logger.log("[SKIP] No 'export.public.images.path' configured, cannot export images")
        return False

    tmp_dir = os.path.join(_EXPORT_PUBLIC_PATH, _IG_TMP_PREFIX, f"pub_{pub_id}")
    os.makedirs(tmp_dir, exist_ok=True)

    _logger and _logger.log(f"  Exporting {len(file_ids)} image(s) to {tmp_dir}...")
    for fid in file_ids:
        dest = os.path.join(tmp_dir, f"{fid}.jpg")
        if os.path.exists(dest):
            _logger and _logger.log(f"    File {fid}.jpg already exists, skipping download")
            continue
        url = f"{API_BASE.rstrip('/')}/product-catalog/files/{fid}/content"
        token = _get_token()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            with open(dest, "wb") as fh:
                fh.write(data)
            _logger and _logger.log(f"    Exported file {fid}.jpg ({len(data)} bytes)")
        except Exception as e:
            _logger and _logger.log(f"    [WARN] Failed to export file {fid}: {e}")
            return False

    _logger and _logger.log(f"  [OK] All images exported to public directory")
    return True


def cleanup_public_images(pub_id):
    if not _EXPORT_PUBLIC_PATH:
        return
    tmp_dir = os.path.join(_EXPORT_PUBLIC_PATH, _IG_TMP_PREFIX, f"pub_{pub_id}")
    if os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        _logger and _logger.log(f"  Cleaned up temp directory: {tmp_dir}")


def build_caption(inv, product_name, collection_name):
    parts = [f"{product_name}"]
    if collection_name:
        parts.append(f"Coleccion: {collection_name}")
    lang = inv.get("language", {})
    if lang and lang.get("name"):
        parts.append(f"Idioma: {lang['name']}")
    cond = inv.get("condition", {})
    if cond and cond.get("name"):
        parts.append(f"Estado: {cond['name']}")
    notes = inv.get("notes")
    if notes and notes.strip():
        parts.append("")
        parts.append(notes.strip())

    if len(parts) == 1:
        parts.append("")

    parts.append("#CardVault #TCG #Coleccionismo")
    return "\n".join(parts)


def fetch_permalink(media_id):
    result = ig_get(f"{media_id}", params={"fields": "permalink"})
    if result and "permalink" in result:
        return result["permalink"]
    _logger and _logger.log(f"  [WARN] Could not fetch permalink for media {media_id}")
    return None


def publish_single_image(file_id, pub_id, caption):
    image_url = build_image_url(file_id, pub_id)
    if not image_url:
        return None, "No 'instagram.public.url.base' setting configured"

    _logger and _logger.log("  Creating media container (single image)...")
    result = ig_post(f"{_IG_USER_ID}/media", {
        "image_url": image_url,
        "caption": caption,
    })
    if not result or result.get("error"):
        return None, f"Media creation failed: {result}"

    creation_id = result.get("id")
    if not creation_id:
        return None, f"No creation ID: {result}"

    _logger and _logger.log(f"  Media container created (ID: {creation_id})")
    if not wait_for_media(creation_id):
        return None, "Media processing did not finish"

    _logger and _logger.log("  Publishing...")
    pub = ig_post(f"{_IG_USER_ID}/media_publish", {"creation_id": creation_id})
    if not pub or pub.get("error"):
        return None, f"Publish failed: {pub}"

    media_id = pub.get("id")
    permalink = fetch_permalink(media_id)
    _logger and _logger.log(f"  [OK] Published! ID: {media_id}")
    return media_id, permalink


def publish_carousel(file_ids, pub_id, caption):
    children_ids = []
    for fid in file_ids:
        image_url = build_image_url(fid, pub_id)
        if not image_url:
            continue

        result = ig_post(f"{_IG_USER_ID}/media", {
            "image_url": image_url,
            "is_carousel_item": "true",
        })
        if not result or result.get("error"):
            _logger and _logger.log(f"  [WARN] Carousel item failed for file {fid}: {result}")
            continue

        child_id = result.get("id")
        if child_id:
            _logger and _logger.log(f"  Carousel item created (ID: {child_id})")
            children_ids.append(child_id)

    if not children_ids:
        return None, "No carousel items could be created"

    if len(children_ids) == 1:
        return publish_single_image(file_ids[0], pub_id, caption)

    _logger and _logger.log(f"  Creating carousel with {len(children_ids)} items...")
    result = ig_post(f"{_IG_USER_ID}/media", {
        "media_type": "CAROUSEL",
        "children": [f"{cid}" for cid in children_ids],
        "caption": caption,
    })
    if not result or result.get("error"):
        return None, f"Carousel creation failed: {result}"

    creation_id = result.get("id")
    if not creation_id:
        return None, f"No carousel creation ID: {result}"

    _logger and _logger.log(f"  Carousel container created (ID: {creation_id})")
    if not wait_for_media(creation_id):
        return None, "Carousel processing did not finish"

    _logger and _logger.log("  Publishing carousel...")
    pub = ig_post(f"{_IG_USER_ID}/media_publish", {"creation_id": creation_id})
    if not pub or pub.get("error"):
        return None, f"Carousel publish failed: {pub}"

    media_id = pub.get("id")
    permalink = fetch_permalink(media_id)
    _logger and _logger.log(f"  [OK] Carousel published! ID: {media_id}")
    return media_id, permalink


def wait_for_media(creation_id, max_attempts=15):
    for attempt in range(max_attempts):
        result = ig_get(f"{creation_id}", params={"fields": "status_code"})
        if not result:
            time.sleep(5)
            continue

        status = result.get("status_code", "UNKNOWN")
        _logger and _logger.log(f"  Status check {attempt + 1}: {status}")

        if status == "FINISHED":
            return True
        elif status == "ERROR":
            _logger and _logger.log("  [FAIL] Media status is ERROR")
            return False
        elif status == "EXPIRED":
            _logger and _logger.log("  [FAIL] Media expired")
            return False
        time.sleep(5)

    _logger and _logger.log("  [FAIL] Media did not finish")
    return False


def _load_smtp_config():
    host = get_setting("smtp.host")
    port = get_setting("smtp.port")
    user = get_setting("smtp.username")
    pwd = get_setting("smtp.password")
    fr = get_setting("smtp.from")
    return {
        "host": host or "",
        "port": int(port) if port else 587,
        "user": user or "",
        "pass": pwd or "",
        "from": fr or "cardvault@localhost",
    }


def send_email_notification(to_addr, subject, message):
    if not to_addr:
        _logger and _logger.log("  [NOTIFY email] no recipient")
        return
    cfg = _load_smtp_config()
    if not cfg["host"]:
        _logger and _logger.log("  [NOTIFY email] SMTP not configured")
        return
    try:
        msg = MIMEText(message, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = cfg["from"]
        msg["To"] = to_addr
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=10) as server:
            server.starttls()
            if cfg["user"] and cfg["pass"]:
                server.login(cfg["user"], cfg["pass"])
            server.send_message(msg)
        _logger and _logger.log(f"  [NOTIFY email] Sent to {to_addr}")
    except Exception as e:
        _logger and _logger.log(f"  [NOTIFY email] error: {e}")




def send_telegram_notification(message, chat_id):
    bot_token = get_setting("bot.telegram.token")
    if not bot_token or not chat_id:
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message[:4096],
        }).encode("utf-8")
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        urllib.request.urlopen(req, timeout=10)
        _logger and _logger.log(f"  [NOTIFY telegram] Sent to {chat_id}")
    except Exception as e:
        _logger and _logger.log(f"  [NOTIFY telegram] error for {chat_id}: {e}")


def process_publication(pub):
    pub_id = pub["id"]
    inv_id = pub["inventory_id"]
    caption = pub.get("caption") or ""
    existing_status = pub.get("status", "pending_publish")

    if existing_status in ("published", "cancelled"):
        _logger and _logger.log(f"  [SKIP] Publication #{pub_id} already {existing_status}")
        return

    _logger and _logger.log(f"\n{'=' * 58}")
    _logger and _logger.log(f"Processing publication #{pub_id} (inventory #{inv_id})")
    _logger and _logger.log(f"{'=' * 58}")

    inv = api_get(f"inventory/{inv_id}")
    if not inv:
        _logger and _logger.log(f"[FAIL] Inventory #{inv_id} not found")
        api_patch(f"publications/{pub_id}", {"status": "failed", "error_message": "Inventory not found"})
        return

    product = inv.get("product") or {}
    collection = inv.get("collection") or {}
    product_name = product.get("name") or product.get("product_number", f"Product #{product.get('id', '?')}")
    collection_name = collection.get("name") or collection.get("code", "")

    if not caption:
        caption = build_caption(inv, product_name, collection_name)
        _logger and _logger.log(f"  Auto-generated caption ({len(caption)} chars)")

    files_data = api_get(f"files/by-inventory/{inv_id}") or []
    ig_files = [f for f in files_data if f.get("instagram_sort_order") is not None]
    ig_files.sort(key=lambda f: f["instagram_sort_order"])
    file_ids = [f["id"] for f in ig_files if f.get("id")]

    _logger and _logger.log(f"  Product: {product_name}")
    _logger and _logger.log(f"  Collection: {collection_name}")
    _logger and _logger.log(f"  Files: {len(file_ids)} image(s)")

    api_patch(f"publications/{pub_id}", {"status": "processing"})

    if not export_images_to_public(pub_id, file_ids):
        error_msg = "Could not export images to public directory"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publications/{pub_id}", {"status": "failed", "error_message": error_msg})
        return

    media_id = None
    permalink = None
    error_msg = None

    try:
        if len(file_ids) == 0:
            error_msg = "No images for this inventory item"
            _logger and _logger.log(f"  [FAIL] {error_msg}")
        elif len(file_ids) == 1:
            media_id, permalink_or_err = publish_single_image(file_ids[0], pub_id, caption)
            if media_id:
                permalink = permalink_or_err
            else:
                error_msg = permalink_or_err
        else:
            media_id, permalink_or_err = publish_carousel(file_ids[:_MAX_CAROUSEL_ITEMS], pub_id, caption)
            if media_id:
                permalink = permalink_or_err
            else:
                error_msg = permalink_or_err
    except Exception as e:
        error_msg = str(e)
        _logger and _logger.log(f"  [EXCEPTION] {error_msg}")
    finally:
        cleanup_public_images(pub_id)

    if media_id:
        update_data = {
            "status": "published",
            "published_at": datetime.now().isoformat(),
            "instagram_media_id": media_id,
            "instagram_permalink": permalink,
        }
        api_patch(f"publications/{pub_id}", update_data)
        api_patch(f"inventory/{inv_id}", {"posted_instagram": "1"})
        api_post("inventory-urls", {"inventory_id": inv_id, "url": permalink})
        _logger and _logger.log(f"  [OK] Publication #{pub_id} completed: {permalink}")

        notify_msg = f"Publicacion #{pub_id} completada!\nProducto: {product_name}\n{permalink}"
        owner_id = inv.get("user_id")
        if owner_id:
            owner = api_get(f"auth/user/{owner_id}")
            if owner:
                if owner.get("email"):
                    send_email_notification(owner["email"], "CardVault - Publicacion Instagram", notify_msg)
                if owner.get("telegram_id"):
                    send_telegram_notification(notify_msg, owner["telegram_id"])
    else:
        api_patch(f"publications/{pub_id}", {
            "status": "failed",
            "error_message": error_msg or "Unknown error",
        })
        _logger and _logger.log(f"  [FAIL] Publication #{pub_id} failed: {error_msg}")


def main():
    global _IG_TOKEN, _logger

    parser = argparse.ArgumentParser(description="CardVault Instagram Publisher")
    parser.add_argument("--publication-id", type=int, default=None, help="Publish a specific publication by ID")
    parser.add_argument("--dry-run", action="store_true", help="Show pending without publishing")
    args = parser.parse_args()

    if not API_BASE:
        print("[FAIL] CARDVAULT_API_BASE not set in .env")
        sys.exit(1)

    settings_data = api_get("settings") or {}
    settings_list = settings_data.get("items", [])
    settings_by_key = {item["setting_key"]: item.get("setting_value") for item in settings_list if "setting_key" in item}

    log_path_setting = settings_by_key.get("tasks.log.path", "./logs")
    log_dir = log_path_setting if os.path.isabs(log_path_setting) else os.path.join(_API_ROOT, log_path_setting)
    _logger = TaskLogger(log_dir, "instagram_publisher")

    _logger.log(f"CardVault API: {API_BASE}")
    if not _login():
        _logger.log("[FAIL] Could not authenticate")
        sys.exit(1)
    _logger.log("[OK] Authenticated")

    ig_token = settings_by_key.get("instagram.api.token") or os.getenv("IG_ACCESS_TOKEN")
    if not ig_token:
        _logger.log("[FAIL] No Instagram access token. Set 'instagram.api.token' setting.")
        finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)
        sys.exit(1)

    _IG_TOKEN = try_refresh_token(ig_token)

    if not validate_ig_account():
        finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)
        sys.exit(1)

    global _PUBLIC_URL_BASE, _EXPORT_PUBLIC_PATH

    _PUBLIC_URL_BASE = settings_by_key.get("instagram.public.url.base")
    if _PUBLIC_URL_BASE:
        _logger.log(f"[OK] Public URL base: {_PUBLIC_URL_BASE}")
    else:
        _logger.log("[WARN] No 'instagram.public.url.base' configured. Image URLs will not be resolvable.")
        _logger.log("  Set setting 'instagram.public.url.base' to your public web URL (e.g. https://chorizox.duckdns.org:9454)")

    _EXPORT_PUBLIC_PATH = settings_by_key.get("export.public.images.path")
    if _EXPORT_PUBLIC_PATH:
        _logger.log(f"[OK] Export public images path: {_EXPORT_PUBLIC_PATH}")
    else:
        _logger.log("[WARN] No 'export.public.images.path' configured. Cannot export images for Instagram.")
        _logger.log("  Set setting 'export.public.images.path' to the public web images directory.")
        _logger.log("  Images will be temporarily copied to {path}/ig_tmp/pub_{id}/ for Instagram to fetch.")

    if args.publication_id:
        pub = api_get(f"publications/{args.publication_id}")
        if not pub:
            _logger.log(f"[FAIL] Publication #{args.publication_id} not found")
            finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)
            sys.exit(1)
        process_publication(pub)
    else:
        pending = api_get("publications/pending-publish") or []
        _logger.log(f"Found {len(pending)} pending publication(s)")

        if args.dry_run:
            for p in pending:
                inv = api_get(f"inventory/{p['inventory_id']}")
                product = (inv or {}).get("product", {})
                name = product.get("name") or product.get("product_number", "?")
                _logger.log(f"  #{p['id']} | inv #{p['inventory_id']} | {name} | scheduled: {p.get('scheduled_at')}")
            finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)
            return

        for pub in pending:
            process_publication(pub)

    _logger.log("[DONE] Instagram publisher finished")
    finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)


if __name__ == "__main__":
    main()
