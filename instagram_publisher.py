#!/usr/bin/env python3
"""
Production Instagram Publisher for CardVault.

Fetches pending publications from the CardVault API and publishes them
to Instagram via instagrapi (private API). Sube imagenes directamente,
sin necesidad de URLs HTTPS publicas.

Steps per publication:
  1. Load inventory item and its files (ordered, instagram_sort_order)
  2. Build caption from product/inventory info
  3. Download images to temp files
  4. Upload photo(s) via instagrapi:
     - Single photo -> photo_upload
     - Multiple photos -> album_upload
  5. Share first photo to stories (with video + music if FFmpeg available)
  6. Update publication status, save Instagram media ID/permalink
  7. Mark inventory as posted_instagram = True

Usage:
  python instagram_publisher.py                        # publish all pending
  python instagram_publisher.py --publication-id 42    # publish specific
  python instagram_publisher.py --dry-run              # only list pending

Environment (tasks .env):
  CARDVAULT_API_BASE, CARDVAULT_API_USERNAME, CARDVAULT_API_PASSWORD

Settings (via CardVault API):
  instagram.username        → Instagram username
  instagram.password        → Instagram password
  instagram.music.dir       → (optional) music directory for story videos
  instagram.enable.stories  → (optional, default "1") "0" to disable stories
"""

import argparse
import json
import os
import random
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

from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, BadPassword, ChallengeRequired

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

_MAX_CAROUSEL_ITEMS = 10

_SETTINGS_CACHE: dict | None = None
_SMTP_CONFIG_CACHE: dict | None = None
_TELEGRAM_BOT_TOKEN: str | None = None

_FONT_FILE: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_GIF_DIR: str = os.path.join(_SCRIPT_DIR, "gif")

_IG_CLIENT: Client | None = None
_SESSION_FILE: str = os.path.join(_SCRIPT_DIR, "ig_session.json")


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


def get_ig_client() -> Client | None:
    global _IG_CLIENT

    if _IG_CLIENT is not None:
        return _IG_CLIENT

    ig_username = get_setting("instagram.username") or os.getenv("IG_USERNAME")
    ig_password = get_setting("instagram.password") or os.getenv("IG_PASSWORD")

    if not ig_username or not ig_password:
        _logger and _logger.log("[FAIL] No Instagram credentials. Set 'instagram.username' and 'instagram.password'")
        return None

    cl = Client()
    cl.delay_range = [2, 5]

    if os.path.exists(_SESSION_FILE):
        try:
            cl.load_settings(_SESSION_FILE)
            cl.login(ig_username, ig_password)
            cl.get_timeline_feed()
            _logger and _logger.log("[OK] IG session restaurada desde archivo")
            _IG_CLIENT = cl
            return cl
        except (LoginRequired, Exception) as e:
            _logger and _logger.log(f"[WARN] No se pudo restaurar sesion IG: {e}, haciendo login fresco...")
            if os.path.exists(_SESSION_FILE):
                os.remove(_SESSION_FILE)

    try:
        cl.login(ig_username, ig_password)
        cl.dump_settings(_SESSION_FILE)
        _logger and _logger.log("[OK] Login en IG correcto, sesion guardada")
        _IG_CLIENT = cl
        return cl
    except BadPassword:
        _logger and _logger.log("[FAIL] IG password incorrecto")
        return None
    except ChallengeRequired:
        _logger and _logger.log("[FAIL] IG requiere verificacion (challenge). Debes iniciar sesion manualmente.")
        return None
    except Exception as e:
        _logger and _logger.log(f"[FAIL] Login en IG fallo: {e}")
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
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.write(data)
            tmp.close()
            tmp_files.append(tmp.name)
            _logger and _logger.log(f"  Descargado fichero {fid} ({len(data)} bytes)")
        except Exception as e:
            _logger and _logger.log(f"  [WARN] Fallo al descargar fichero {fid}: {e}")
    return tmp_files


def cleanup_temp_files(tmp_files):
    for f in tmp_files:
        try:
            os.unlink(f)
        except Exception:
            pass


