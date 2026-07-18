#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv
from task_logger import TaskLogger, finalize_log

load_dotenv()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
_API_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "CardVault-API"))

_logger: TaskLogger | None = None

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

SEP = "=" * 58
PRODUCT_IMAGE_PATH_RE = re.compile(r"^sync\..+\.products\.img\.path$")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
LOG_FILE_SUFFIXES = {".log", ".txt"}

_token: str | None = None
_token_expires_at: datetime | None = None


def _login() -> bool:
    global _token, _token_expires_at
    if not API_USERNAME or not API_PASSWORD:
        return False
    try:
        body = json.dumps({
            "username": API_USERNAME,
            "password": API_PASSWORD
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{API_BASE.rstrip('/')}/auth/login",
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _token = data["token"]
        _token_expires_at = datetime.fromisoformat(data["expires_at"]).replace(tzinfo=timezone.utc)
        return True
    except Exception:
        return False


def _get_token() -> str | None:
    global _token, _token_expires_at
    now = datetime.now(timezone.utc)
    if not _token or not _token_expires_at or _token_expires_at <= now:
        _login()
    return _token


def api_request(method, path, data=None):
    clean_path = path.strip("/")
    if "?" in clean_path:
        clean_path, query_string = clean_path.split("?", 1)
        url = f"{API_BASE.rstrip('/')}/{clean_path}/?{query_string}"
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
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else None
        if e.code == 404:
            return None
        (_logger or print)(f"\n  [API {e.code}] {method} {path}")
        return None
    except Exception as e:
        (_logger or print)(f"\n  [API error] {method} {path}: {e}")
        return None


def api_get(path, params=None):
    if params:
        path = f"{path}?{urllib.parse.urlencode(params)}"
    return api_request("GET", path)


def api_get_all(path, params=None):
    page = 1
    items = []
    merged_params = {**(params or {}), "page": page, "per_page": 500}

    while True:
        data = api_get(path, merged_params)
        if not data:
            return items
        items.extend(data.get("items", []))
        pagination = data.get("pagination", {})
        if not pagination.get("has_next"):
            return items
        page += 1
        merged_params["page"] = page


def api_delete(path):
    return api_request("DELETE", path)


def real_path(path):
    return os.path.realpath(os.path.abspath(path))


def resolve_configured_path(path_value):
    if os.path.isabs(path_value):
        return real_path(path_value)
    return real_path(os.path.join(_API_ROOT, path_value))


def resolve_file_path(file_path):
    if not file_path or file_path.lower().startswith(("http://", "https://")):
        return None
    if os.path.isabs(file_path):
        return real_path(file_path)

    candidates = [
        os.path.join(_API_ROOT, file_path),
        os.path.join(_PROJECT_ROOT, file_path),
        os.path.join(_SCRIPT_DIR, file_path),
    ]
    existing = [real_path(p) for p in candidates if os.path.exists(p)]
    return existing[0] if existing else real_path(candidates[0])


def is_inside(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


# ── Orphan image files ────────────────────────────────────────────────

def get_product_image_roots(settings, extra_paths):
    roots_by_label = {}
    for setting in settings:
        key = setting.get("setting_key", "")
        value = setting.get("setting_value")
        if value and PRODUCT_IMAGE_PATH_RE.match(key):
            roots_by_label.setdefault(resolve_configured_path(value), []).append(key)

    for path in extra_paths:
        roots_by_label.setdefault(real_path(path), []).append("--path")

    return roots_by_label


def get_referenced_paths(files, roots):
    referenced = set()
    missing = 0
    remote = 0
    outside = 0

    for item in files:
        file_path = item.get("file_path")
        if file_path and file_path.lower().startswith(("http://", "https://")):
            remote += 1
            continue

        resolved = resolve_file_path(file_path)
        if not resolved:
            missing += 1
            continue

        if any(is_inside(resolved, root) for root in roots):
            referenced.add(resolved)
        else:
            outside += 1

    return referenced, missing, remote, outside


def _walk_error(os_error):
    (_logger or print)(f"  [WARN] No se pudo acceder a: {os_error}")


def prune_empty_dirs(root, delete):
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        if dirnames or filenames:
            continue
        if delete:
            try:
                os.rmdir(dirpath)
                removed += 1
            except OSError:
                pass
        else:
            removed += 1
    return removed


def run_orphan_images_cleanup(settings, extra_paths, delete, all_files, prune, limit, verbose, files=None):
    _logger.log(f"\n  {'─' * 58}")
    _logger.log("  FASE 1: Archivos huérfanos (en disco sin registro en BD)")
    _logger.log(f"  {'─' * 58}")

    roots_by_label = get_product_image_roots(settings, extra_paths)
    roots = sorted(roots_by_label)

    if not roots:
        _logger.log("  No hay raíces de imágenes configuradas (sync.*.products.img.path)")
        _logger.log("  Usa --path /ruta/a/products_images para escanear manualmente")
        return False

    _logger.log("  Raíces de imágenes:")
    existing_roots = []
    for root in roots:
        exists = os.path.isdir(root)
        labels = ", ".join(sorted(roots_by_label[root]))
        _logger.log(f"    {root} ({labels}) {'OK' if exists else 'SIN DIRECTORIO'}")
        if exists:
            existing_roots.append(root)

    if not existing_roots:
        _logger.log("  No hay directorios existentes que escanear")
        return False

    if files is None:
        _logger.log("\n  Obteniendo registros de files desde la API...")
        files = api_get_all("files", {"per_page": 500})
        _logger.log(f"  Obtenidos {len(files)} registros")
    referenced, missing, remote, outside = get_referenced_paths(files, existing_roots)
    _logger.log(f"  Registros en files:    {len(files)}")
    _logger.log(f"  Referenciados local:   {len(referenced)}")
    if missing:
        _logger.log(f"  Sin file_path util:    {missing}")
    if remote:
        _logger.log(f"  Remotos (URL):         {remote}")
    if outside:
        _logger.log(f"  Fuera de raices:       {outside}")

    orphan_files = []
    total_files = 0
    for root in existing_roots:
        _logger.log(f"\n  Escaneando {root}...")
        try:
            for dirpath, _, filenames in os.walk(root, onerror=_walk_error):
                for filename in filenames:
                    total_files += 1
                    if not all_files and os.path.splitext(filename)[1].lower() not in IMAGE_EXTENSIONS:
                        continue
                    local_path = real_path(os.path.join(dirpath, filename))
                    if local_path not in referenced:
                        orphan_files.append(local_path)
                if total_files % 10000 == 0:
                    _logger.log(f"    Escaneados ~{total_files} archivos, {len(orphan_files)} huerfanos...")
        except Exception as e:
            _logger.log(f"    [ERROR] Fallo al escanear {root}: {e}")

    orphan_files.sort()
    _logger.log(f"\n  Archivos locales escaneados: {total_files}")
    _logger.log(f"  Archivos huérfanos:         {len(orphan_files)}")

    if not orphan_files:
        return True

    deleted = 0
    failed = 0
    printable = orphan_files
    if not delete and not verbose and limit >= 0:
        printable = orphan_files[:limit]

    for path in printable:
        rel = path
        for root in existing_roots:
            if is_inside(path, root):
                rel = os.path.relpath(path, root)
                break

        if delete:
            try:
                os.remove(path)
                deleted += 1
                _logger.log(f"    eliminado {rel}")
            except OSError as e:
                failed += 1
                _logger.log(f"    error     {rel}: {e}")
        else:
            _logger.log(f"    huérfano  {rel}")

    hidden = len(orphan_files) - len(printable)
    if hidden > 0:
        _logger.log(f"    ... {hidden} archivos más ocultos. Usa --verbose para listar todos.")

    if delete and len(printable) < len(orphan_files):
        for path in orphan_files[len(printable):]:
            try:
                os.remove(path)
                deleted += 1
            except OSError:
                failed += 1

    empty_dirs = 0
    if prune:
        for root in existing_roots:
            empty_dirs += prune_empty_dirs(root, delete)

    _logger.log(f"\n  Resultados:")
    _logger.log(f"    Huérfanos:    {len(orphan_files)}")
    _logger.log(f"    Eliminados:   {deleted}")
    _logger.log(f"    Fallos:       {failed}")
    if prune:
        label = "Direct. eliminados" if delete else "Direct. vacíos"
        _logger.log(f"    {label}: {empty_dirs}")
    if not delete and orphan_files:
        _logger.log("  (modo dry-run. Ejecuta con --delete para eliminar)")

    return True


# ── Old log files ─────────────────────────────────────────────────────

def run_old_logs_cleanup(log_dir, max_age_days, delete):
    _logger.log(f"\n  {'─' * 58}")
    _logger.log(f"  FASE 2: Logs antiguos (> {max_age_days} días)")
    _logger.log(f"  {'─' * 58}")

    log_dir = resolve_configured_path(log_dir)
    _logger.log(f"  Directorio de logs: {log_dir}")

    if not os.path.isdir(log_dir):
        _logger.log("  El directorio no existe, se omite")
        return True

    now = datetime.now().timestamp()
    cutoff = now - (max_age_days * 86400)

    candidates = []
    try:
        for entry in os.listdir(log_dir):
            full = os.path.join(log_dir, entry)
            if not os.path.isfile(full):
                continue
            ext = os.path.splitext(entry)[1].lower()
            if ext not in LOG_FILE_SUFFIXES:
                continue
            mtime = os.path.getmtime(full)
            if mtime < cutoff:
                candidates.append((entry, full, mtime))
    except OSError as e:
        _logger.log(f"  Error leyendo directorio de logs: {e}")
        return False

    candidates.sort(key=lambda x: x[1])

    _logger.log(f"  Archivos de log encontrados: {len(candidates)}")

    deleted = 0
    failed = 0
    for entry, full, mtime in candidates:
        age_days = (now - mtime) / 86400
        if delete:
            try:
                os.remove(full)
                deleted += 1
                _logger.log(f"    eliminado {entry}  ({age_days:.0f} días)")
            except OSError as e:
                failed += 1
                _logger.log(f"    error     {entry}: {e}")
        else:
            _logger.log(f"    antiguo   {entry}  ({age_days:.0f} días)")

    _logger.log(f"\n  Resultados:")
    _logger.log(f"    Antiguos:  {len(candidates)}")
    if delete:
        _logger.log(f"    Eliminados: {deleted}")
        if failed:
            _logger.log(f"    Fallos:     {failed}")
    if not delete and candidates:
        _logger.log("  (modo dry-run. Ejecuta con --delete para eliminar)")

    return True

def _file_physical_path(file_record):
    fp = file_record.get("file_path")
    if not fp or fp.lower().startswith(("http://", "https://")):
        return None

    if os.path.isabs(fp):
        return real_path(fp) if os.path.exists(real_path(fp)) else None

    candidates = [
        os.path.join(_API_ROOT, fp),
        os.path.join(_PROJECT_ROOT, fp),
        os.path.join(_SCRIPT_DIR, fp),
    ]
    for c in candidates:
        rp = real_path(c)
        if os.path.exists(rp):
            return rp

    return None


def run_orphan_db_records_cleanup(delete, files=None):
    _logger.log(f"\n  {'─' * 58}")
    _logger.log("  FASE 3: Registros huérfanos en BD (sin archivo físico)")
    _logger.log(f"  {'─' * 58}")

    if files is None:
        _logger.log("  Obteniendo registros de files desde la API...")
        files = api_get_all("files", {"per_page": 500})
    _logger.log(f"  Total registros en files: {len(files)}")

    orphaned = []
    for f in files:
        file_id = f.get("id")
        fp = f.get("file_path", "")
        physical = _file_physical_path(f)
        if physical is None:
            orphaned.append((file_id, fp))

    _logger.log(f"  Registros huérfanos (sin archivo en disco): {len(orphaned)}")

    if not orphaned:
        return True

    deleted = 0
    failed = 0
    for file_id, fp in orphaned:
        display = fp or f"id={file_id}"
        if delete:
            result = api_delete(f"files/{file_id}")
            if result is not None:
                deleted += 1
                _logger.log(f"    eliminado registro {file_id}  ({display})")
            else:
                check = api_get(f"files/{file_id}")
                if check is None:
                    deleted += 1
                    _logger.log(f"    eliminado registro {file_id}  ({display}) [confirmado 404]")
                else:
                    failed += 1
                    _logger.log(f"    error     registro {file_id}  ({display})")
        else:
            _logger.log(f"    huérfano  registro {file_id}  ({display})")

    _logger.log(f"\n  Resultados:")
    _logger.log(f"    Huérfanos: {len(orphaned)}")
    if delete:
        _logger.log(f"    Eliminados: {deleted}")
        if failed:
            _logger.log(f"    Fallos:     {failed}")
    if not delete and orphaned:
        _logger.log("  (modo dry-run. Ejecuta con --delete para eliminar)")

    return True

def parse_args():
    parser = argparse.ArgumentParser(
        description="Recolector de basura: limpia archivos huérfanos, logs antiguos "
                    "y registros de BD sin archivo físico."
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Eliminar los elementos encontrados. Sin esta flag solo se reporta."
    )
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help="Saltar la limpieza de imágenes huérfanas (FASE 1)"
    )
    parser.add_argument(
        "--skip-logs",
        action="store_true",
        help="Saltar la limpieza de logs antiguos (FASE 2)"
    )
    parser.add_argument(
        "--skip-db-records",
        action="store_true",
        help="Saltar la limpieza de registros huérfanos en BD (FASE 3)"
    )
    parser.add_argument(
        "--max-log-age",
        type=int,
        default=30,
        help="Días de antigüedad máxima para logs (defecto: 30)"
    )
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="Raíz adicional de imágenes a escanear. Puede repetirse."
    )
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="Escanear todo tipo de archivos, no solo imágenes"
    )
    parser.add_argument(
        "--prune-empty-dirs",
        action="store_true",
        help="Eliminar directorios vacíos tras la limpieza de imágenes"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Máximo de rutas a mostrar en dry-run (defecto: 100)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Mostrar todas las rutas encontradas"
    )
    return parser.parse_args()


