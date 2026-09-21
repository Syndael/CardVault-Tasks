#!/usr/bin/env python3
"""
Threads Publisher for CardVault.

Fetches pending Threads publication details from the CardVault API and publishes them
to Threads via Threads Graph API (Meta). Uses Cloudflare R2 for temporary image storage
to get public URLs.

Steps per detail:
  1. Load publication and its files
  2. Build caption from product/inventory info
  3. Download images to temp files
  4. Upload images to R2 (temporary public URLs)
  5. Publish via Threads Graph API
  6. Delete temp files from R2
  7. Update detail status, save post ID/permalink

Usage:
  python threads_publisher.py                        # publish all pending
  python threads_publisher.py --publication-id 42    # publish specific
  python threads_publisher.py --dry-run              # only list pending

Environment (tasks .env):
  CARDVAULT_API_BASE, CARDVAULT_API_USERNAME, CARDVAULT_API_PASSWORD

Settings (via CardVault API):
  task.publisher.threads.access.token    → Threads Access Token
  task.publisher.threads.user.id         → Threads User ID
  task.publisher.threads.app.id          → Facebook App ID
  task.publisher.threads.app.secret      → Facebook App Secret
  task.publisher.r2.endpoint             → Cloudflare R2 endpoint
  task.publisher.r2.bucket               → R2 bucket name
  task.publisher.r2.access.key           → R2 access key
  task.publisher.r2.secret.key           → R2 secret key
  task.publisher.r2.public.url           → R2 public URL
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

import boto3
import requests
from botocore.exceptions import ClientError
from dotenv import load_dotenv

from task_logger import TaskLogger, finalize_log
from caption_resolver import resolve_caption_tags
from task_notifier import notify_unresolved_tags

load_dotenv()

BUILD_VERSION = "v1.5"

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


def get_threads_config():
    return {
        "access_token": get_setting("task.publisher.threads.access.token") or "",
        "user_id": get_setting("task.publisher.threads.user.id") or "",
        "app_id": get_setting("task.publisher.threads.app.id") or "",
        "app_secret": get_setting("task.publisher.threads.app.secret") or "",
        "username": get_setting("task.publisher.threads.username") or "",
    }


def refresh_threads_token(app_secret, current_token):
    try:
        resp = requests.post(
            "https://graph.threads.net/access_token",
            data={
                "grant_type": "th_exchange_token",
                "client_secret": app_secret,
                "access_token": current_token,
            },
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        new_token = data.get("access_token")
        if new_token:
            api_patch("settings/by-key/task.publisher.threads.access.token/", {"setting_value": new_token})
            _logger and _logger.log(f"  [OK] Token de Threads refrescado")
            return new_token
    except Exception as e:
        _logger and _logger.log(f"  [WARN] No se pudo refrescar token: {e}")
    return None


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


class R2Storage:
    def __init__(self, endpoint, bucket_name, access_key, secret_key, public_url):
        self.endpoint = endpoint
        self.bucket_name = bucket_name
        self.access_key = access_key
        self.secret_key = secret_key
        self.public_url = public_url.rstrip("/")

        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name="auto",
        )

    def upload_image(self, local_path, content_type="image/jpeg"):
        filename = os.path.basename(local_path)
        key = f"threads-temp/{int(time.time())}_{filename}"
        try:
            with open(local_path, "rb") as f:
                self.client.put_object(
                    Bucket=self.bucket_name,
                    Key=key,
                    Body=f.read(),
                    ContentType=content_type,
                )
            
            # Generar URL firmada válida por 1 hora
            try:
                public_url = self.client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': self.bucket_name, 'Key': key},
                    ExpiresIn=3600  # 1 hora
                )
            except Exception:
                # Fallback a URL pública si no se puede firmar
                public_url = f"{self.public_url}/{key}"
            
            _logger and _logger.log(f"  Imagen subida a R2: {key}")
            return public_url, key
        except Exception as e:
            _logger and _logger.log(f"  [ERROR] Subiendo a R2: {e}")
            return None, None

    def delete_object(self, key):
        try:
            self.client.delete_object(Bucket=self.bucket_name, Key=key)
            _logger and _logger.log(f"  Objeto eliminado de R2: {key}")
            return True
        except ClientError as e:
            _logger and _logger.log(f"  [WARN] Error eliminando de R2: {e}")
            return False


def get_r2_config():
    return {
        "endpoint": get_setting("task.publisher.r2.endpoint") or "",
        "bucket": get_setting("task.publisher.r2.bucket") or "",
        "access_key": get_setting("task.publisher.r2.access.key") or "",
        "secret_key": get_setting("task.publisher.r2.secret.key") or "",
        "public_url": get_setting("task.publisher.r2.public.url") or "",
    }


def create_r2_client():
    cfg = get_r2_config()
    if not all([cfg["endpoint"], cfg["bucket"], cfg["access_key"], cfg["secret_key"], cfg["public_url"]]):
        _logger and _logger.log("[FAIL] R2 config incompleta. Configure r2.endpoint, r2.bucket, r2.access.key, r2.secret.key, r2.public.url")
        return None
    try:
        r2 = R2Storage(cfg["endpoint"], cfg["bucket"], cfg["access_key"], cfg["secret_key"], cfg["public_url"])
        return r2
    except Exception as e:
        _logger and _logger.log(f"[FAIL] Error inicializando R2: {e}")
        return None


class ThreadsGraphAPI:
    GRAPH_URL = "https://graph.threads.net/v1.0"

    def __init__(self, access_token, user_id, app_secret=None):
        self.access_token = access_token
        self.user_id = user_id
        self.app_secret = app_secret
        self.max_retries = 3

    def _make_request(self, endpoint, params=None, method="POST"):
        url = f"{self.GRAPH_URL}/{endpoint}"
        if params is None:
            params = {}
        params["access_token"] = self.access_token

        for attempt in range(self.max_retries):
            try:
                if method == "POST":
                    r = requests.post(url, data=params, timeout=60)
                else:
                    r = requests.get(url, params=params, timeout=60)
                r.raise_for_status()
                return r.json()
            except requests.exceptions.HTTPError as e:
                if r.status_code == 400 and self.app_secret:
                    error_data = r.json() if r else {}
                    error_msg = error_data.get("error", {}).get("message", "")
                    if "expired" in error_msg.lower() or "session" in error_msg.lower():
                        _logger and _logger.log(f"  [INFO] Token expirado, intentando refrescar...")
                        new_token = refresh_threads_token(self.app_secret, self.access_token)
                        if new_token:
                            self.access_token = new_token
                            params["access_token"] = new_token
                            continue
                    else:
                        _logger and _logger.log(f"  [ERROR] Threads API error: {error_msg}")
                _logger and _logger.log(f"  Request fallo (intento {attempt + 1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(2)
                else:
                    raise
            except requests.exceptions.RequestException as e:
                _logger and _logger.log(f"  Request fallo (intento {attempt + 1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(2)
                else:
                    raise
        return None

    def _wait_for_container(self, container_id, timeout=120):
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                result = self._make_request(
                    container_id,
                    params={"fields": "status"},
                    method="GET",
                )
                status = result.get("status", "")
                if status == "FINISHED":
                    return True
                elif status == "ERROR":
                    _logger and _logger.log(f"  Container error: {result.get('error_message', 'unknown')}")
                    return False
                time.sleep(2)
            except Exception as e:
                _logger and _logger.log(f"  [WARN] Error verificando container: {e}")
                time.sleep(2)
        _logger and _logger.log(f"  [ERROR] Timeout esperando container {container_id}")
        return False

    def get_user_info(self):
        return self._make_request(
            self.user_id,
            params={"fields": "id,username"},
            method="GET",
        ) or {}

    def publish_text(self, text):
        try:
            container = self._make_request(
                f"{self.user_id}/threads",
                params={
                    "media_type": "TEXT",
                    "text": text,
                },
            )
            container_id = container.get("id")
            if not container_id:
                return None, None, "No se recibio container_id"

            if not self._wait_for_container(container_id):
                return None, None, "Container no finalizo"

            result = self._make_request(
                f"{self.user_id}/threads_publish",
                params={"creation_id": container_id},
            )
            post_id = result.get("id")
            _logger and _logger.log(f"  Texto publicado: post_id={post_id}")
            return post_id, None, None
        except Exception as e:
            return None, None, str(e)

    def publish_image(self, image_url, caption=""):
        try:
            container = self._make_request(
                f"{self.user_id}/threads",
                params={
                    "media_type": "IMAGE",
                    "image_url": image_url,
                    "text": caption,
                },
            )
            container_id = container.get("id")
            if not container_id:
                return None, None, "No se recibio container_id"

            if not self._wait_for_container(container_id):
                return None, None, "Container no finalizo"

            result = self._make_request(
                f"{self.user_id}/threads_publish",
                params={"creation_id": container_id},
            )
            post_id = result.get("id")
            _logger and _logger.log(f"  Imagen publicada: post_id={post_id}")
            return post_id, None, None
        except Exception as e:
            return None, None, str(e)

    def publish_carousel(self, image_urls, caption=""):
        try:
            if len(image_urls) < 2:
                return self.publish_image(image_urls[0], caption)

            children_ids = []
            for image_url in image_urls[:10]:
                container = self._make_request(
                    f"{self.user_id}/threads",
                    params={
                        "media_type": "IMAGE",
                        "image_url": image_url,
                        "is_carousel_item": "true",
                    },
                )
                child_id = container.get("id")
                if child_id:
                    children_ids.append(child_id)

            if not children_ids:
                return None, None, "No se pudieron crear containers hijos"

            for child_id in children_ids:
                if not self._wait_for_container(child_id):
                    return None, None, f"Container hijo {child_id} no finalizo"

            parent_container = self._make_request(
                f"{self.user_id}/threads",
                params={
                    "media_type": "CAROUSEL",
                    "children": ",".join(children_ids),
                    "text": caption,
                },
            )
            parent_id = parent_container.get("id")
            if not parent_id:
                return None, None, "No se recibio parent_id"

            if not self._wait_for_container(parent_id):
                return None, None, "Container padre no finalizo"

            result = self._make_request(
                f"{self.user_id}/threads_publish",
                params={"creation_id": parent_id},
            )
            post_id = result.get("id")
            _logger and _logger.log(f"  Carousel publicado: post_id={post_id}")
            return post_id, None, None
        except Exception as e:
            return None, None, str(e)


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
    if len(text) > 500:
        text = text[:497] + "..."
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
        _logger and _logger.log(f"Processing Threads detail #{detail_id} (pub #{pub_id}, inventories: {inv_ids})")
    else:
        _logger and _logger.log(f"Processing Threads detail #{detail_id} (pub #{pub_id}, manual)")
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

    threads_cfg = get_threads_config()
    if not all([threads_cfg["access_token"], threads_cfg["user_id"]]):
        error_msg = "Threads credentials not configured"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        return

    threads = ThreadsGraphAPI(threads_cfg["access_token"], threads_cfg["user_id"], threads_cfg["app_secret"])

    _logger and _logger.log(f"  Downloading {len(all_file_ids)} file(s)...")
    tmp_files = download_images_to_temp(all_file_ids[:10])

    if not tmp_files:
        error_msg = "Could not download any images from API"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        return

    r2 = context.get("r2") if context else None
    if not r2:
        r2 = create_r2_client()
    if not r2:
        error_msg = "R2 not available"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        cleanup_temp_files(tmp_files)
        return

    image_urls = []
    r2_keys = []

    post_id = None
    error_msg = None

    try:
        for tmp_file in tmp_files:
            url, key = r2.upload_image(tmp_file)
            if url:
                image_urls.append(url)
                r2_keys.append(key)
            else:
                for k in r2_keys:
                    r2.delete_object(k)
                error_msg = "Error subiendo imagenes a R2"
                break

        if not error_msg:
            if len(image_urls) == 1:
                post_id, _, error = threads.publish_image(image_urls[0], caption)
            else:
                post_id, _, error = threads.publish_carousel(image_urls, caption)

            if not post_id:
                error_msg = error or "Error publicando en Threads"
    except Exception as e:
        error_msg = str(e)
        _logger and _logger.log(f"  [EXCEPTION] {error_msg}")
    finally:
        for key in r2_keys:
            r2.delete_object(key)
        cleanup_temp_files(tmp_files)

    if post_id:
        username = threads_cfg.get("username") or ""
        if not username:
            try:
                user_info = threads.get_user_info()
                username = user_info.get("username", "user")
            except Exception:
                username = "user"
        permalink = f"https://www.threads.net/@{username}/post/{post_id}"
        clean_permalink = permalink.split('?')[0].split('#')[0]
        update_data = {
            "status": "published",
            "published_at": datetime.now().isoformat(),
            "media_id": post_id,
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

    parser = argparse.ArgumentParser(description="CardVault Threads Publisher")
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
    _logger = TaskLogger(log_dir, "threads_publisher")

    _logger.log(f"CardVault API: {API_BASE}")
    _logger.log(f"[OK] Authenticated  [{BUILD_VERSION}]")

    if args.publication_id:
        details = api_get(f"publication-details/by-publication/{args.publication_id}") or []
        th_details = [d for d in details if d.get("platform") == "threads"]
        if not th_details:
            _logger.log(f"[FAIL] No Threads detail for publication #{args.publication_id}")
            finalize_log(_logger, "threads_publisher", _API_ROOT, api_request)
            sys.exit(1)
        for detail in th_details:
            process_detail(detail)
    else:
        pending = api_get("publication-details/pending?platform=threads") or []
        _logger.log(f"Found {len(pending)} pending Threads detail(s)")

        if not pending:
            _logger.log("[DONE] No pending Threads details")
            finalize_log(_logger, "threads_publisher", _API_ROOT, api_request)
            return

        if args.dry_run:
            for d in pending:
                pub_id = d.get("publication_id")
                _logger.log(f"  Detail #{d['id']} | pub #{pub_id} | scheduled: {d.get('scheduled_at')}")
            finalize_log(_logger, "threads_publisher", _API_ROOT, api_request)
            return

        for detail in pending:
            process_detail(detail)

    _logger.log("[DONE] Threads publisher finished")
    finalize_log(_logger, "threads_publisher", _API_ROOT, api_request)


def publish(details, context):
    global _logger, _SETTINGS_CACHE
    _logger = context.get("logger")
    _SETTINGS_CACHE = context.get("settings_cache")
    for detail in details:
        process_detail(detail, context)


if __name__ == "__main__":
    main()
