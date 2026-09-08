#!/usr/bin/env python3
"""
Production Instagram Publisher for CardVault.

Fetches pending publications from the CardVault API and publishes them
to Instagram via Instagram Graph API (official). Uses Cloudflare R2 for
temporary image storage to get public URLs.

Steps per publication:
   1. Load inventory item and its files (ordered, from publication_detail_file)
   2. Build caption from product/inventory info
   3. Download images to temp files
   4. Upload images to R2 (temporary public URLs)
   5. Publish via Instagram Graph API:
      - Single photo -> publish_photo
      - Multiple photos -> publish_album
   6. Share first photo to stories (with video + music via Graph API)
   7. Delete temp files from R2
   8. Update publication status, save Instagram media ID/permalink
   9. Mark inventory as posted_instagram = True

Usage:
  python instagram_publisher.py                        # publish all pending
  python instagram_publisher.py --publication-id 42    # publish specific
  python instagram_publisher.py --dry-run              # only list pending

Environment (tasks .env):
  CARDVAULT_API_BASE, CARDVAULT_API_USERNAME, CARDVAULT_API_PASSWORD

Settings (via CardVault API):
  task.publisher.instagram.access.token    → Instagram Graph API long-lived token
  task.publisher.instagram.user.id         → Instagram Business Account ID
  task.publisher.instagram.app.id          → Facebook App ID
  task.publisher.instagram.app.secret      → Facebook App Secret
  r2.endpoint               → Cloudflare R2 endpoint
  r2.bucket                 → R2 bucket name
  task.publisher.r2.access.key             → R2 access key
  task.publisher.r2.secret.key             → R2 secret key
  task.publisher.r2.public.url             → R2 public URL
  task.publisher.instagram.music.dir       → (optional) music directory for story videos
  task.publisher.instagram.enable.stories  → (optional, default "1") "0" to disable stories
  task.publisher.instagram.gif.dir         → (optional) gif directory for overlays
"""

import argparse
import json
import os
import random
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText

import boto3
import requests
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFilter

from task_logger import TaskLogger, finalize_log

load_dotenv()

BUILD_VERSION = "v5.1-graph-api"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_API_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "CardVault-API"))

_logger: TaskLogger | None = None

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

_TOKEN: str | None = None
_TOKEN_EXPIRES: datetime | None = None

_MAX_CAROUSEL_ITEMS = 10

_SETTINGS_CACHE: dict | None = None
_SMTP_CONFIG_CACHE: dict | None = None
_TELEGRAM_BOT_TOKEN: str | None = None

_TCG_FOLDER_MAP = {
    "Pokémon": "pokemon", "Pokemon": "pokemon", "Pokemon TCG": "pokemon",
    "Magic": "magic", "Magic: The Gathering": "magic", "MTG": "magic",
    "Yu-Gi-Oh!": "yugioh", "Yu-Gi-Oh": "yugioh", "YGO": "yugioh",
    "Digimon": "digimon", "Digimon TCG": "digimon",
    "One Piece": "onepiece", "One Piece TCG": "onepiece", "One Piece CG": "onepiece",
    "Dragon Ball": "dragonball", "Dragon Ball Super": "dragonball", "DBS": "dragonball", "Dragon Ball CG": "dragonball",
    "Lorcana": "lorcana", "Disney Lorcana": "lorcana",
    "Flesh and Blood": "fab", "FAB": "fab",
    "Weiss Schwarz": "weiss", "Weiss": "weiss",
    "Union Arena": "unionarena",
    "Battle Spirits": "battlespirits", "Battle Spirits Saga": "battlespirits",
    "Cardfight": "vanguard", "Cardfight Vanguard": "vanguard", "Vanguard": "vanguard",
    "Final Fantasy": "finalfantasy", "FFTCG": "finalfantasy",
    "Star Wars": "starwars", "Star Wars Unlimited": "starwars",
    "MetaZoo": "metazoo",
    "World of Warcraft TCG": "wow", "WoW TCG": "wow",
}

_DEV_MODE: bool = os.getenv("IG_DEV_MODE", "").lower() in ("1", "true", "yes")


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


def update_setting(key, value):
    return api_patch(f"settings/by-key/{key}", {"setting_value": value}) is not None


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
        if "video" in content_type:
            ext = ".mp4"
        elif "png" in content_type:
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
        key = f"ig-temp/{int(time.time())}_{filename}"
        try:
            with open(local_path, "rb") as f:
                self.client.put_object(
                    Bucket=self.bucket_name,
                    Key=key,
                    Body=f.read(),
                    ContentType=content_type,
                )
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


