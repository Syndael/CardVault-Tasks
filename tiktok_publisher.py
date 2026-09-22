#!/usr/bin/env python3
"""
TikTok Publisher for CardVault.

Fetches pending TikTok publication details from the CardVault API and publishes them
to TikTok via TikTok Content Posting API.

Steps per detail:
  1. Load publication and its files
  2. Build caption from product/inventory info
  3. Download first video/image to temp file
  4. Upload media to TikTok
  5. Create post
  6. Update detail status, save post ID/permalink

Usage:
  python tiktok_publisher.py                        # publish all pending
  python tiktok_publisher.py --publication-id 42    # publish specific
  python tiktok_publisher.py --dry-run              # only list pending

Environment (tasks .env):
  CARDVAULT_API_BASE, CARDVAULT_API_USERNAME, CARDVAULT_API_PASSWORD

Settings (via CardVault API):
  task.publisher.tiktok.client.key      → TikTok Client Key
  task.publisher.tiktok.client.secret   → TikTok Client Secret
  task.publisher.tiktok.access.token    → TikTok Access Token
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


def get_tiktok_config():
    return {
        "client_key": get_setting("task.publisher.tiktok.client.key") or "",
        "client_secret": get_setting("task.publisher.tiktok.client.secret") or "",
        "access_token": get_setting("task.publisher.tiktok.access.token") or "",
        "refresh_token": get_setting("task.publisher.tiktok.refresh.token") or "",
        "expires_at": get_setting("task.publisher.tiktok.expires.at") or "",
    }


def update_setting(key, value):
    return api_request("PATCH", f"settings/key/{key}", {"setting_value": value})


def refresh_tiktok_token(cfg=None):
    global _SETTINGS_CACHE
    if cfg is None:
        cfg = get_tiktok_config()
    
    if not cfg.get("refresh_token"):
        _logger and _logger.log("  [WARN] No hay refresh_token disponible")
        return False
    
    _logger and _logger.log("  Refrescando token de TikTok...")
    
    token_url = "https://open.tiktokapis.com/v2/oauth/token/"
    
    data = {
        "client_key": cfg["client_key"],
        "client_secret": cfg["client_secret"],
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh_token"],
    }
    
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cache-Control": "no-cache",
    }
    
    try:
        resp = requests.post(token_url, data=data, headers=headers, timeout=30)
        resp.raise_for_status()
        token_data = resp.json()
        
        if "access_token" not in token_data:
            _logger and _logger.log(f"  [ERROR] Respuesta inesperada al refrescar: {token_data}")
            return False
        
        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token", cfg["refresh_token"])
        expires_in = token_data.get("expires_in", 86400)
        
        from datetime import timedelta
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        expires_at_str = expires_at.isoformat()
        
        _logger and _logger.log(f"  Token refrescado (expira en {expires_in/3600:.1f}h)")
        
        update_setting("task.publisher.tiktok.access.token", access_token)
        if refresh_token:
            update_setting("task.publisher.tiktok.refresh.token", refresh_token)
        update_setting("task.publisher.tiktok.expires.at", expires_at_str)
        
        _SETTINGS_CACHE = None
        return True
        
    except Exception as e:
        _logger and _logger.log(f"  [ERROR] Fallo al refrescar token: {e}")
        return False


def ensure_valid_token():
    cfg = get_tiktok_config()
    
    if not cfg["access_token"]:
        _logger and _logger.log("  [ERROR] No hay access_token configurado")
        return None
    
    if cfg["expires_at"]:
        try:
            expires_dt = datetime.fromisoformat(cfg["expires_at"])
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            remaining = (expires_dt - now).total_seconds()
            
            if remaining < 300:
                _logger and _logger.log(f"  Token expira en {remaining/60:.0f}min, refrescando...")
                if not refresh_tiktok_token(cfg):
                    return None
                cfg = get_tiktok_config()
        except Exception as e:
            _logger and _logger.log(f"  [WARN] Error parseando expires_at: {e}")
    
    return cfg


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


class TikTokAPI:
    UPLOAD_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
    PUBLISH_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
    STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

    def __init__(self, client_key, client_secret, access_token, refresh_token=None, expires_at=None):
        self.client_key = client_key
        self.client_secret = client_secret
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at

    def _ensure_valid_token(self):
        if not self.expires_at:
            return True
        
        try:
            expires_dt = datetime.fromisoformat(self.expires_at)
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            remaining = (expires_dt - now).total_seconds()
            
            if remaining < 300:
                _logger and _logger.log("  Token expira pronto, refrescando...")
                cfg = {
                    "client_key": self.client_key,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                }
                if refresh_tiktok_token(cfg):
                    new_cfg = get_tiktok_config()
                    self.access_token = new_cfg["access_token"]
                    self.refresh_token = new_cfg.get("refresh_token")
                    self.expires_at = new_cfg.get("expires_at")
                    return True
                else:
                    return False
        except Exception as e:
            _logger and _logger.log(f"  [WARN] Error verificando token: {e}")
        
        return True

    def upload_video(self, video_path, caption=""):
        if not self._ensure_valid_token():
            _logger and _logger.log("  [ERROR] No se pudo validar/refrescar el token")
            return None
        
        try:
            file_size = os.path.getsize(video_path)
            
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            }
            
            init_data = {
                "post_info": {
                    "title": caption[:150] if caption else "",
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": file_size,
                }
            }
            
            resp = requests.post(
                self.PUBLISH_URL,
                headers=headers,
                json=init_data,
                timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            
            publish_id = data.get("data", {}).get("publish_id")
            upload_url = data.get("data", {}).get("upload_url")
            
            if not upload_url:
                _logger and _logger.log(f"  [ERROR] No upload_url received: {data}")
                return None
            
            with open(video_path, "rb") as f:
                upload_resp = requests.put(
                    upload_url,
                    data=f,
                    headers={"Content-Type": "video/mp4"},
                    timeout=300
                )
                upload_resp.raise_for_status()
            
            _logger and _logger.log(f"  Video subido a TikTok, publish_id={publish_id}")
            return publish_id
            
        except Exception as e:
            _logger and _logger.log(f"  [ERROR] Subiendo video a TikTok: {e}")
            return None

    def check_status(self, publish_id):
        try:
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            }
            
            resp = requests.post(
                self.STATUS_URL,
                headers=headers,
                json={"publish_id": publish_id},
                timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            
            status = data.get("data", {}).get("status")
            return status, data
            
        except Exception as e:
            _logger and _logger.log(f"  [ERROR] Checking status: {e}")
            return None, None


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
    if len(text) > 150:
        text = text[:147] + "..."
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
        _logger and _logger.log(f"Processing TikTok detail #{detail_id} (pub #{pub_id}, inventories: {inv_ids})")
    else:
        _logger and _logger.log(f"Processing TikTok detail #{detail_id} (pub #{pub_id}, manual)")
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

    _logger and _logger.log(f"  Downloading first image...")
    tmp_file = download_first_image_to_temp(all_file_ids)

    if not tmp_file:
        error_msg = "Could not download image from API"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        return

    tiktok_cfg = ensure_valid_token()
    if not tiktok_cfg:
        error_msg = "No se pudo obtener un token valido de TikTok. Ejecuta tiktok_oauth_helper.py --authorize"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        cleanup_temp_file(tmp_file)
        return
    
    if not all([tiktok_cfg["client_key"], tiktok_cfg["client_secret"], tiktok_cfg["access_token"]]):
        error_msg = "TikTok credentials not configured"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        cleanup_temp_file(tmp_file)
        return

    tiktok = TikTokAPI(
        tiktok_cfg["client_key"], 
        tiktok_cfg["client_secret"], 
        tiktok_cfg["access_token"],
        tiktok_cfg.get("refresh_token"),
        tiktok_cfg.get("expires_at")
    )

    publish_id = None
    error_msg = None

    try:
        publish_id = tiktok.upload_video(tmp_file, caption)
        if not publish_id:
            error_msg = "Error subiendo video a TikTok"
    except Exception as e:
        error_msg = str(e)
        _logger and _logger.log(f"  [EXCEPTION] {error_msg}")
    finally:
        cleanup_temp_file(tmp_file)

    if publish_id:
        permalink = f"https://www.tiktok.com/@user/video/{publish_id}"
        clean_permalink = permalink.split('?')[0].split('#')[0]
        update_data = {
            "status": "published",
            "published_at": datetime.now().isoformat(),
            "media_id": publish_id,
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

    parser = argparse.ArgumentParser(description="CardVault TikTok Publisher")
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
    _logger = TaskLogger(log_dir, "tiktok_publisher")

    _logger.log(f"CardVault API: {API_BASE}")
    _logger.log(f"[OK] Authenticated  [{BUILD_VERSION}]")

    if args.publication_id:
        details = api_get(f"publication-details/by-publication/{args.publication_id}") or []
        tt_details = [d for d in details if d.get("platform") == "tiktok"]
        if not tt_details:
            _logger.log(f"[FAIL] No TikTok detail for publication #{args.publication_id}")
            finalize_log(_logger, "tiktok_publisher", _API_ROOT, api_request)
            sys.exit(1)
        for detail in tt_details:
            process_detail(detail)
    else:
        pending = api_get("publication-details/pending?platform=tiktok") or []
        _logger.log(f"Found {len(pending)} pending TikTok detail(s)")

        if not pending:
            _logger.log("[DONE] No pending TikTok details")
            finalize_log(_logger, "tiktok_publisher", _API_ROOT, api_request)
            return

        if args.dry_run:
            for d in pending:
                pub_id = d.get("publication_id")
                _logger.log(f"  Detail #{d['id']} | pub #{pub_id} | scheduled: {d.get('scheduled_at')}")
            finalize_log(_logger, "tiktok_publisher", _API_ROOT, api_request)
            return

        for detail in pending:
            process_detail(detail)

    _logger.log("[DONE] TikTok publisher finished")
    finalize_log(_logger, "tiktok_publisher", _API_ROOT, api_request)


def publish(details, context):
    global _logger, _SETTINGS_CACHE
    _logger = context.get("logger")
    _SETTINGS_CACHE = context.get("settings_cache")
    for detail in details:
        process_detail(detail, context)


if __name__ == "__main__":
    main()
