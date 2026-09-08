#!/usr/bin/env python3
"""
BlueSky Publisher for CardVault.

Fetches pending BlueSky publication details from the CardVault API and publishes them
to BlueSky via AT Protocol (bsky.app API).

Steps per detail:
  1. Load publication and its files
  2. Build caption from product/inventory info
  3. Download images to temp files
  4. Upload images to BlueSky blob storage
  5. Create post with embedded images
  6. Update detail status, save post URI/permalink

Usage:
  python bluesky_publisher.py                        # publish all pending
  python bluesky_publisher.py --publication-id 42    # publish specific
  python bluesky_publisher.py --dry-run              # only list pending

Environment (tasks .env):
  CARDVAULT_API_BASE, CARDVAULT_API_USERNAME, CARDVAULT_API_PASSWORD

Settings (via CardVault API):
  task.publisher.bluesky.identifier  → BlueSky handle (e.g., user.bsky.social)
  task.publisher.bluesky.password    → BlueSky app password
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


def get_bluesky_config():
    return {
        "identifier": get_setting("task.publisher.bluesky.identifier") or "",
        "password": get_setting("task.publisher.bluesky.password") or "",
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
                content_type = resp.headers.get("Content-Type", "")
        except Exception as e:
            _logger and _logger.log(f"  [WARN] Fallo al descargar fichero {fid}: {e}")
            continue

        ext = ".jpg"
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


class BlueSkyAPI:
    BSKY_URL = "https://bsky.social/xrpc"

    def __init__(self, identifier, password):
        self.identifier = identifier
        self.password = password
        self.access_token = None
        self.did = None
        self._authenticate()

    def _authenticate(self):
        try:
            resp = requests.post(
                f"{self.BSKY_URL}/com.atproto.server.createSession",
                json={
                    "identifier": self.identifier,
                    "password": self.password,
                },
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            self.access_token = data.get("accessJwt")
            self.did = data.get("did")
            _logger and _logger.log(f"  BlueSky authenticated: {self.identifier} ({self.did})")
        except Exception as e:
            _logger and _logger.log(f"  [ERROR] BlueSky auth failed: {e}")
            raise

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def upload_blob(self, file_path, content_type="image/jpeg"):
        try:
            with open(file_path, "rb") as f:
                data = f.read()

            resp = requests.post(
                f"{self.BSKY_URL}/com.atproto.repo.uploadBlob",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": content_type,
                },
                data=data,
                timeout=60
            )
            resp.raise_for_status()
            result = resp.json()
            blob = result.get("blob")
            _logger and _logger.log(f"  Blob subido: {blob.get('ref', {}).get('$link', '')[:20]}...")
            return blob
        except Exception as e:
            _logger and _logger.log(f"  [ERROR] Subiendo blob: {e}")
            return None

    def create_post(self, text, images=None):
        try:
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            post_data = {
                "$type": "app.bsky.feed.post",
                "text": text,
                "createdAt": now,
            }

            if images:
                embed_images = []
                for img in images[:4]:
                    embed_images.append({
                        "alt": "",
                        "image": img,
                    })
                post_data["embed"] = {
                    "$type": "app.bsky.embed.images",
                    "images": embed_images,
                }

            resp = requests.post(
                f"{self.BSKY_URL}/com.atproto.repo.createRecord",
                headers=self._headers(),
                json={
                    "repo": self.did,
                    "collection": "app.bsky.feed.post",
                    "record": post_data,
                },
                timeout=30
            )
            resp.raise_for_status()
            result = resp.json()
            uri = result.get("uri")
            _logger and _logger.log(f"  Post creado: {uri}")
            return uri, None
        except Exception as e:
            _logger and _logger.log(f"  [ERROR] Creando post: {e}")
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
    if len(text) > 300:
        text = text[:297] + "..."
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
        _logger and _logger.log(f"Processing BlueSky detail #{detail_id} (pub #{pub_id}, inventories: {inv_ids})")
    else:
        _logger and _logger.log(f"Processing BlueSky detail #{detail_id} (pub #{pub_id}, manual)")
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
            caption = f"Publicacion #{pub_id} #CardVault #TCG"
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

    if not all_file_ids:
        error_msg = "No images for this publication"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        return

    api_patch(f"publication-details/{detail_id}", {"status": "processing"})

    _logger and _logger.log(f"  Downloading {len(all_file_ids)} file(s)...")
    tmp_files = download_images_to_temp(all_file_ids[:4])

    if not tmp_files:
        error_msg = "Could not download any images from API"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        return

    bluesky_cfg = get_bluesky_config()
    if not all([bluesky_cfg["identifier"], bluesky_cfg["password"]]):
        error_msg = "BlueSky credentials not configured"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        cleanup_temp_files(tmp_files)
        return

    bluesky = None
    uri = None
    error_msg = None

    try:
        bluesky = BlueSkyAPI(bluesky_cfg["identifier"], bluesky_cfg["password"])

        blobs = []
        for tmp_file in tmp_files:
            blob = bluesky.upload_blob(tmp_file)
            if blob:
                blobs.append(blob)

        if not blobs:
            error_msg = "No se pudieron subir imagenes a BlueSky"
        else:
            uri, error = bluesky.create_post(caption, blobs)
            if not uri:
                error_msg = error or "Error creando post en BlueSky"
    except Exception as e:
        error_msg = str(e)
        _logger and _logger.log(f"  [EXCEPTION] {error_msg}")
    finally:
        cleanup_temp_files(tmp_files)

    if uri:
        handle = bluesky_cfg["identifier"]
        rkey = uri.split("/")[-1]
        permalink = f"https://bsky.app/profile/{handle}/post/{rkey}"
        clean_permalink = permalink.split('?')[0].split('#')[0]
        update_data = {
            "status": "published",
            "published_at": datetime.now().isoformat(),
            "media_id": uri,
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

    parser = argparse.ArgumentParser(description="CardVault BlueSky Publisher")
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
    _logger = TaskLogger(log_dir, "bluesky_publisher")

    _logger.log(f"CardVault API: {API_BASE}")
    _logger.log(f"[OK] Authenticated  [{BUILD_VERSION}]")

    if args.publication_id:
        details = api_get(f"publication-details/by-publication/{args.publication_id}") or []
        bs_details = [d for d in details if d.get("platform") == "bluesky"]
        if not bs_details:
            _logger.log(f"[FAIL] No BlueSky detail for publication #{args.publication_id}")
            finalize_log(_logger, "bluesky_publisher", _API_ROOT, api_request)
            sys.exit(1)
        for detail in bs_details:
            process_detail(detail)
    else:
        pending = api_get("publication-details/pending?platform=bluesky") or []
        _logger.log(f"Found {len(pending)} pending BlueSky detail(s)")

        if not pending:
            _logger.log("[DONE] No pending BlueSky details")
            finalize_log(_logger, "bluesky_publisher", _API_ROOT, api_request)
            return

        if args.dry_run:
            for d in pending:
                pub_id = d.get("publication_id")
                _logger.log(f"  Detail #{d['id']} | pub #{pub_id} | scheduled: {d.get('scheduled_at')}")
            finalize_log(_logger, "bluesky_publisher", _API_ROOT, api_request)
            return

        for detail in pending:
            process_detail(detail)

    _logger.log("[DONE] BlueSky publisher finished")
    finalize_log(_logger, "bluesky_publisher", _API_ROOT, api_request)


def publish(details, context):
    global _logger, _SETTINGS_CACHE
    _logger = context.get("logger")
    _SETTINGS_CACHE = context.get("settings_cache")
    for detail in details:
        process_detail(detail, context)


if __name__ == "__main__":
    main()