def publish_instagram(cl, image_paths, caption):
    try:
        if len(image_paths) == 1:
            media = cl.photo_upload(image_paths[0], caption=caption)
        else:
            media = cl.album_upload(image_paths[:10], caption=caption)

        pk = str(media.pk)
        code = media.code if hasattr(media, 'code') and media.code else pk
        permalink = f"https://www.instagram.com/p/{code}/"
        _logger and _logger.log(f"  [OK] Publicado! PK: {pk}  Permalink: {permalink}")
        return code, pk, permalink, None
    except Exception as e:
        return None, None, None, str(e)


def _pick_music(platform_name):
    music_dir = get_setting("instagram.music.dir") or os.getenv("IG_MUSIC_DIR")
    if not music_dir:
        return None
    if music_dir.startswith("./"):
        music_dir = os.path.join(_SCRIPT_DIR, music_dir[2:])
    if not os.path.isdir(music_dir):
        _logger and _logger.log(f"  [WARN] Directorio de musica no encontrado: {music_dir}")
        return None

    platform_map = {
        "NES": "nes", "SNES": "snes", "Super Nintendo": "snes",
        "N64": "n64", "Nintendo 64": "n64",
        "GameCube": "gamecube", "Wii": "wii", "Wii U": "wiiu",
        "Switch": "switch", "Nintendo Switch": "switch",
        "Game Boy": "gameboy", "Game Boy Color": "gameboy",
        "Game Boy Advance": "gba", "GBA": "gba",
        "DS": "ds", "Nintendo DS": "ds", "3DS": "3ds",
        "PS1": "ps1", "PlayStation": "ps1",
        "PS2": "ps2", "PlayStation 2": "ps2",
        "PS3": "ps3", "PlayStation 3": "ps3",
        "PS4": "ps4", "PlayStation 4": "ps4",
        "PS5": "ps5", "PlayStation 5": "ps5",
        "PSP": "psp", "PS Vita": "vita",
        "Xbox": "xbox", "Xbox 360": "xbox360",
        "Xbox One": "xbone", "Xbox Series": "xboxseries",
        "Mega Drive": "megadrive", "Genesis": "megadrive",
        "Dreamcast": "dreamcast", "Saturn": "saturn",
        "PC": "pc", "Steam": "pc",
    }

    folder = platform_map.get(platform_name, "default")
    search_dir = os.path.join(music_dir, folder)
    if not os.path.isdir(search_dir):
        _logger and _logger.log(f"  Sin carpeta '{folder}' para '{platform_name}', usando default")
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
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", chosen],
            capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            _logger and _logger.log(f"  Audio descartado (invalido): {os.path.basename(chosen)}")
            continue
        _logger and _logger.log(f"  Musica seleccionada: {os.path.basename(chosen)} [{folder}]")
        return chosen

    _logger and _logger.log(f"  [WARN] Ningun audio valido en: {search_dir}")
    return None


def _pick_overlay():
    if not os.path.isdir(_GIF_DIR):
        return None

    img_exts = (".gif", ".png", ".jpg", ".jpeg", ".webp")
    candidates = [os.path.join(_GIF_DIR, f) for f in os.listdir(_GIF_DIR)
                  if f.lower().endswith(img_exts)]
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


def _generate_thumbnail(video_path):
    if not shutil.which("ffmpeg"):
        return None
    try:
        fd, thumb_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            thumb_path,
        ], capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and os.path.getsize(thumb_path) > 0:
            return thumb_path
        try:
            os.unlink(thumb_path)
        except Exception:
            pass
        return None
    except Exception as e:
        _logger and _logger.log(f"  Thumbnail fallo: {e}")
        return None