class InstagramGraphAPI:
    GRAPH_URL = "https://graph.facebook.com/v21.0"

    def __init__(self, access_token, ig_user_id):
        self.access_token = access_token
        self.ig_user_id = ig_user_id
        self.max_retries = 3
        self._token_valid = None

    def verify_token(self):
        if self._token_valid is not None:
            return self._token_valid
        try:
            result = self._make_request("me", params={"fields": "id,name"}, method="GET")
            self._token_valid = True
            _logger and _logger.log(f"  Token IG valido para: {result.get('name', '?')}")
            return True
        except Exception as e:
            self._token_valid = False
            error_msg = str(e)
            if "OAuthException" in error_msg or "Invalid OAuth" in error_msg:
                _logger and _logger.log(f"  [ERROR] Token IG expirado o invalido: {error_msg}")
            else:
                _logger and _logger.log(f"  [ERROR] Error verificando token IG: {error_msg}")
            return False

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
                    params={"fields": "status_code,status"},
                    method="GET",
                )
                status = result.get("status_code", "")
                if status == "FINISHED":
                    return True
                elif status == "ERROR":
                    _logger and _logger.log(f"  Container error: {result.get('status', 'unknown')}")
                    return False
                time.sleep(2)
            except Exception as e:
                _logger and _logger.log(f"  [WARN] Error verificando container: {e}")
                time.sleep(2)
        _logger and _logger.log(f"  [ERROR] Timeout esperando container {container_id}")
        return False

    def _get_media_shortcode(self, media_id):
        try:
            result = self._make_request(
                media_id,
                params={"fields": "shortcode,permalink"},
                method="GET",
            )
            shortcode = result.get("shortcode", "")
            if not shortcode:
                permalink = result.get("permalink", "")
                if permalink:
                    match = re.search(r"/p/([^/]+)/", permalink)
                    if match:
                        shortcode = match.group(1)
            return shortcode
        except Exception as e:
            _logger and _logger.log(f"  [WARN] Error obteniendo shortcode: {e}")
            return ""

    def publish_photo(self, image_url, caption=""):
        try:
            container = self._make_request(
                f"{self.ig_user_id}/media",
                params={
                    "image_url": image_url,
                    "caption": caption,
                    "media_type": "IMAGE",
                },
            )
            container_id = container.get("id")
            if not container_id:
                return None, None, "No se recibio container_id"

            if not self._wait_for_container(container_id):
                return None, None, "Container no finalizo"

            result = self._make_request(
                f"{self.ig_user_id}/media_publish",
                params={"creation_id": container_id},
            )
            media_id = result.get("id")
            shortcode = result.get("shortcode", "")
            if not shortcode and media_id:
                shortcode = self._get_media_shortcode(media_id)

            _logger and _logger.log(f"  Foto publicada: media_id={media_id}, shortcode={shortcode}")
            return shortcode, media_id, None
        except Exception as e:
            return None, None, str(e)

    def publish_album(self, image_urls, caption=""):
        try:
            if len(image_urls) < 2:
                return self.publish_photo(image_urls[0], caption)

            children_ids = []
            for image_url in image_urls[:10]:
                container = self._make_request(
                    f"{self.ig_user_id}/media",
                    params={
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
                f"{self.ig_user_id}/media",
                params={
                    "media_type": "CAROUSEL",
                    "children": ",".join(children_ids),
                    "caption": caption,
                },
            )
            parent_id = parent_container.get("id")
            if not parent_id:
                return None, None, "No se recibio parent_id"

            if not self._wait_for_container(parent_id):
                return None, None, "Container padre no finalizo"

            result = self._make_request(
                f"{self.ig_user_id}/media_publish",
                params={"creation_id": parent_id},
            )
            media_id = result.get("id")
            shortcode = result.get("shortcode", "")
            if not shortcode and media_id:
                shortcode = self._get_media_shortcode(media_id)

            _logger and _logger.log(f"  Album publicado: media_id={media_id}, shortcode={shortcode}")
            return shortcode, media_id, None
        except Exception as e:
            return None, None, str(e)

    def publish_story(self, image_url=None, video_url=None):
        try:
            params = {"media_type": "STORIES"}
            if video_url:
                params["video_url"] = video_url
                if image_url:
                    params["thumb_offset"] = 0
            elif image_url:
                params["image_url"] = image_url
            else:
                return None, None, "Se requiere image_url o video_url"

            container = self._make_request(
                f"{self.ig_user_id}/media",
                params=params,
            )
            container_id = container.get("id")
            if not container_id:
                return None, None, "No se recibio container_id para story"

            if not self._wait_for_container(container_id, timeout=180):
                return None, None, "Container de story no finalizo"

            result = self._make_request(
                f"{self.ig_user_id}/media_publish",
                params={"creation_id": container_id},
            )
            media_id = result.get("id")
            shortcode = result.get("shortcode", "")
            if not shortcode and media_id:
                shortcode = self._get_media_shortcode(media_id)

            _logger and _logger.log(f"  Story publicada: media_id={media_id}, shortcode={shortcode}")
            return shortcode, media_id, None
        except Exception as e:
            return None, None, str(e)


def refresh_ig_token(access_token, app_id, app_secret):
    try:
        url = "https://graph.instagram.com/refresh_access_token"
        params = {
            "grant_type": "ig_refresh_token",
            "access_token": access_token,
        }
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        new_token = data.get("access_token")
        expires_in = data.get("expires_in", 5184000)
        if new_token:
            _logger and _logger.log(f"  Token refrescado correctamente (valido {expires_in // 86400} dias)")
            return new_token, expires_in
        else:
            _logger and _logger.log(f"  [ERROR] No se recibio nuevo token: {data}")
            return None, None
    except requests.exceptions.RequestException as e:
        _logger and _logger.log(f"  [ERROR] Error refrescando token: {e}")
        return None, None


def try_refresh_token(cfg_data):
    _logger and _logger.log("Verificando token de Instagram...")
    ig = InstagramGraphAPI(cfg_data["access_token"], cfg_data["user_id"])

    if ig.verify_token():
        _logger and _logger.log("  Token valido, no es necesario refrescar")
        return True

    _logger and _logger.log("  [WARN] Token invalido o expirado, intentando refrescar...")
    new_token, expires_in = refresh_ig_token(
        cfg_data["access_token"], cfg_data["app_id"], cfg_data["app_secret"]
    )

    if new_token:
        if update_setting("task.publisher.instagram.access.token", new_token):
            _logger and _logger.log("  Token refrescado y guardado")
            cfg_data["access_token"] = new_token
            return True
        else:
            _logger and _logger.log("  [ERROR] Token refrescado pero no se pudo guardar")
            return False
    else:
        _logger and _logger.log("  [ERROR] No se pudo refrescar el token")
        return False


def get_ig_config():
    return {
        "access_token": get_setting("task.publisher.instagram.access.token") or "",
        "user_id": get_setting("task.publisher.instagram.user.id") or "",
        "app_id": get_setting("task.publisher.instagram.app.id") or "",
        "app_secret": get_setting("task.publisher.instagram.app.secret") or "",
    }


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
        _logger and _logger.log("[FAIL] R2 config incompleta. Configure r2.endpoint, r2.bucket, r2.access_key, r2.secret_key, r2.public_url")
        return None
    try:
        r2 = R2Storage(cfg["endpoint"], cfg["bucket"], cfg["access_key"], cfg["secret_key"], cfg["public_url"])
        return r2
    except Exception as e:
        _logger and _logger.log(f"[FAIL] Error inicializando R2: {e}")
        return None


def publish_instagram(r2, image_paths, caption):
    ig_cfg = get_ig_config()
    if not all([ig_cfg["access_token"], ig_cfg["user_id"]]):
        return None, None, None, "Credenciales IG incompletas. Configure instagram.access_token e instagram.user_id"

    ig = InstagramGraphAPI(ig_cfg["access_token"], ig_cfg["user_id"])
    if not ig.verify_token():
        return None, None, None, "Token de Instagram expirado o invalido. Regenera el token en Facebook Developers."

    image_urls = []
    r2_keys = []

    try:
        for image_path in image_paths:
            url, key = r2.upload_image(image_path)
            if url:
                image_urls.append(url)
                r2_keys.append(key)
            else:
                for k in r2_keys:
                    r2.delete_object(k)
                return None, None, None, "Error subiendo imagenes a R2"

        if len(image_urls) == 1:
            code, media_id, error = ig.publish_photo(image_urls[0], caption)
        else:
            code, media_id, error = ig.publish_album(image_urls, caption)

        if code:
            permalink = f"https://www.instagram.com/p/{code}/"
            _logger and _logger.log(f"  [OK] Publicado! Permalink: {permalink}")
            return code, media_id, permalink, None
        else:
            return None, None, None, error
    except Exception as e:
        return None, None, None, str(e)
    finally:
        for key in r2_keys:
            r2.delete_object(key)


def share_to_story(r2, image_path, collection_name, video_path=None, frame_path=None):
    ig_cfg = get_ig_config()
    if not all([ig_cfg["access_token"], ig_cfg["user_id"]]):
        _logger and _logger.log("  [ERROR] Credenciales IG incompletas para story")
        if video_path:
            return False, video_path
        return False, None

    ig = InstagramGraphAPI(ig_cfg["access_token"], ig_cfg["user_id"])
    if not ig.verify_token():
        _logger and _logger.log("  [ERROR] Token IG expirado, no se puede publicar story")
        if video_path:
            return False, video_path
        return False, None

    own_video = video_path is None
    if own_video:
        video_path, frame_path = _create_story_video(image_path, collection_name)
    if not video_path:
        _logger and _logger.log("  No se genero el video de story, se omite.")
        if frame_path and own_video:
            try:
                os.unlink(frame_path)
            except Exception:
                pass
        return False, None

    video_url, video_key = r2.upload_image(video_path, content_type="video/mp4")
    if not video_url:
        _logger and _logger.log("  [ERROR] Error subiendo video de story a R2")
        if own_video:
            os.unlink(video_path)
            if frame_path:
                os.unlink(frame_path)
        return False, video_path

    try:
        _logger and _logger.log("  Publicando story via Graph API...")
        code, media_id, error = ig.publish_story(video_url=video_url)

        if code:
            _logger and _logger.log(f"  [OK] Story publicada. shortcode={code}")
            if own_video:
                os.unlink(video_path)
                if frame_path:
                    os.unlink(frame_path)
            return True, None
        else:
            _logger and _logger.log(f"  [ERROR] Error publicando story: {error}")
            if own_video and frame_path:
                try:
                    os.unlink(frame_path)
                except Exception:
                    pass
            return False, video_path
    finally:
        if video_key:
            r2.delete_object(video_key)


def _pick_music(collection_name):
    music_dir = get_setting("task.publisher.instagram.music.dir")
    if not music_dir:
        return None
    if music_dir.startswith("./"):
        music_dir = os.path.join(_SCRIPT_DIR, music_dir[2:])
    if not os.path.isdir(music_dir):
        _logger and _logger.log(f"  [WARN] Directorio de musica no encontrado: {music_dir}")
        return None

    folder = _tcg_folder(collection_name)
    search_dir = os.path.join(music_dir, folder)
    if not os.path.isdir(search_dir):
        _logger and _logger.log(f"  Sin carpeta '{folder}' para '{collection_name}', usando default")
        search_dir = os.path.join(music_dir, "default")
    if not os.path.isdir(search_dir):
        search_dir = music_dir

    audio_exts = (".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac")
    songs = [os.path.join(search_dir, f) for f in os.listdir(search_dir)
             if f.lower().endswith(audio_exts)]
    if not songs:
        _logger and _logger.log(f"  [WARN] Sin archivos de audio en: {search_dir}")
        return None

    random.shuffle(songs)
    for chosen in songs:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", chosen],
                capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                _logger and _logger.log(f"  Audio descartado (invalido): {os.path.basename(chosen)}")
                continue
        except subprocess.TimeoutExpired:
            _logger and _logger.log(f"  Audio descartado (timeout): {os.path.basename(chosen)}")
            continue
        except Exception as e:
            _logger and _logger.log(f"  Audio descartado (error): {os.path.basename(chosen)}: {e}")
            continue
        _logger and _logger.log(f"  Musica seleccionada: {os.path.basename(chosen)} [{folder}]")
        return chosen

    _logger and _logger.log(f"  [WARN] Ningun audio valido en: {search_dir}")
    return None


def _tcg_folder(collection_name):
    return _TCG_FOLDER_MAP.get(collection_name, "default")


def _pick_overlay(collection_name):
    gif_dir = get_setting("task.publisher.instagram.gif.dir") or os.path.join(_SCRIPT_DIR, "gif")
    if gif_dir.startswith("./"):
        gif_dir = os.path.join(_SCRIPT_DIR, gif_dir[2:])
    if not os.path.isdir(gif_dir):
        return None

    folder = _tcg_folder(collection_name)
    search_dirs = [
        os.path.join(gif_dir, folder),
        os.path.join(gif_dir, "default"),
        gif_dir,
    ]

    img_exts = (".gif", ".png", ".jpg", ".jpeg", ".webp")
    candidates = []
    for sd in search_dirs:
        if not os.path.isdir(sd):
            continue
        candidates = [os.path.join(sd, f) for f in os.listdir(sd)
                      if f.lower().endswith(img_exts)]
        if candidates:
            break

    if not candidates:
        return None

    random.shuffle(candidates)
    for chosen in candidates:
        if chosen.lower().endswith(".gif"):
            try:
                result = subprocess.run(
                    ["ffmpeg", "-v", "error", "-stream_loop", "-1", "-i", chosen, "-t", "1", "-f", "null", "-"],
                    capture_output=True, text=True, timeout=8)
                if result.returncode != 0:
                    _logger and _logger.log(f"  Overlay descartado (invalido): {os.path.basename(chosen)}")
                    continue
            except Exception:
                _logger and _logger.log(f"  Overlay descartado (timeout/invalido): {os.path.basename(chosen)}")
                continue
        _logger and _logger.log(f"  Overlay seleccionado: {os.path.basename(chosen)}")
        return chosen

    _logger and _logger.log("  [WARN] Ningun overlay valido encontrado")
    return None


def _render_story_frame(image_path, heavy_blur=False):
    W, H = 1080, 1920
    HALF = (540, 960)
    FG_W, FG_H = 405, 720
    RADIUS = 25

    try:
        img = Image.open(image_path).convert("RGBA")
    except Exception:
        _logger and _logger.log(f"  [WARN] Cannot open image for story frame: {image_path}, using fallback")
        canvas = Image.new("RGBA", HALF, (20, 20, 30, 255))
        fd, frame_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        canvas = canvas.convert("RGB")
        canvas.save(frame_path, "JPEG", quality=90)
        return frame_path

    blur_bg = img.copy()
    blur_bg = blur_bg.resize(HALF, Image.LANCZOS)
    blur_bg = blur_bg.filter(ImageFilter.GaussianBlur(radius=15))

    canvas = Image.new("RGBA", HALF, (0, 0, 0, 255))
    canvas.paste(blur_bg, (0, 0))

    img.thumbnail((FG_W, FG_H), Image.LANCZOS)
    fg_w, fg_h = img.size
    fg_x = (HALF[0] - fg_w) // 2
    fg_y = (HALF[1] - fg_h) // 2

    if heavy_blur:
        img = img.filter(ImageFilter.GaussianBlur(radius=3))

    mask = Image.new("L", (fg_w, fg_h), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([(0, 0), (fg_w, fg_h)], radius=RADIUS, fill=255)

    canvas.paste(img, (fg_x, fg_y), mask)

    fd, frame_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    canvas = canvas.convert("RGB")
    canvas.save(frame_path, "JPEG", quality=90)
    return frame_path


def _compute_overlay_pos(overlay_path):
    W, H = 540, 960
    MARGIN = 10
    try:
        ovl = Image.open(overlay_path)
        w, h = ovl.size
    except Exception:
        return None, None, None, None

    target_ratio = random.uniform(0.12, 0.18)
    ov_area = W * H * target_ratio
    aspect = w / h if h > 0 else 1.0
    ow = int((ov_area * aspect) ** 0.5)
    oh = max(int(ow / aspect), 1)

    max_overlap = int(H * 0.15)
    max_y = max(max_overlap - oh, MARGIN)

    max_x = max(W - ow - MARGIN, MARGIN)
    ox = random.randint(MARGIN, max(max_x, MARGIN))
    oy = random.randint(MARGIN, max(max_y, MARGIN))

    return ox, oy, ow, oh


def _create_story_video(image_path, collection_name, output_path=None, heavy_blur=False):
    if not shutil.which("ffmpeg"):
        _logger and _logger.log("  FFmpeg no encontrado")
        return None, None

    music_path = _pick_music(collection_name)
    overlay_path = _pick_overlay(collection_name)
    if not music_path:
        _logger and _logger.log("  Generando story sin musica (video silencioso)")

    try:
        _logger and _logger.log("  Renderizando frame con Pillow...")
        frame_path = _render_story_frame(image_path, heavy_blur=heavy_blur)
        _logger and _logger.log(f"  Frame renderizado: {frame_path}")

        if output_path:
            video_path = output_path
        else:
            fd, video_path = tempfile.mkstemp(suffix=".mp4")
            os.close(fd)

        duration = 15
        cmd = ["ffmpeg", "-y"]
        inputs = 1
        filter_parts = []
        last_out = "[0:v]"

        cmd += ["-loop", "1", "-i", frame_path]

        if overlay_path:
            ox, oy, ow, oh = _compute_overlay_pos(overlay_path)
            if ox is None:
                overlay_path = None
                _logger and _logger.log("  Overlay no valido, omitiendo")
            else:
                _logger and _logger.log(f"  Overlay GIF pos=({ox},{oy}) size=({ow},{oh})")
                overlay_ext = os.path.splitext(overlay_path)[1].lower()
                loop_opt = "-stream_loop" if overlay_ext == ".gif" else "-loop"
                cmd += [loop_opt, "-1" if overlay_ext == ".gif" else "1", "-i", overlay_path]
                inputs += 1
                filter_parts.append(
                    f"[{inputs - 1}:v]scale={ow}:{oh},setsar=1[ov];"
                    f"{last_out}[ov]overlay={ox}:{oy}[out0]"
                )
                last_out = "[out0]"

        if music_path:
            cmd += ["-i", music_path]

        if filter_parts:
            vf = ";".join(filter_parts)
            if last_out == "[out0]":
                vf += f";{last_out}scale=1080:1920:flags=lanczos[outv]"
                last_out = "[outv]"
            else:
                vf += f";scale=1080:1920:flags=lanczos[outv]"
                last_out = "[outv]"
            cmd += ["-filter_complex", vf, "-map", last_out]
        else:
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
                    "-vf", "scale=1080:1920:flags=lanczos"]

        cmd += [
            "-pix_fmt", "yuv420p", "-t", str(duration),
            "-movflags", "+faststart",
        ]

        if music_path:
            audio_idx = inputs
            cmd += ["-map", f"{audio_idx}:a:0", "-shortest", "-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-an"]
        cmd += [video_path]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            _logger and _logger.log(f"  FFmpeg error: {result.stderr[-500:]}")
            if not output_path:
                try:
                    os.unlink(video_path)
                except Exception:
                    pass
            return None, frame_path

        if os.path.getsize(video_path) > 0:
            _logger and _logger.log(f"  Video generado: {video_path}")
            return video_path, frame_path
        return None, frame_path
    except subprocess.TimeoutExpired:
        _logger and _logger.log("  FFmpeg timeout")
        return None, None
    except Exception as e:
        _logger and _logger.log(f"  FFmpeg excepcion: {e}")
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
    notes = inv.get("notes")
    if notes and notes.strip():
        parts.append("")
        parts.append(notes.strip())

    if len(parts) == 1:
        parts.append("")

    parts.append("#CardVault #TCG #Coleccionismo")
    return "\n".join(parts)


def _load_smtp_config():
    global _SMTP_CONFIG_CACHE
    if _SMTP_CONFIG_CACHE is not None:
        return _SMTP_CONFIG_CACHE
    host = get_setting("smtp.host")
    port = get_setting("smtp.port")
    user = get_setting("smtp.username")
    pwd = get_setting("smtp.password")
    fr = get_setting("smtp.from")
    _SMTP_CONFIG_CACHE = {
        "host": host or "",
        "port": int(port) if port else 587,
        "user": user or "",
        "pass": pwd or "",
        "from": fr or "cardvault@localhost",
    }
    return _SMTP_CONFIG_CACHE


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


def send_telegram_photo(chat_id, photo_path, caption=""):
    global _TELEGRAM_BOT_TOKEN
    if _TELEGRAM_BOT_TOKEN is None:
        _TELEGRAM_BOT_TOKEN = get_setting("bot.telegram.token")
    bot_token = _TELEGRAM_BOT_TOKEN
    if not bot_token or not chat_id:
        return
    try:
        boundary = "boundary" + str(int(time.time() * 1000000))
        with open(photo_path, "rb") as fh:
            photo_data = fh.read()
        filename = os.path.basename(photo_path)
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode("utf-8") + photo_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

        url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=120):
            pass
        _logger and _logger.log(f"  [NOTIFY telegram] Foto enviada a {chat_id}")
    except Exception as e:
        _logger and _logger.log(f"  [NOTIFY telegram] error envio foto a {chat_id}: {e}")


