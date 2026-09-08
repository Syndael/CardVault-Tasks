#!/usr/bin/env python3
"""
Telegram Publisher for CardVault.

Fetches pending Telegram publication details from the CardVault API and publishes them
to Telegram via Bot API.

Steps per detail:
  1. Load publication and its files
  2. Build caption from product/inventory info
  3. Download first image to temp file
  4. Send photo + caption via Bot API
  5. Update detail status, save message ID/permalink

Usage:
  python telegram_publisher.py                        # publish all pending
  python telegram_publisher.py --publication-id 42    # publish specific
  python telegram_publisher.py --dry-run              # only list pending

Environment (tasks .env):
  CARDVAULT_API_BASE, CARDVAULT_API_USERNAME, CARDVAULT_API_PASSWORD

Settings (via CardVault API):
  task.publisher.telegram.token    → Telegram Bot Token for publishing
  task.publisher.telegram.chat.id  → Default chat/channel ID for publications
"""

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv

from task_logger import TaskLogger, finalize_log
from caption_resolver import resolve_caption_tags
from task_notifier import notify_unresolved_tags

load_dotenv()

BUILD_VERSION = "v1.1"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_API_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "CardVault-API"))

_logger: TaskLogger | None = None

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

_TOKEN: str | None = None
_TOKEN_EXPIRES: datetime | None = None

_SETTINGS_CACHE: dict | None = None
_TELEGRAM_BOT_TOKEN: str | None = None


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
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else None
            except Exception:
                return None
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
    if _SETTINGS_CACHE is not None:
        return _SETTINGS_CACHE.get(key)
    data = api_get(f"settings/by-key/{key}")
    if data and "setting_value" in data:
        return data["setting_value"]
    return None


def download_first_image_to_temp(file_ids):
    if not file_ids:
        return None
    fid = file_ids[0]
    url = f"{API_BASE.rstrip('/')}/product-catalog/files/{fid}/content"
    token = _get_token()
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except Exception as e:
        _logger and _logger.log(f"  [WARN] Fallo al descargar fichero {fid}: {e}")
        return None

    ext = ".jpg"
    content_type = resp.headers.get("Content-Type", "")
    if "png" in content_type:
        ext = ".png"
    elif "gif" in content_type:
        ext = ".gif"
    elif "webp" in content_type:
        ext = ".webp"

    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp.write(data)
    tmp.close()
    _logger and _logger.log(f"  Descargado fichero {fid} ({len(data)} bytes)")
    return tmp.name


def cleanup_temp_file(tmp_file):
    if tmp_file:
        try:
            os.unlink(tmp_file)
        except Exception:
            pass


def send_telegram_photo(chat_id, photo_path, caption=""):
    global _TELEGRAM_BOT_TOKEN
    if _TELEGRAM_BOT_TOKEN is None:
        _TELEGRAM_BOT_TOKEN = get_setting("task.publisher.telegram.token")
    bot_token = _TELEGRAM_BOT_TOKEN
    if not bot_token or not chat_id:
        return None, "No bot token or chat_id"
    try:
        boundary = "boundary" + str(int(time.time() * 1000000))
        with open(photo_path, "rb") as fh:
            photo_data = fh.read()
        filename = os.path.basename(photo_path)
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption[:1024]}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode("utf-8") + photo_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

        url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        message_id = result.get("result", {}).get("message_id")
        _logger and _logger.log(f"  [OK] Foto enviada a {chat_id}, message_id={message_id}")
        return message_id, None
    except Exception as e:
        _logger and _logger.log(f"  [ERROR] error envio foto a {chat_id}: {e}")
        return None, str(e)


def send_telegram_message(chat_id, text):
    global _TELEGRAM_BOT_TOKEN
    if _TELEGRAM_BOT_TOKEN is None:
        _TELEGRAM_BOT_TOKEN = get_setting("task.publisher.telegram.token")
    bot_token = _TELEGRAM_BOT_TOKEN
    if not bot_token or not chat_id:
        return None, "No bot token or chat_id"
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text[:4096],
        }).encode("utf-8")
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        message_id = result.get("result", {}).get("message_id")
        _logger and _logger.log(f"  [OK] Mensaje enviado a {chat_id}, message_id={message_id}")
        return message_id, None
    except Exception as e:
        _logger and _logger.log(f"  [ERROR] error envio mensaje a {chat_id}: {e}")
        return None, str(e)


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

    text = "\n".join(parts)
    if len(text) > 1024:
        text = text[:1021] + "..."
    return text


