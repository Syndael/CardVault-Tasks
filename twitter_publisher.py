#!/usr/bin/env python3
"""
Twitter/X Publisher for CardVault.

Fetches pending Twitter publication details from the CardVault API and publishes them
to Twitter/X via Twitter API v2.

Steps per detail:
  1. Load publication and its files
  2. Build tweet text from product/inventory info
  3. Download images to temp files
  4. Upload media to Twitter
  5. Create tweet with media
  6. Update detail status, save tweet ID/permalink

Usage:
  python twitter_publisher.py                        # publish all pending
  python twitter_publisher.py --publication-id 42    # publish specific
  python twitter_publisher.py --dry-run              # only list pending

Environment (tasks .env):
  CARDVAULT_API_BASE, CARDVAULT_API_USERNAME, CARDVAULT_API_PASSWORD

Settings (via CardVault API):
  task.publisher.twitter.api.key         → Twitter API Key
  task.publisher.twitter.api.secret      → Twitter API Secret
  task.publisher.twitter.access.token    → Twitter Access Token
  task.publisher.twitter.access.secret   → Twitter Access Token Secret
  task.publisher.twitter.bearer.token    → Twitter Bearer Token
"""

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import requests
from requests_oauthlib import OAuth1
from dotenv import load_dotenv

from task_logger import TaskLogger, finalize_log
from caption_resolver import resolve_caption_tags
from task_notifier import notify_unresolved_tags

load_dotenv()

BUILD_VERSION = "v1.2"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_API_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "CardVault-API"))

_logger: TaskLogger | None = None

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

_TOKEN: str | None = None
_TOKEN_EXPIRES: datetime | None = None

_SETTINGS_CACHE: dict | None = None


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


def get_twitter_config():
    return {
        "api_key": get_setting("task.publisher.twitter.api.key") or "",
        "api_secret": get_setting("task.publisher.twitter.api.secret") or "",
        "access_token": get_setting("task.publisher.twitter.access.token") or "",
        "access_secret": get_setting("task.publisher.twitter.access.secret") or "",
        "bearer_token": get_setting("task.publisher.twitter.bearer.token") or "",
    }


def download_images_to_temp(file_ids):
    tmp_files = []
    for fid in file_ids:
        url = f"{API_BASE.rstrip('/')}/product-catalog/files/{fid}/content"
        token = _get_token()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
        except Exception as e:
            _logger and _logger.log(f"  [WARN] Fallo al descargar fichero {fid}: {e}")
            continue

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
        tmp_files.append(tmp.name)
        _logger and _logger.log(f"  Descargado fichero {fid} ({len(data)} bytes)")
    return tmp_files


def cleanup_temp_files(tmp_files):
    for f in tmp_files:
        try:
            os.unlink(f)
        except Exception:
            pass