def send_telegram_notification(message, chat_id):
    global _TELEGRAM_BOT_TOKEN
    if _TELEGRAM_BOT_TOKEN is None:
        _TELEGRAM_BOT_TOKEN = get_setting("bot.telegram.token")
    bot_token = _TELEGRAM_BOT_TOKEN
    if not bot_token or not chat_id:
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message[:4096],
        }).encode("utf-8")
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=10):
            pass
        _logger and _logger.log(f"  [NOTIFY telegram] Sent to {chat_id}")
    except Exception as e:
        _logger and _logger.log(f"  [NOTIFY telegram] error for {chat_id}: {e}")


def send_telegram_video(chat_id, video_path, caption=""):
    global _TELEGRAM_BOT_TOKEN
    if _TELEGRAM_BOT_TOKEN is None:
        _TELEGRAM_BOT_TOKEN = get_setting("bot.telegram.token")
    bot_token = _TELEGRAM_BOT_TOKEN
    if not bot_token or not chat_id:
        return
    try:
        boundary = "boundary" + str(int(time.time()))
        with open(video_path, "rb") as fh:
            video_data = fh.read()
        filename = os.path.basename(video_path)
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="video"; filename="{filename}"\r\n'
            f"Content-Type: video/mp4\r\n\r\n"
        ).encode("utf-8") + video_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

        url = f"https://api.telegram.org/bot{bot_token}/sendVideo"
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=120):
            pass
        _logger and _logger.log(f"  [NOTIFY telegram] Video enviado a {chat_id}")
    except Exception as e:
        _logger and _logger.log(f"  [NOTIFY telegram] error envio video a {chat_id}: {e}")