def process_detail(detail, context=None):
    detail_id = detail["id"]
    pub_id = detail["publication_id"]
    pub = detail.get("publication") or api_get(f"publications/{pub_id}") or {}
    caption = detail.get("resolved_caption") or detail.get("caption") or pub.get("caption") or ""
    existing_status = detail.get("status", "pending_publish")

    if existing_status in ("published", "cancelled"):
        _logger and _logger.log(f"  [SKIP] Detail #{detail_id} already {existing_status}")
        return

    inventories = pub.get("inventories") or []
    inv_ids = [inv["id"] for inv in inventories if inv.get("id")]

    _logger and _logger.log(f"\n{'=' * 58}")
    if inv_ids:
        _logger and _logger.log(f"Processing Telegram detail #{detail_id} (pub #{pub_id}, inventories: {inv_ids})")
    else:
        _logger and _logger.log(f"Processing Telegram detail #{detail_id} (pub #{pub_id}, manual)")
    _logger and _logger.log(f"{'=' * 58}")

    product_name = "Manual publication"
    collection_name = ""
    inv_for_meta = None

    for inv_id in inv_ids:
        inv = api_get(f"inventory/{inv_id}")
        if not inv:
            continue
        if inv_for_meta is None:
            inv_for_meta = inv
            product = inv.get("product") or {}
            collection = inv.get("collection") or {}
            product_name = product.get("name") or product.get("product_number", f"Product #{product.get('id', '?')}")
            collection_name = collection.get("name") or collection.get("code", "")

    pub_files = api_get(f"files/by-publication/{pub_id}") or []
    detail_files = detail.get("files") or []
    all_file_ids = [f["file_id"] for f in detail_files if f.get("file_id")]

    if not caption:
        if inv_for_meta:
            caption = build_caption(inv_for_meta, product_name, collection_name)
        else:
            caption = f"Publicacion #{pub_id}\n#CardVault #TCG"
        _logger and _logger.log(f"  Auto-generated caption ({len(caption)} chars)")

    resolved_caption, unresolved_tags = resolve_caption_tags(caption, pub)
    if unresolved_tags:
        error_msg = f"Tags no resueltos: {', '.join(unresolved_tags)}"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        notify_unresolved_tags(detail, pub, unresolved_tags, API_BASE, _get_token, _logger)
        return
    caption = resolved_caption

    _logger and _logger.log(f"  Product: {product_name}")
    if collection_name:
        _logger and _logger.log(f"  Collection: {collection_name}")
    _logger and _logger.log(f"  Files: {len(all_file_ids)} file(s)")
    
    is_broadcast = detail.get("is_broadcast", False)
    if is_broadcast:
        _logger and _logger.log(f"  Mode: broadcast (text only)")
    else:
        _logger and _logger.log(f"  Mode: content (with images)")

    if not all_file_ids and not is_broadcast:
        error_msg = "No images for this publication"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        return

    api_patch(f"publication-details/{detail_id}", {"status": "processing"})

    chat_id = get_setting("task.publisher.telegram.chat.id")
    if not chat_id:
        error_msg = "task.publisher.telegram.chat.id not configured"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        return

    message_id = None
    error_msg = None

    if is_broadcast:
        _logger and _logger.log(f"  Sending text message...")
        try:
            message_id, error = send_telegram_message(chat_id, caption)
            if not message_id:
                error_msg = error or "Error enviando mensaje a Telegram"
        except Exception as e:
            error_msg = str(e)
            _logger and _logger.log(f"  [EXCEPTION] {error_msg}")
    else:
        _logger and _logger.log(f"  Downloading first image...")
        tmp_file = download_first_image_to_temp(all_file_ids)

        if not tmp_file:
            error_msg = "Could not download image from API"
            _logger and _logger.log(f"  [FAIL] {error_msg}")
            api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
            return

        try:
            message_id, error = send_telegram_photo(chat_id, tmp_file, caption)
            if not message_id:
                error_msg = error or "Error enviando foto a Telegram"
        except Exception as e:
            error_msg = str(e)
            _logger and _logger.log(f"  [EXCEPTION] {error_msg}")
        finally:
            cleanup_temp_file(tmp_file)

    if message_id:
        bot_token = get_setting("task.publisher.telegram.token") or ""
        permalink = f"https://t.me/c/{chat_id.replace('-', '')}/{message_id}" if chat_id.startswith("-") else f"https://t.me/{chat_id}/{message_id}"
        clean_permalink = permalink.split('?')[0].split('#')[0]
        update_data = {
            "status": "published",
            "published_at": datetime.now().isoformat(),
            "media_id": str(message_id),
            "permalink": clean_permalink,
        }
        api_patch(f"publication-details/{detail_id}", update_data)
        _logger and _logger.log(f"  [OK] Detail #{detail_id} completed: {clean_permalink}")
    else:
        api_patch(f"publication-details/{detail_id}", {
            "status": "failed",
            "error_message": error_msg or "Unknown error",
        })
        _logger and _logger.log(f"  [FAIL] Detail #{detail_id} failed: {error_msg}")