def main():
    global _logger

    args = parse_args()

    if not API_BASE or not API_USERNAME or not API_PASSWORD:
        print("  Faltan variables CARDVAULT_API_*")
        sys.exit(1)

    print(f"\n  {SEP}")
    print("  GARBAGE COLLECTOR")
    print(f"  {SEP}")
    print(f"  API: {API_BASE}")
    print(f"  Mode: {'DELETE' if args.delete else 'dry-run'}")
    print(f"  Fases activas:")
    if not args.skip_images:
        print(f"    [x] FASE 1 - Archivos huerfanos en disco")
    else:
        print(f"    [ ] FASE 1 - Omitida")
    if not args.skip_logs:
        print(f"    [x] FASE 2 - Logs antiguos (> {args.max_log_age} dias)")
    else:
        print(f"    [ ] FASE 2 - Omitida")
    if not args.skip_db_records:
        print(f"    [x] FASE 3 - Registros huerfanos en BD")
    else:
        print(f"    [ ] FASE 3 - Omitida")

    if not _login():
        print("  Error de login")
        sys.exit(1)
    print("  Login OK\n")

    print("  Obteniendo settings...")
    settings = api_get_all("settings")
    settings_by_key = {s["setting_key"]: s.get("setting_value") for s in settings}
    log_path_setting = settings_by_key.get("tasks.log.path", "./logs")
    log_dir = log_path_setting if os.path.isabs(log_path_setting) else os.path.join(_API_ROOT, log_path_setting)
    _logger = TaskLogger(log_dir, "garbage_collector")
    _logger.log(f"  Modo: {'DELETE' if args.delete else 'dry-run'}")
    _logger.log(f"  Log path: {log_dir}")

    all_ok = True
    cached_files = api_get_all("files", {"per_page": 500}) if (not args.skip_images or not args.skip_db_records) else []

    if not args.skip_images:
        ok = run_orphan_images_cleanup(
            settings, args.path, args.delete, args.all_files,
            args.prune_empty_dirs, args.limit, args.verbose,
            files=cached_files
        )
        if not ok:
            all_ok = False

    if not args.skip_logs:
        ok = run_old_logs_cleanup(log_dir, args.max_log_age, args.delete)
        if not ok:
            all_ok = False

    if not args.skip_db_records:
        ok = run_orphan_db_records_cleanup(args.delete, files=cached_files)
        if not ok:
            all_ok = False

    _logger.log(f"\n  {SEP}")
    _logger.log("  GARBAGE COLLECTOR FINALIZADO")
    _logger.log(f"  {'CON ERRORES' if not all_ok else 'TODO OK'}")
    _logger.log(f"  {SEP}\n")
    finalize_log(_logger, "garbage_collector", _API_ROOT, api_request)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrumpido")
        sys.exit(1)
    except Exception as e:
        print(f"\n  Error fatal: {e}")
        if _logger:
            _logger.log(f"\n  ERROR FATAL: {e}")
            finalize_log(_logger, "garbage_collector", _API_ROOT, api_request)
        sys.exit(1)