def _notify_owner(inv, subject, message):
    owner_id = inv.get("user_id")
    if not owner_id:
        return
    token = _get_token()
    if not token:
        return
    try:
        url = f"{API_BASE.rstrip('/')}/auth/user/{owner_id}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            owner = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return
    if owner:
        if owner.get("email"):
            send_email_notification(owner["email"], subject, message)
        if owner.get("telegram_id"):
            send_telegram_notification(message, owner["telegram_id"])


def process_detail(detail, r2):
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
        _logger and _logger.log(f"Processing detail #{detail_id} (pub #{pub_id}, inventories: {inv_ids})")
    else:
        _logger and _logger.log(f"Processing detail #{detail_id} (pub #{pub_id}, manual, no inventory)")
    _logger and _logger.log(f"{'=' * 58}")

    product_name = "Manual publication"
    collection_name = ""
    inv_for_meta = None

    all_file_ids = []
    for inv_id in inv_ids:
        inv = api_get(f"inventory/{inv_id}")
        if not inv:
            _logger and _logger.log(f"  [WARN] Inventory #{inv_id} not found, skipping")
            continue
        if inv_for_meta is None:
            inv_for_meta = inv
            product = inv.get("product") or {}
            collection = inv.get("collection") or {}
            product_name = product.get("name") or product.get("product_number", f"Product #{product.get('id', '?')}")
            collection_name = collection.get("name") or collection.get("code", "")

    pub_detail = api_get(f"files/by-publication/{pub_id}") or []
    detail_files = detail.get("files") or []
    all_file_ids = [f["file_id"] for f in detail_files if f.get("file_id")]

    if not caption:
        if inv_for_meta:
            caption = build_caption(inv_for_meta, product_name, collection_name)
        else:
            caption = f"Publicacion #{pub_id}\n\n#CardVault #TCG #Coleccionismo"
        _logger and _logger.log(f"  Auto-generated caption ({len(caption)} chars)")

    _logger and _logger.log(f"  Product: {product_name}")
    if collection_name:
        _logger and _logger.log(f"  Collection: {collection_name}")
    _logger and _logger.log(f"  Files: {len(all_file_ids)} file(s)")

    if len(all_file_ids) > _MAX_CAROUSEL_ITEMS:
        _logger and _logger.log(f"  [WARN] {len(all_file_ids)} images found, only first {_MAX_CAROUSEL_ITEMS} will be published")
        all_file_ids = all_file_ids[:_MAX_CAROUSEL_ITEMS]

    if not all_file_ids:
        error_msg = "No images for this publication"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publication-details/{detail_id}", {"status": "failed", "error_message": error_msg})
        return

    dev_mode = (_DEV_MODE or
                (get_setting("task.publisher.instagram.dev.mode") or "").strip() in ("1", "true", "yes"))

    if not dev_mode:
        api_patch(f"publication-details/{detail_id}", {"status": "processing"})

    _logger and _logger.log(f"  Downloading {len(all_file_ids)} file(s)...")
    tmp_files = download_images_to_temp(all_file_ids)

    if not tmp_files:
        error_msg = "Could not download any images from API"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publications/{pub_id}", {"status": "failed", "error_message": error_msg})
        return

    ig_code = None
    media_pk = None
    permalink = None
    error_msg = None
    story_video_path = None
    story_frame_path = None
    story_failed = False

    stories_enabled_str = get_setting("task.publisher.instagram.enable.stories")
    if stories_enabled_str is None or stories_enabled_str.strip() not in ("0", "false", "no"):
        first_is_video = tmp_files and tmp_files[0].lower().endswith(('.mp4', '.mov', '.webm'))
        first_image = next((f for f in tmp_files if not f.lower().endswith(('.mp4', '.mov', '.webm'))), tmp_files[0] if tmp_files else None)
        story_video_path, story_frame_path = _create_story_video(first_image, collection_name, heavy_blur=first_is_video)
        if not story_video_path:
            error_msg = "No se pudo generar el video de story (verificar FFmpeg, musica y gif)"
            _logger and _logger.log(f"  [FAIL] {error_msg}")

    try:
        if dev_mode:
            dev_base = os.path.join(_SCRIPT_DIR, "dev_output")
            dev_dir = os.path.join(os.path.abspath(dev_base), str(pub_id))
            os.makedirs(dev_dir, exist_ok=True)
            _logger and _logger.log(f"  [DEV] Guardando archivos en: {dev_dir}")

            for i, f in enumerate(tmp_files):
                ext = os.path.splitext(f)[1] or ".jpg"
                dest = os.path.join(dev_dir, f"media_{i + 1}{ext}")
                shutil.copy2(f, dest)
            caption_file = os.path.join(dev_dir, "caption.txt")
            with open(caption_file, "w", encoding="utf-8") as cf:
                cf.write(caption)
            meta_file = os.path.join(dev_dir, "meta.txt")
            with open(meta_file, "w", encoding="utf-8") as mf:
                mf.write(f"Publication ID: {pub_id}\n")
                mf.write(f"Product: {product_name}\n")
                mf.write(f"Collection: {collection_name}\n")
                mf.write(f"Inventories: {inv_ids}\n")
                mf.write(f"Total files: {len(tmp_files)}\n")
            if story_frame_path:
                shutil.copy2(story_frame_path, os.path.join(dev_dir, "story_frame.jpg"))
            if story_video_path:
                shutil.copy2(story_video_path, os.path.join(dev_dir, "story.mp4"))

            _logger and _logger.log(f"  [DEV] {len(tmp_files)} archivos + caption + meta guardados en {dev_dir}")

            ig_code = f"DEV_{pub_id}"
            permalink = dev_dir

        if not dev_mode:
            _logger and _logger.log(f"  Publishing to Instagram ({len(tmp_files)} file(s))...")
            ig_code, media_pk, permalink, error = publish_instagram(r2, tmp_files, caption)

            if ig_code and story_video_path:
                story_uploaded, story_video_fallback = share_to_story(
                    r2, tmp_files[0], collection_name,
                    video_path=story_video_path, frame_path=story_frame_path,
                )
                if not story_uploaded:
                    story_failed = True
                    if story_video_fallback and inv_for_meta:
                        owner_id = inv_for_meta.get("user_id")
                        if owner_id:
                            token = _get_token()
                            if token:
                                try:
                                    url = f"{API_BASE.rstrip('/')}/auth/user/{owner_id}"
                                    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
                                    with urllib.request.urlopen(req, timeout=10) as resp:
                                        owner = json.loads(resp.read().decode("utf-8"))
                                    if owner and owner.get("telegram_id"):
                                        send_telegram_video(owner["telegram_id"], story_video_fallback,
                                                            f"Story fallida para #{pub_id}: {product_name}")
                                except Exception:
                                    pass
                    if story_video_fallback:
                        try:
                            os.unlink(story_video_fallback)
                        except Exception:
                            pass
                else:
                    story_video_path = None
                    story_frame_path = None
            elif not ig_code and error:
                if not error_msg:
                    error_msg = error
    except Exception as e:
        error_msg = str(e)
        _logger and _logger.log(f"  [EXCEPTION] {error_msg}")
    finally:
        cleanup_temp_files(tmp_files)

    if ig_code and not dev_mode:
        clean_permalink = permalink.split('?')[0].split('#')[0] if permalink else None
        update_data = {
            "status": "published",
            "published_at": datetime.now().isoformat(),
            "media_id": media_pk or ig_code,
            "permalink": clean_permalink,
        }
        api_patch(f"publication-details/{detail_id}", update_data)
        for inv_id in inv_ids:
            api_patch(f"inventory/{inv_id}", {"posted_instagram": "1"})
            if clean_permalink:
                api_post("inventory-urls", {"inventory_id": inv_id, "url": clean_permalink})
        _logger and _logger.log(f"  [OK] Publication #{pub_id} (detail #{detail_id}) completed: {clean_permalink}")

        if story_failed:
            _logger and _logger.log("  [WARN] Story no se subio. Publicacion en IG correcta pero sin story.")

        notify_msg = f"Publicacion #{pub_id} completada!\nProducto: {product_name}\n{permalink}"
        if inv_for_meta:
            _notify_owner(inv_for_meta, "CardVault - Publicacion Instagram", notify_msg)
        else:
            _logger and _logger.log(f"  [INFO] No inventory owner to notify")
    elif ig_code and dev_mode:
        _logger and _logger.log(f"  [DEV] Publicacion #{pub_id} guardada en modo developer")
    else:
        if not dev_mode:
            api_patch(f"publication-details/{detail_id}", {
                "status": "failed",
                "error_message": error_msg or "Unknown error",
            })
        _logger and _logger.log(f"  [FAIL] Publication #{pub_id} (detail #{detail_id}) failed: {error_msg}")

        notify_msg = f"Publicacion #{pub_id} ERROR!\nProducto: {product_name}\nError: {error_msg}"
        if inv_for_meta:
            _notify_owner(inv_for_meta, "CardVault - ERROR Publicacion Instagram", notify_msg)

    for p in [story_video_path, story_frame_path]:
        if p and not dev_mode:
            try:
                os.unlink(p)
            except Exception:
                pass


