import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from croniter import croniter
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scheduler")

API_BASE = os.getenv("CARDVAULT_API_BASE")
API_USERNAME = os.getenv("CARDVAULT_API_USERNAME")
API_PASSWORD = os.getenv("CARDVAULT_API_PASSWORD")

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


def api_request(method, path, data=None, timeout=15):
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            if _login():
                token = _get_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                req = urllib.request.Request(url, data=body, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else None
        raise


def api_get(path):
    return api_request("GET", path)


def dt_to_str(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def get_enabled_tasks():
    data = api_get("scheduled-tasks/enabled")
    return data if data else []


def get_last_execution(task_id):
    return api_get(f"task-executions/last/{task_id}")


def create_execution(task_id, scheduled_date):
    result = api_request("POST", "task-executions", {
        "scheduled_task_id": task_id,
        "status": "pending",
        "scheduled_date": scheduled_date,
    })
    return result is not None


def get_pending_executions():
    return api_get("task-executions/pending") or []


def update_execution(execution_id, status, started_at=None, finished_at=None, output=None):
    data = {"status": status}
    if started_at is not None:
        data["started_at"] = started_at
    if finished_at is not None:
        data["finished_at"] = finished_at
    if output is not None:
        data["output"] = output
    api_request("PATCH", f"task-executions/{execution_id}", data)


def process_task(task, now):
    task_id = task["id"]
    task_name = task["name"]

    last = get_last_execution(task_id)
    if last:
        base = datetime.fromisoformat(last["scheduled_date"]).replace(tzinfo=timezone.utc)
    else:
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)

    cron = croniter(task["cron_expression"], base)
    created = 0
    while True:
        next_run = cron.get_next(datetime)
        if next_run > now:
            break
        if create_execution(task_id, dt_to_str(next_run)):
            created += 1
    if created:
        log.info("Created %d executions for '%s'", created, task_name)


def _fetch_task(task_id):
    for t in get_enabled_tasks():
        if t["id"] == task_id:
            return t
    return {}


def run_execution(execution):
    exec_id = execution["id"]
    task = execution.get("scheduled_task") or _fetch_task(execution.get("scheduled_task_id"))
    task_name = task.get("name", f"task-{execution.get('scheduled_task_id')}")
    script_path = task.get("script_path", "")

    log.info("Running '%s' (execution %d)", task_name, exec_id)

    if not script_path:
        log.error("No script_path for execution %d", exec_id)
        update_execution(exec_id, "error", started_at=dt_to_str(datetime.now(timezone.utc)),
                         finished_at=dt_to_str(datetime.now(timezone.utc)), output="No script_path configured")
        return

    update_execution(exec_id, "running", started_at=dt_to_str(datetime.now(timezone.utc)))

    try:
        if not os.path.isabs(script_path):
            script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script_path)

        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True, text=True, timeout=3600,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        output = result.stdout
        if result.stderr:
            output += "\n--- stderr ---\n" + result.stderr

        if result.returncode == 0:
            status = "completed"
            log.info("'%s' completed (exit %d)", task_name, result.returncode)
        else:
            status = "error"
            log.warning("'%s' failed (exit %d)", task_name, result.returncode)

        update_execution(exec_id, status, finished_at=dt_to_str(datetime.now(timezone.utc)), output=output)
    except subprocess.TimeoutExpired:
        update_execution(exec_id, "error", finished_at=dt_to_str(datetime.now(timezone.utc)),
                         output="Timeout after 3600s")
        log.error("'%s' timed out", task_name)
    except Exception as e:
        update_execution(exec_id, "error", finished_at=dt_to_str(datetime.now(timezone.utc)), output=str(e))
        log.exception("'%s' raised exception", task_name)


def main_loop(interval):
    log.info("Scheduler started (poll every %ds)", interval)
    while True:
        try:
            now = datetime.now(timezone.utc)

            tasks = get_enabled_tasks()
            for task in tasks:
                try:
                    process_task(task, now)
                except Exception as e:
                    log.error("Error processing task '%s': %s", task.get("name"), e)

            pending = get_pending_executions()
            for execution in pending:
                try:
                    run_execution(execution)
                except Exception as e:
                    log.error("Error running execution %d: %s", execution.get("id"), e)

        except Exception as e:
            log.exception("Unexpected error in main loop")

        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CardVault task scheduler")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds")
    args = parser.parse_args()

    if not API_BASE:
        log.error("CARDVAULT_API_BASE not set")
        sys.exit(1)

    try:
        main_loop(args.interval)
    except KeyboardInterrupt:
        log.info("Scheduler stopped by user")
        sys.exit(0)
