#!/usr/bin/env python3
"""
Multi-Platform Publisher Orchestrator for CardVault.

Fetches all pending publication details from the CardVault API and dispatches them
to platform-specific publishers in order (Instagram → Twitter → ... → Telegram).

Usage:
  python multi_platform_publisher.py              # publish all pending
  python multi_platform_publisher.py --dry-run    # only list pending

Environment (tasks .env):
  CARDVAULT_API_BASE, CARDVAULT_API_USERNAME, CARDVAULT_API_PASSWORD

This is the single task registered in the scheduler. Each platform publisher
can also be run independently for debugging.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv

from task_logger import TaskLogger, finalize_log

load_dotenv()

BUILD_VERSION = "v1.1"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_API_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "CardVault-API"))

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

_TOKEN: str | None = None
_TOKEN_EXPIRES: datetime | None = None
_SETTINGS_CACHE: dict | None = None
_logger: TaskLogger | None = None

_PLATFORM_ORDER = {
    "instagram": 10,
    "tiktok": 20,
    "twitter": 30,
    "threads": 40,
    "bluesky": 50,
    "telegram": 60,
}


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
        print(f"[ERROR] Login failed: {e}")
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


def _load_publisher(platform):
    if platform == "instagram":
        import instagram_publisher
        return instagram_publisher
    elif platform == "twitter":
        import twitter_publisher
        return twitter_publisher
    elif platform == "tiktok":
        import tiktok_publisher
        return tiktok_publisher
    elif platform == "threads":
        import threads_publisher
        return threads_publisher
    elif platform == "bluesky":
        import bluesky_publisher
        return bluesky_publisher
    elif platform == "telegram":
        import telegram_publisher
        return telegram_publisher
    return None


def main():
    global _logger, _SETTINGS_CACHE

    parser = argparse.ArgumentParser(description="CardVault Multi-Platform Publisher")
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
    _logger = TaskLogger(log_dir, "multi_platform_publisher")

    _logger.log(f"CardVault API: {API_BASE}")
    _logger.log(f"[OK] Authenticated  [{BUILD_VERSION}]")
    _logger.log(f"Settings loaded: {len(settings_list)} total")

    pending = api_get("publication-details/pending") or []
    _logger.log(f"Found {len(pending)} pending detail(s) across all platforms")

    if not pending:
        _logger.log("[DONE] No pending publication details")
        finalize_log(_logger, "multi_platform_publisher", _API_ROOT, api_request)
        return

    by_platform = {}
    for d in pending:
        platform = d.get("platform", "unknown")
        by_platform.setdefault(platform, []).append(d)

    sorted_platforms = sorted(by_platform.keys(), key=lambda p: _PLATFORM_ORDER.get(p, 50))

    for platform in sorted_platforms:
        details = by_platform[platform]
        _logger.log(f"\n{'#' * 58}")
        _logger.log(f"# Platform: {platform} ({len(details)} detail(s))")
        _logger.log(f"{'#' * 58}")

        if args.dry_run:
            for d in details:
                pub_id = d.get("publication_id")
                _logger.log(f"  Detail #{d['id']} | pub #{pub_id} | scheduled: {d.get('scheduled_at')}")
            continue

        publisher = _load_publisher(platform)
        if not publisher:
            _logger.log(f"  [WARN] No publisher module for platform '{platform}', skipping")
            for d in details:
                api_request("PATCH", f"publication-details/{d['id']}", {
                    "status": "failed",
                    "error_message": f"No publisher module for platform '{platform}'",
                })
            continue

        context = {
            "logger": _logger,
            "settings_cache": _SETTINGS_CACHE,
            "api_base": API_BASE,
        }

        if platform in ("instagram", "threads"):
            r2 = None
            try:
                if platform == "instagram":
                    from instagram_publisher import create_r2_client, get_ig_config
                    ig_cfg = get_ig_config()
                    if all([ig_cfg["access_token"], ig_cfg["user_id"]]):
                        r2 = create_r2_client()
                    else:
                        _logger.log("  [WARN] Instagram credentials incomplete")
                else:
                    from threads_publisher import create_r2_client, get_threads_config
                    th_cfg = get_threads_config()
                    if all([th_cfg["access_token"], th_cfg["user_id"]]):
                        r2 = create_r2_client()
                    else:
                        _logger.log("  [WARN] Threads credentials incomplete")
            except Exception as e:
                _logger.log(f"  [WARN] Error initializing {platform} R2: {e}")
            context["r2"] = r2

        try:
            publisher.publish(details, context)
        except Exception as e:
            _logger.log(f"  [ERROR] Publisher for '{platform}' raised: {e}")

    _logger.log("\n[DONE] Multi-platform publisher finished")
    finalize_log(_logger, "multi_platform_publisher", _API_ROOT, api_request)


if __name__ == "__main__":
    main()
