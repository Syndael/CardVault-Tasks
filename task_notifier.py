"""
Task notification helpers for CardVault publishers.

Sends error/info notifications to the publication owner via Telegram.
The owner is determined from the first inventory linked to the publication.
"""

import json
import urllib.parse
import urllib.request


def _get_user_info(api_base, token, user_id):
    try:
        url = f"{api_base.rstrip('/')}/auth/user/{user_id}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _send_telegram_message(bot_token, chat_id, text):
    if not bot_token or not chat_id:
        return False
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text[:4096],
        }).encode("utf-8")
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except Exception:
        return False


def _get_owner_telegram_id(api_base, get_token, publication):
    inventories = publication.get("inventories") or []
    for inv in inventories:
        user_id = inv.get("user_id")
        if not user_id:
            continue
        token = get_token()
        if not token:
            continue
        user = _get_user_info(api_base, token, user_id)
        if user and user.get("telegram_id"):
            return user["telegram_id"]
    return None


def _platform_status_label(publication, platform):
    details = publication.get("details") or []
    for d in details:
        if d.get("platform") == platform:
            status = d.get("status") or "sin estado"
            permalink = d.get("permalink")
            if permalink:
                return f"{platform}: OK"
            return f"{platform}: {status}"
    return f"{platform}: no existe"


def notify_unresolved_tags(detail, publication, unresolved_tags, api_base, get_token, logger=None):
    bot_token = None
    try:
        from task_logger import TaskLogger
        pass
    except Exception:
        pass

    try:
        import os
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN_OVERRIDE")
    except Exception:
        pass

    if not bot_token:
        try:
            import urllib.request as _ur
            import json as _json
            settings_url = f"{api_base.rstrip('/')}/settings/by-key/bot.telegram.token"
            token = get_token()
            req = _ur.Request(settings_url, headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            })
            with _ur.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
                bot_token = data.get("setting_value")
        except Exception:
            pass

    chat_id = _get_owner_telegram_id(api_base, get_token, publication)
    if not chat_id:
        if logger:
            logger.log("  [NOTIFY] No se encontró telegram_id del dueño")
        return

    platform = detail.get("platform", "?")
    detail_id = detail.get("id", "?")
    pub_id = publication.get("id", "?")

    tags_str = ", ".join(f"<{t}>" for t in unresolved_tags)

    content_platforms = ["instagram", "tiktok"]
    status_parts = []
    for cp in content_platforms:
        details = publication.get("details") or []
        exists = any(d.get("platform") == cp for d in details)
        if exists:
            status_parts.append(_platform_status_label(publication, cp))

    status_str = ", ".join(status_parts) if status_parts else "sin plataformas de contenido"

    message = (
        f"Error en la difusión de {platform} (pub #{pub_id}, detail #{detail_id}).\n"
        f"Tags no resueltos: {tags_str}\n"
        f"{status_str}"
    )

    ok = _send_telegram_message(bot_token, chat_id, message)
    if logger:
        logger.log(f"  [NOTIFY] Telegram al dueño ({chat_id}): {'OK' if ok else 'FAIL'}")
