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


def iter_local_files(root, include_all_files):
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if include_all_files or ext in IMAGE_EXTENSIONS:
                yield real_path(os.path.join(dirpath, filename))


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


def parse_args():
    parser = argparse.ArgumentParser(
        description="Clean product image files that are not referenced by the files table."
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete orphan files. Without this flag the task only reports what would be deleted."
    )
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="Extra product image root to scan. Can be passed more than once."
    )
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="Scan every regular file, not only common image extensions."
    )
    parser.add_argument(
        "--prune-empty-dirs",
        action="store_true",
        help="Remove empty directories after deleting orphan files."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum orphan paths to print in dry-run mode unless --verbose is used."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every orphan path."
    )
    return parser.parse_args()


def main():
    global _logger

    args = parse_args()

    if not API_BASE or not API_USERNAME or not API_PASSWORD:
        print("  Missing CARDVAULT_API_* env vars")
        sys.exit(1)

    print(f"\n  {SEP}")
    print("  Clean orphan product image files")
    print(f"  {SEP}")
    print(f"  API: {API_BASE}")
    print(f"  Mode: {'DELETE' if args.delete else 'dry-run'}")

    if not _login():
        print("  Login failed")
        sys.exit(1)
    print("  Login OK\n")

    print("  Fetching settings...")
    settings = api_get_all("settings")
    settings_by_key = {s["setting_key"]: s.get("setting_value") for s in settings}
    log_path_setting = settings_by_key.get("tasks.log.path", "./logs")
    log_dir = log_path_setting if os.path.isabs(log_path_setting) else os.path.join(_API_ROOT, log_path_setting)
    _logger = TaskLogger(log_dir, "clean_orphan_images")
    _logger.log(f"  Mode: {'DELETE' if args.delete else 'dry-run'}")
    _logger.log(f"  Log path: {log_dir}")

    roots_by_label = get_product_image_roots(settings, args.path)
    roots = sorted(roots_by_label)

    if not roots:
        _logger.log("  No product image roots found in settings.")
        _logger.log("  Use --path /path/to/products_images to scan a directory manually.")
        finalize_log(_logger, "clean_orphan_images", _API_ROOT, api_request)
        sys.exit(1)

    _logger.log("  Product image roots:")
    existing_roots = []
    for root in roots:
        exists = os.path.isdir(root)
        labels = ", ".join(sorted(roots_by_label[root]))
        _logger.log(f"    {root} ({labels}) {'OK' if exists else 'missing'}")
        if exists:
            existing_roots.append(root)

    if not existing_roots:
        _logger.log("\n  No existing directories to scan.")
        finalize_log(_logger, "clean_orphan_images", _API_ROOT, api_request)
        return

    _logger.log("\n  Fetching files from API...")
    files = api_get_all("files", {"per_page": 500})
    referenced, missing, remote, outside = get_referenced_paths(files, existing_roots)
    _logger.log(f"  API file rows: {len(files)}")
    _logger.log(f"  Referenced local files in scanned roots: {len(referenced)}")
    if missing:
        _logger.log(f"  Rows without usable file_path: {missing}")
    if remote:
        _logger.log(f"  Remote file_path rows skipped: {remote}")
    if outside:
        _logger.log(f"  Rows outside scanned roots skipped: {outside}")

    orphan_files = []
    total_files = 0
    for root in existing_roots:
        for local_path in iter_local_files(root, args.all_files):
            total_files += 1
            if local_path not in referenced:
                orphan_files.append(local_path)

    orphan_files.sort()
    _logger.log(f"\n  Local files scanned: {total_files}")
    _logger.log(f"  Orphan files found: {len(orphan_files)}")

    deleted = 0
    failed = 0
    printable_orphans = orphan_files
    if not args.delete and not args.verbose and args.limit >= 0:
        printable_orphans = orphan_files[:args.limit]

    for path in printable_orphans:
        rel = path
        for root in existing_roots:
            if is_inside(path, root):
                rel = os.path.relpath(path, root)
                break

        if args.delete:
            try:
                os.remove(path)
                deleted += 1
                _logger.log(f"    deleted {rel}")
            except OSError as e:
                failed += 1
                _logger.log(f"    failed  {rel}: {e}")
        else:
            _logger.log(f"    orphan  {rel}")

    hidden = len(orphan_files) - len(printable_orphans)
    if hidden > 0:
        _logger.log(f"    ... {hidden} more orphan files hidden. Use --verbose to list all.")

    if args.delete and len(printable_orphans) < len(orphan_files):
        for path in orphan_files[len(printable_orphans):]:
            try:
                os.remove(path)
                deleted += 1
            except OSError:
                failed += 1

    empty_dirs = 0
    if args.prune_empty_dirs:
        for root in existing_roots:
            empty_dirs += prune_empty_dirs(root, args.delete)

    _logger.log(f"\n  {SEP}")
    _logger.log(f"  Orphans:      {len(orphan_files)}")
    _logger.log(f"  Deleted:      {deleted}")
    _logger.log(f"  Failed:       {failed}")
    if args.prune_empty_dirs:
        label = "Removed dirs" if args.delete else "Empty dirs"
        _logger.log(f"  {label}:   {empty_dirs}")
    if not args.delete and orphan_files:
        _logger.log("  Dry-run only. Re-run with --delete to remove these files.")
    _logger.log(f"  {SEP}\n")
    finalize_log(_logger, "clean_orphan_images", _API_ROOT, api_request)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrupted")
        sys.exit(1)