def _create_story_video(image_path, product_name, platform_name, output_path=None):
    if not shutil.which("ffmpeg"):
        _logger and _logger.log("  FFmpeg no encontrado")
        return None

    music_path = _pick_music(platform_name)
    overlay_path = _pick_overlay()
    safe_name = product_name[:40].replace("'", "'\\''")

    try:
        if output_path:
            video_path = output_path
        else:
            fd, video_path = tempfile.mkstemp(suffix=".mp4")
            os.close(fd)

        duration = 15

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", image_path,
        ]
        input_idx = 1
        music_idx = None
        overlay_idx = None
        if music_path:
            music_idx = input_idx
            input_idx += 1
            cmd += ["-i", music_path]
        if overlay_path:
            overlay_idx = input_idx
            input_idx += 1
            if overlay_path.lower().endswith(".gif"):
                cmd += ["-stream_loop", "-1", "-i", overlay_path]
            else:
                cmd += ["-loop", "1", "-i", overlay_path]

        base_filter = (
            f"[0:v]split[bg][fg];"
            f"[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,boxblur=30:15[bg];"
            f"[fg]scale=810:1440:force_original_aspect_ratio=decrease,"
            f"format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            f"a='if("
            f"lt(X,50)*lt(Y,50)*gt(hypot(50-X,50-Y),50)+"
            f"gt(X,W-50)*lt(Y,50)*gt(hypot(X-(W-50),50-Y),50)+"
            f"lt(X,50)*gt(Y,H-50)*gt(hypot(50-X,Y-(H-50)),50)+"
            f"gt(X,W-50)*gt(Y,H-50)*gt(hypot(X-(W-50),Y-(H-50)),50)"
            f",0,255)'[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base]"
        )

        if overlay_path:
            ovl_w = random.randint(300, 480)
            corner = random.choice(["tl", "tr", "bl", "br"])
            m = 20
            jx = 200
            jy = 350
            if corner == "tl":
                ovl_x = random.randint(m, m + jx)
                ovl_y = random.randint(m, m + jy)
            elif corner == "tr":
                ovl_x = random.randint(1080 - ovl_w - jx, 1080 - ovl_w - m)
                ovl_y = random.randint(m, m + jy)
            elif corner == "bl":
                ovl_x = random.randint(m, m + jx)
                ovl_y = random.randint(1920 - ovl_w - jy, 1920 - ovl_w - m)
            else:
                ovl_x = random.randint(1080 - ovl_w - jx, 1080 - ovl_w - m)
                ovl_y = random.randint(1920 - ovl_w - jy, 1920 - ovl_w - m)
            filter_chain = (
                f"{base_filter};"
                f"[{overlay_idx}:v]fps=25,scale={ovl_w}:-1[ovl];"
                f"[base][ovl]overlay={ovl_x}:{ovl_y}[v]"
            )
        else:
            font = os.path.exists(_FONT_FILE)
            if font:
                filter_chain = (
                    f"{base_filter};"
                    f"[base]"
                    f"drawbox=x=0:y=ih*0.72:w=iw:h=ih*0.28:color=black@0.45:t=fill,"
                    f"drawtext=fontfile='{_FONT_FILE}':text='NUEVO POST':fontcolor=white:fontsize=72:"
                    f"x=(w-text_w)/2:y=h*0.79:box=1:boxcolor=black@0.5:boxborderw=16:"
                    f"shadowcolor=black:shadowx=3:shadowy=3,"
                    f"drawtext=fontfile='{_FONT_FILE}':text='{safe_name}':fontcolor=white:fontsize=38:"
                    f"x=(w-text_w)/2:y=h*0.87:box=1:boxcolor=black@0.5:boxborderw=10:"
                    f"shadowcolor=black:shadowx=2:shadowy=2"
                    f"[v]"
                )
            else:
                filter_chain = (
                    f"{base_filter};"
                    f"[base]"
                    f"drawbox=x=0:y=ih*0.72:w=iw:h=ih*0.28:color=black@0.45:t=fill,"
                    f"drawtext=text='NUEVO POST':fontcolor=white:fontsize=72:"
                    f"x=(w-text_w)/2:y=h*0.79:box=1:boxcolor=black@0.5:boxborderw=16:"
                    f"shadowcolor=black:shadowx=3:shadowy=3,"
                    f"drawtext=text='{safe_name}':fontcolor=white:fontsize=38:"
                    f"x=(w-text_w)/2:y=h*0.87:box=1:boxcolor=black@0.5:boxborderw=10:"
                    f"shadowcolor=black:shadowx=2:shadowy=2"
                    f"[v]"
                )

        cmd += ["-filter_complex", filter_chain,
                "-map", "[v]"]

        if music_path:
            cmd += ["-map", f"{music_idx}:a", "-shortest", "-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-an"]

        cmd += ["-c:v", "libx264", "-preset", "fast", "-tune", "stillimage",
                "-pix_fmt", "yuv420p", "-t", str(duration),
                "-movflags", "+faststart", video_path]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            _logger and _logger.log(f"  FFmpeg error: {result.stderr[-800:]}")
            if not output_path:
                try:
                    os.unlink(video_path)
                except Exception:
                    pass
            return None

        if os.path.getsize(video_path) > 0:
            return video_path
        return None
    except Exception as e:
        _logger and _logger.log(f"  FFmpeg excepcion: {e}")
        return None