def main():
    global _logger, _SETTINGS_CACHE

    parser = argparse.ArgumentParser(description="CardVault Telegram Publisher")
    parser.add_argument("--publication-id", type=int, default=None, help="Publish a specific publication by ID")
    parser.add_argument("--dry-run", action="store_true", help="Show pending without publishing")
    args = parser.parse_args()

    if not API_BASE:
        print("[FAIL] CARDVAULT_API_BASE not set in .env")
        sys.exit(1)

    if not _login():
        print("[FAIL] Could not authenticate with CardVault API")
        sys.exit(1)

    settings_data = api_get("settings?per_page=500") or {}
    settings_list = settings_data.get("items", [])
    settings_by_key = {item["setting_key"]: item.get("setting_value") for item in settings_list if "setting_key" in item}

    _SETTINGS_CACHE = settings_by_key

    log_path_setting = settings_by_key.get("tasks.log.path", "./logs")
    log_dir = log_path_setting if os.path.isabs(log_path_setting) else os.path.join(_API_ROOT, log_path_setting)
    _logger = TaskLogger(log_dir, "telegram_publisher")

    _logger.log(f"CardVault API: {API_BASE}")
    _logger.log(f"[OK] Authenticated  [{BUILD_VERSION}]")

    if args.publication_id:
        details = api_get(f"publication-details/by-publication/{args.publication_id}") or []
        tg_details = [d for d in details if d.get("platform") == "telegram"]
        if not tg_details:
            _logger.log(f"[FAIL] No Telegram detail for publication #{args.publication_id}")
            finalize_log(_logger, "telegram_publisher", _API_ROOT, api_request)
            sys.exit(1)
        for detail in tg_details:
            process_detail(detail)
    else:
        pending = api_get("publication-details/pending?platform=telegram") or []
        _logger.log(f"Found {len(pending)} pending Telegram detail(s)")

        if not pending:
            _logger.log("[DONE] No pending Telegram details")
            finalize_log(_logger, "telegram_publisher", _API_ROOT, api_request)
            return

        if args.dry_run:
            for d in pending:
                pub_id = d.get("publication_id")
                _logger.log(f"  Detail #{d['id']} | pub #{pub_id} | scheduled: {d.get('scheduled_at')}")
            finalize_log(_logger, "telegram_publisher", _API_ROOT, api_request)
            return

        for detail in pending:
            process_detail(detail)

    _logger.log("[DONE] Telegram publisher finished")
    finalize_log(_logger, "telegram_publisher", _API_ROOT, api_request)


def publish(details, context):
    global _logger, _SETTINGS_CACHE
    _logger = context.get("logger")
    _SETTINGS_CACHE = context.get("settings_cache")
    for detail in details:
        process_detail(detail, context)


if __name__ == "__main__":
    main()
