#!/usr/bin/env python3
"""
TikTok OAuth Helper for CardVault.

Obtiene el access token inicial y gestiona el refresh automático.

Uso:
  python tiktok_oauth_helper.py --authorize    # Abre navegador para autorización
  python tiktok_oauth_helper.py --refresh      # Fuerza refresh del token
  python tiktok_oauth_helper.py --status       # Muestra estado actual del token
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
import time

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

REDIRECT_PORT = 8080
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

_token = None
_token_expires_at = None


def _login() -> bool:
    global _token, _token_expires_at
    if not API_USERNAME or not API_PASSWORD:
        print("Faltan CARDVAULT_API_* env vars")
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
    except Exception as e:
        print(f"Login fallido: {e}")
        return False


def _get_token() -> str | None:
    global _token, _token_expires_at
    now = datetime.now(timezone.utc)
    if not _token or not _token_expires_at or _token_expires_at <= now:
        if not _login():
            return None
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


def get_setting(key):
    data = api_request("GET", f"settings?key={key}")
    if data:
        items = data.get("items", []) if isinstance(data, dict) else data
        if items and isinstance(items, list):
            return items[0].get("setting_value")
    return None


def update_setting(key, value):
    return api_request("PATCH", f"settings/key/{key}", {"setting_value": value})


def get_tiktok_config():
    return {
        "client_key": get_setting("task.publisher.tiktok.client.key") or "",
        "client_secret": get_setting("task.publisher.tiktok.client.secret") or "",
        "access_token": get_setting("task.publisher.tiktok.access.token") or "",
        "refresh_token": get_setting("task.publisher.tiktok.refresh.token") or "",
        "expires_at": get_setting("task.publisher.tiktok.expires.at") or "",
    }


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    auth_code = None
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        
        if "code" in params:
            OAuthCallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Autorizacion completada!</h1>")
            self.wfile.write(b"<p>Puedes cerrar esta ventana.</p></body></html>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Error: no code received")
    
    def log_message(self, format, *args):
        pass


def authorize():
    cfg = get_tiktok_config()
    
    if not cfg["client_key"] or not cfg["client_secret"]:
        print("ERROR: Falta client_key o client_secret en settings")
        return False
    
    scopes = ["video.upload", "video.publish", "user.info.basic"]
    scope_str = ",".join(scopes)
    
    auth_url = (
        f"https://www.tiktok.com/v2/auth/authorize/"
        f"?client_key={cfg['client_key']}"
        f"&scope={scope_str}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&state=cardvault_{int(time.time())}"
    )
    
    print(f"\nAbriendo navegador para autorizacion...")
    print(f"URL: {auth_url}\n")
    
    server = HTTPServer(("localhost", REDIRECT_PORT), OAuthCallbackHandler)
    thread = Thread(target=lambda: server.handle_request())
    thread.start()
    
    webbrowser.open(auth_url)
    
    print("Esperando autorizacion...")
    thread.join(timeout=120)
    server.server_close()
    
    if not OAuthCallbackHandler.auth_code:
        print("ERROR: No se recibio el codigo de autorizacion")
        return False
    
    print(f"Code recibido: {OAuthCallbackHandler.auth_code[:20]}...")
    
    return exchange_code_for_token(OAuthCallbackHandler.auth_code, cfg)


def exchange_code_for_token(code, cfg):
    print("Intercambiando code por token...")
    
    token_url = "https://open.tiktokapis.com/v2/oauth/token/"
    
    data = {
        "client_key": cfg["client_key"],
        "client_secret": cfg["client_secret"],
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
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
            print(f"ERROR: Respuesta inesperada: {token_data}")
            return False
        
        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token", "")
        expires_in = token_data.get("expires_in", 86400)
        refresh_expires_in = token_data.get("refresh_expires_in", 31536000)
        
        expires_at = datetime.now(timezone.utc).timestamp() + expires_in
        expires_at_str = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
        
        print(f"\nToken obtenido!")
        print(f"  Access token: {access_token[:30]}...")
        print(f"  Refresh token: {refresh_token[:30]}..." if refresh_token else "  Refresh token: NO RECIBIDO")
        print(f"  Expira en: {expires_in}s ({expires_in/3600:.1f}h)")
        print(f"  Expires at: {expires_at_str}")
        
        print("\nGuardando en CardVault...")
        
        update_setting("task.publisher.tiktok.access.token", access_token)
        if refresh_token:
            update_setting("task.publisher.tiktok.refresh.token", refresh_token)
        update_setting("task.publisher.tiktok.expires.at", expires_at_str)
        
        print("OK! Tokens guardados.")
        return True
        
    except Exception as e:
        print(f"ERROR al obtener token: {e}")
        return False


def refresh_token():
    cfg = get_tiktok_config()
    
    if not cfg["refresh_token"]:
        print("ERROR: No hay refresh_token guardado. Ejecuta --authorize primero.")
        return False
    
    print("Refrescando token...")
    
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
            print(f"ERROR: Respuesta inesperada: {token_data}")
            return False
        
        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token", cfg["refresh_token"])
        expires_in = token_data.get("expires_in", 86400)
        
        expires_at = datetime.now(timezone.utc).timestamp() + expires_in
        expires_at_str = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
        
        print(f"\nToken refrescado!")
        print(f"  Access token: {access_token[:30]}...")
        print(f"  Expira en: {expires_in}s ({expires_in/3600:.1f}h)")
        print(f"  Expires at: {expires_at_str}")
        
        print("\nGuardando en CardVault...")
        
        update_setting("task.publisher.tiktok.access.token", access_token)
        if refresh_token:
            update_setting("task.publisher.tiktok.refresh.token", refresh_token)
        update_setting("task.publisher.tiktok.expires.at", expires_at_str)
        
        print("OK! Tokens actualizados.")
        return True
        
    except Exception as e:
        print(f"ERROR al refrescar token: {e}")
        return False


def show_status():
    cfg = get_tiktok_config()
    
    print("\n=== Estado TikTok OAuth ===\n")
    print(f"Client Key: {cfg['client_key'][:10]}..." if cfg['client_key'] else "Client Key: NO CONFIGURADO")
    print(f"Client Secret: {'***' if cfg['client_secret'] else 'NO CONFIGURADO'}")
    print(f"Access Token: {cfg['access_token'][:30]}..." if cfg['access_token'] else "Access Token: NO CONFIGURADO")
    print(f"Refresh Token: {cfg['refresh_token'][:30]}..." if cfg['refresh_token'] else "Refresh Token: NO CONFIGURADO")
    
    if cfg['expires_at']:
        try:
            expires_dt = datetime.fromisoformat(cfg['expires_at'])
            now = datetime.now(timezone.utc)
            remaining = (expires_dt - now).total_seconds()
            
            if remaining > 0:
                print(f"Expires At: {cfg['expires_at']} (en {remaining/3600:.1f}h)")
            else:
                print(f"Expires At: {cfg['expires_at']} (EXPIRADO hace {-remaining/3600:.1f}h)")
        except:
            print(f"Expires At: {cfg['expires_at']} (formato invalido)")
    else:
        print("Expires At: NO CONFIGURADO")
    
    print()


def main():
    parser = argparse.ArgumentParser(description="TikTok OAuth Helper")
    parser.add_argument("--authorize", action="store_true", help="Iniciar flujo de autorizacion")
    parser.add_argument("--refresh", action="store_true", help="Forzar refresh del token")
    parser.add_argument("--status", action="store_true", help="Mostrar estado actual")
    
    args = parser.parse_args()
    
    if not _login():
        sys.exit(1)
    
    if args.authorize:
        if not authorize():
            sys.exit(1)
    elif args.refresh:
        if not refresh_token():
            sys.exit(1)
    elif args.status:
        show_status()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