def share_to_story(cl, first_image_path, product_name, platform_name):
    enable_stories = get_setting("instagram.enable.stories")
    if enable_stories is not None and enable_stories.strip() in ("0", "false", "no"):
        _logger and _logger.log("  [SKIP] Stories desactivadas via config")
        return

    video_path = _create_story_video(first_image_path, product_name, platform_name)
    thumb_path = None
    if video_path:
        try:
            thumb_path = _generate_thumbnail(video_path)
            _logger and _logger.log("  Subiendo story con video...")
            cl.video_upload_to_story(video_path, thumbnail=thumb_path)
            _logger and _logger.log("  [OK] Story con video subida")
        except Exception as e:
            _logger and _logger.log(f"  [WARN] Story video fallo: {e}")
        finally:
            for p in [video_path, thumb_path]:
                if p:
                    try:
                        os.unlink(p)
                    except Exception:
                        pass
    else:
        _logger and _logger.log("  [WARN] No se genero el video de story, se omite.")


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

    if len(file_ids) > _MAX_CAROUSEL_ITEMS:
        _logger and _logger.log(f"  [WARN] {len(file_ids)} images found, only first {_MAX_CAROUSEL_ITEMS} will be published")
        file_ids = file_ids[:_MAX_CAROUSEL_ITEMS]

    if not file_ids:
        error_msg = "No images for this inventory item"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publications/{pub_id}", {"status": "failed", "error_message": error_msg})
        return

    cl = get_ig_client()
    if not cl:
        error_msg = "Could not connect to Instagram"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publications/{pub_id}", {"status": "failed", "error_message": error_msg})
        return

    _logger and _logger.log(f"  Downloading {len(file_ids)} image(s)...")
    tmp_files = download_images_to_temp(file_ids)

    if not tmp_files:
        error_msg = "Could not download any images from API"
        _logger and _logger.log(f"  [FAIL] {error_msg}")
        api_patch(f"publications/{pub_id}", {"status": "failed", "error_message": error_msg})
        return

    ig_code = None
    media_pk = None
    permalink = None
    error_msg = None

    try:
        _logger and _logger.log(f"  Publishing to Instagram ({len(tmp_files)} photo(s))...")
        ig_code, media_pk, permalink, error = publish_instagram(cl, tmp_files, caption)

        if ig_code:
            collection_obj = collection if isinstance(collection, dict) else {}
            platform_name = collection_obj.get("name") or ""
            first_image = tmp_files[0] if tmp_files else None

            share_to_story(cl, first_image, product_name, platform_name)
        else:
            error_msg = error
    except LoginRequired:
        _logger and _logger.log("  Sesion IG expirada, limpiando y reintentando...")
        global _IG_CLIENT
        _IG_CLIENT = None
        if os.path.exists(_SESSION_FILE):
            os.remove(_SESSION_FILE)
        error_msg = "Instagram session expired, will retry on next run"
    except Exception as e:
        error_msg = str(e)
        _logger and _logger.log(f"  [EXCEPTION] {error_msg}")
    finally:
        cleanup_temp_files(tmp_files)

    if ig_code:
        update_data = {
            "status": "published",
            "published_at": datetime.now().isoformat(),
            "instagram_media_id": media_pk or ig_code,
            "instagram_permalink": permalink,
        }
        api_patch(f"publications/{pub_id}", update_data)
        api_patch(f"inventory/{inv_id}", {"posted_instagram": "1"})
        if permalink:
            api_post("inventory-urls", {"inventory_id": inv_id, "url": permalink})
        _logger and _logger.log(f"  [OK] Publication #{pub_id} completed: {permalink}")

        notify_msg = f"Publicacion #{pub_id} completada!\nProducto: {product_name}\n{permalink}"
        _notify_owner(inv, "CardVault - Publicacion Instagram", notify_msg)
    else:
        api_patch(f"publications/{pub_id}", {
            "status": "failed",
            "error_message": error_msg or "Unknown error",
        })
        _logger and _logger.log(f"  [FAIL] Publication #{pub_id} failed: {error_msg}")

        notify_msg = f"Publicacion #{pub_id} ERROR!\nProducto: {product_name}\nError: {error_msg}"
        _notify_owner(inv, "CardVault - ERROR Publicacion Instagram", notify_msg)