def main():
    global _logger, _SETTINGS_CACHE

    parser = argparse.ArgumentParser(description="CardVault Instagram Publisher (Graph API)")
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
    _logger = TaskLogger(log_dir, "instagram_publisher")

    _logger.log(f"CardVault API: {API_BASE}")
    _logger.log(f"[OK] Authenticated  [{BUILD_VERSION}]")
    _logger.log(f"Settings loaded: {len(settings_list)} total, {len(settings_by_key)} by key")

    dev_mode = (_DEV_MODE or
                (settings_by_key.get("task.publisher.instagram.dev.mode") or get_setting("task.publisher.instagram.dev.mode") or "").strip() in ("1", "true", "yes"))

    r2 = None
    if not dev_mode:
        ig_cfg = get_ig_config()
        if not all([ig_cfg["access_token"], ig_cfg["user_id"]]):
            _logger.log("[FAIL] No Instagram Graph API credentials.")
            _logger.log("  Set settings 'instagram.access_token' and 'instagram.user_id' in CardVault")
            finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)
            sys.exit(1)

        _logger.log(f"IG User ID: {ig_cfg['user_id']}")

        r2 = create_r2_client()
        if not r2:
            _logger.log("[FAIL] R2 not configured. Set r2.* settings in CardVault")
            finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)
            sys.exit(1)
        _logger.log("[OK] R2 initialized")

        try_refresh_token(ig_cfg)
        _SETTINGS_CACHE = None
    else:
        dev_output = (settings_by_key.get("task.publisher.instagram.dev.output") or
                      get_setting("task.publisher.instagram.dev.output") or
                      os.path.join(_SCRIPT_DIR, "dev_output"))
        dev_output = os.path.abspath(dev_output)
        _logger.log("[DEV] Modo developer activo — no se publica en Instagram")
        _logger.log(f"[DEV] Salida local: {dev_output}")

    music_dir = settings_by_key.get("task.publisher.instagram.music.dir")
    if music_dir:
        _logger.log(f"[OK] IG Music dir: {music_dir}")
    else:
        _logger.log("[INFO] No 'task.publisher.instagram.music.dir' configured.")

    if args.publication_id:
        pub = api_get(f"publications/{args.publication_id}")
        if not pub:
            _logger.log(f"[FAIL] Publication #{args.publication_id} not found")
            finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)
            sys.exit(1)
        details = api_get(f"publication-details/by-publication/{args.publication_id}") or []
        ig_details = [d for d in details if d.get("platform") == "instagram"]
        if not ig_details:
            _logger.log(f"[FAIL] No Instagram detail for publication #{args.publication_id}")
            finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)
            sys.exit(1)
        for detail in ig_details:
            process_detail(detail, r2)
    else:
        pending = api_get("publication-details/pending?platform=instagram") or []
        _logger.log(f"Found {len(pending)} pending Instagram detail(s)")

        if not pending:
            _logger.log("[DONE] No pending Instagram details")
            finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)
            return

        if args.dry_run:
            for d in pending:
                pub_id = d.get("publication_id")
                _logger.log(f"  Detail #{d['id']} | pub #{pub_id} | scheduled: {d.get('scheduled_at')}")
            finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)
            return

        for detail in pending:
            process_detail(detail, r2)

    _logger.log("[DONE] Instagram publisher finished")
    finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)


def publish(details, context):
    global _logger, _SETTINGS_CACHE
    _logger = context.get("logger")
    _SETTINGS_CACHE = context.get("settings_cache")
    r2 = context.get("r2")
    for detail in details:
        process_detail(detail, r2)


if __name__ == "__main__":
    main()