class TwitterAPI:
    UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
    TWEET_URL = "https://api.twitter.com/2/tweets"

    def __init__(self, api_key, api_secret, access_token, access_secret):
        self.auth = OAuth1(api_key, api_secret, access_token, access_secret, signature_type='body')

    def upload_media(self, image_path):
        try:
            with open(image_path, "rb") as f:
                files = {"media_data": f.read()}
            import base64
            with open(image_path, "rb") as f:
                media_data = base64.b64encode(f.read()).decode("utf-8")
            resp = requests.post(
                self.UPLOAD_URL,
                auth=self.auth,
                data={"media_data": media_data},
                timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            media_id = data.get("media_id_string")
            _logger and _logger.log(f"  Media subida: {media_id}")
            return media_id
        except Exception as e:
            _logger and _logger.log(f"  [ERROR] Subiendo media a Twitter: {e}")
            return None

    def create_tweet(self, text, media_ids=None):
        try:
            payload = {"text": text}
            if media_ids:
                payload["media"] = {"media_ids": media_ids}
            resp = requests.post(
                self.TWEET_URL,
                auth=self.auth,
                json=payload,
                timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            tweet_id = data.get("data", {}).get("id")
            _logger and _logger.log(f"  Tweet creado: {tweet_id}")
            return tweet_id, None
        except Exception as e:
            _logger and _logger.log(f"  [ERROR] Creando tweet: {e}")
            return None, str(e)


def build_tweet_text(inv, product_name, collection_name):
    parts = [f"{product_name}"]
    if collection_name:
        parts.append(f"Coleccion: {collection_name}")
    lang = inv.get("language", {})
    if lang and lang.get("name"):
        parts.append(f"Idioma: {lang['name']}")
    cond = inv.get("condition", {})
    if cond and cond.get("name"):
        parts.append(f"Estado: {cond['name']}")

    text = " | ".join(parts)
    if len(text) > 280:
        text = text[:277] + "..."
    return text


def process_detail(detail, context=None):
    detail_id = detail["id"]
    pub_id = detail["publication_id"]
    pub = detail.get("publication") or api_get(f"publications/{pub_id}") or {}
    caption = detail.get("resolved_caption") or detail.get("caption") or pub.get("caption") or ""
    existing_status = detail.get("status", "pending_publish")
    is_broadcast = detail.get("is_broadcast", False)

    if existing_status in ("published", "cancelled"):
        _logger and _logger.log(f"  [SKIP] Detail #{detail_id} already {existing_status}")
        return

    inventories = pub.get("inventories") or []
    inv_ids = [inv["id"] for inv in inventories if inv.get("id")]

    _logger and _logger.log(f"\n{'=' * 58}")
    if inv_ids:
        _logger and _logger.log(f"Processing Twitter detail #{detail_id} (pub #{pub_id}, inventories: {inv_ids})")
    else:
        _logger and _logger.log(f"Processing Twitter detail #{detail_id} (pub #{pub_id}, manual)")
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
            caption = build_tweet_text(inv_for_meta, product_name, collection_name)
        else:
            caption = f"Publicacion #{pub_id} #CardVault #TCG"
        _logger and _logger.log(f"  Auto-generated tweet text ({len(caption)} chars)")

    if len(caption) > 280:
        caption = caption[:277] + "..."

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
    if is_broadcast:
        _logger and _logger.log(f"  Mode: broadcast (text-only allowed)")

    if not all_file_ids and not is_broadcast:
        error_msg = "No images for this publication"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        return

    api_patch(f"publication-details/{detail_id}", {"status": "processing"})

    tmp_files = []
    if all_file_ids:
        _logger and _logger.log(f"  Downloading {len(all_file_ids)} file(s)...")
        tmp_files = download_images_to_temp(all_file_ids)

        if not tmp_files:
            error_msg = "Could not download any images from API"
            _logger and _logger.log(f"  [FAIL] {error_msg}")
            api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
            return

    twitter_cfg = get_twitter_config()
    if not all([twitter_cfg["api_key"], twitter_cfg["api_secret"], twitter_cfg["access_token"], twitter_cfg["access_secret"]]):
        error_msg = "Twitter credentials not configured"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        cleanup_temp_files(tmp_files)
        return

    twitter = TwitterAPI(twitter_cfg["api_key"], twitter_cfg["api_secret"], twitter_cfg["access_token"], twitter_cfg["access_secret"])

    tweet_id = None
    error_msg = None

    try:
        media_ids = []
        for image_path in tmp_files[:4]:
            media_id = twitter.upload_media(image_path)
            if media_id:
                media_ids.append(media_id)

        if tmp_files and not media_ids:
            error_msg = "No se pudieron subir imagenes a Twitter"
        else:
            tweet_id, error = twitter.create_tweet(caption, media_ids if media_ids else None)
            if not tweet_id:
                error_msg = error or "Error creando tweet"
    except Exception as e:
        error_msg = str(e)
        _logger and _logger.log(f"  [EXCEPTION] {error_msg}")
    finally:
        cleanup_temp_files(tmp_files)

    if tweet_id:
        permalink = f"https://twitter.com/i/status/{tweet_id}"
        clean_permalink = permalink.split('?')[0].split('#')[0]
        update_data = {
            "status": "published",
            "published_at": datetime.now().isoformat(),
            "media_id": tweet_id,
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

    parser = argparse.ArgumentParser(description="CardVault Twitter Publisher")
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
    _logger = TaskLogger(log_dir, "twitter_publisher")

    _logger.log(f"CardVault API: {API_BASE}")
    _logger.log(f"[OK] Authenticated  [{BUILD_VERSION}]")

    if args.publication_id:
        details = api_get(f"publication-details/by-publication/{args.publication_id}") or []
        tw_details = [d for d in details if d.get("platform") == "twitter"]
        if not tw_details:
            _logger.log(f"[FAIL] No Twitter detail for publication #{args.publication_id}")
            finalize_log(_logger, "twitter_publisher", _API_ROOT, api_request)
            sys.exit(1)
        for detail in tw_details:
            process_detail(detail)
    else:
        pending = api_get("publication-details/pending?platform=twitter") or []
        _logger.log(f"Found {len(pending)} pending Twitter detail(s)")

        if not pending:
            _logger.log("[DONE] No pending Twitter details")
            finalize_log(_logger, "twitter_publisher", _API_ROOT, api_request)
            return

        if args.dry_run:
            for d in pending:
                pub_id = d.get("publication_id")
                _logger.log(f"  Detail #{d['id']} | pub #{pub_id} | scheduled: {d.get('scheduled_at')}")
            finalize_log(_logger, "twitter_publisher", _API_ROOT, api_request)
            return

        for detail in pending:
            process_detail(detail)

    _logger.log("[DONE] Twitter publisher finished")
    finalize_log(_logger, "twitter_publisher", _API_ROOT, api_request)


def publish(details, context):
    global _logger, _SETTINGS_CACHE
    _logger = context.get("logger")
    _SETTINGS_CACHE = context.get("settings_cache")
    for detail in details:
        process_detail(detail, context)


if __name__ == "__main__":
    main()