def main():
    global _IG_CLIENT, _logger, _SETTINGS_CACHE, _SESSION_FILE

    parser = argparse.ArgumentParser(description="CardVault Instagram Publisher (instagrapi)")
    parser.add_argument("--publication-id", type=int, default=None, help="Publish a specific publication by ID")
    parser.add_argument("--dry-run", action="store_true", help="Show pending without publishing")
    args = parser.parse_args()

    if not API_BASE:
        print("[FAIL] CARDVAULT_API_BASE not set in .env")
        sys.exit(1)

    settings_data = api_get("settings") or {}
    settings_list = settings_data.get("items", [])
    settings_by_key = {item["setting_key"]: item.get("setting_value") for item in settings_list if "setting_key" in item}

    _SETTINGS_CACHE = settings_by_key

    log_path_setting = settings_by_key.get("tasks.log.path", "./logs")
    log_dir = log_path_setting if os.path.isabs(log_path_setting) else os.path.join(_API_ROOT, log_path_setting)
    _logger = TaskLogger(log_dir, "instagram_publisher")

    _logger.log(f"CardVault API: {API_BASE}")
    if not _login():
        _logger.log("[FAIL] Could not authenticate")
        sys.exit(1)
    _logger.log("[OK] Authenticated")

    custom_session = settings_by_key.get("instagram.session.path") or os.getenv("IG_SESSION_PATH")
    if custom_session:
        _SESSION_FILE = custom_session
    _logger.log(f"IG Session file: {_SESSION_FILE}")

    ig_username = settings_by_key.get("instagram.username") or os.getenv("IG_USERNAME")
    ig_password = settings_by_key.get("instagram.password") or os.getenv("IG_PASSWORD")

    if not ig_username or not ig_password:
        _logger.log("[FAIL] No Instagram credentials.")
        _logger.log("  Set settings 'instagram.username' and 'instagram.password' in CardVault")
        _logger.log("  or env vars IG_USERNAME, IG_PASSWORD")
        finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)
        sys.exit(1)

    music_dir = settings_by_key.get("instagram.music.dir") or os.getenv("IG_MUSIC_DIR")
    if music_dir:
        _logger.log(f"[OK] IG Music dir: {music_dir}")
    else:
        _logger.log("[INFO] No 'instagram.music.dir' configured. Story videos will be images only.")

    _logger.log(f"IG User: {ig_username}")

    cl = get_ig_client()
    if not cl:
        _logger.log("[FAIL] Could not login to Instagram")
        finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)
        sys.exit(1)

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
            cl = get_ig_client()
            if not cl:
                _logger.log("[FAIL] IG session lost mid-run, aborting remaining")
                break
            process_publication(pub)

    _logger.log("[DONE] Instagram publisher finished")
    finalize_log(_logger, "instagram_publisher", _API_ROOT, api_request)


if __name__ == "__main__":
    main()
