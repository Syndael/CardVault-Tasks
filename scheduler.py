import argparse
import json
import logging
import os
import subprocess
import sys
import threading
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
        log.error("HTTP %d on %s %s", e.code, method, path)
        return None
    except Exception as e:
        log.error("Request error on %s %s: %s", method, path, e)
        return None


def api_get(path):
    return api_request("GET", path)


def dt_to_str(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _sanitize_mysql_text(text):
    return "".join(c for c in text if ord(c) <= 0xFFFF)


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


MAX_OUTPUT_LENGTH = 50000


def update_execution(execution_id, status, started_at=None, finished_at=None, output=None):
    data = {"status": status}
    if started_at is not None:
        data["started_at"] = started_at
    if finished_at is not None:
        data["finished_at"] = finished_at
    if output is not None:
        if len(output) > MAX_OUTPUT_LENGTH:
            output = output[-MAX_OUTPUT_LENGTH:] + "\n... (truncated)"
        output = _sanitize_mysql_text(output)
        data["output"] = output

    for attempt in range(3):
        result = api_request("PATCH", f"task-executions/{execution_id}", data)
        if result is not None:
            return
        if attempt < 2:
            log.warning("Retry %d/3 updating execution %d to '%s'", attempt + 1, execution_id, status)
            time.sleep(2)
    log.error("Failed to update execution %d to '%s' after 3 attempts", execution_id, status)


def process_task(task, now):
    task_id = task["id"]
    task_name = task["name"]
    cron_expr = task.get("cron_expression")

    if not cron_expr:
        return

    last = get_last_execution(task_id)
    if last:
        base = datetime.fromisoformat(last["scheduled_date"]).replace(tzinfo=None)
    else:
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)

    cron = croniter(cron_expr, base)
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


def _stream_output(stream, lines):
    for line in iter(stream.readline, ""):
        print(line, end="", flush=True)
        lines.append(line)


def run_execution(execution):
    exec_id = execution["id"]
    task = execution.get("scheduled_task") or _fetch_task(execution.get("scheduled_task_id"))
    task_name = task.get("name", f"task-{execution.get('scheduled_task_id')}")
    script_path = task.get("script_path", "")

    log.info("Running '%s' (execution %d)", task_name, exec_id)

    if not script_path:
        log.error("No script_path for execution %d", exec_id)
        update_execution(exec_id, "error", started_at=dt_to_str(datetime.now()),
                         finished_at=dt_to_str(datetime.now()), output="No script_path configured")
        return

    update_execution(exec_id, "running", started_at=dt_to_str(datetime.now()))

    try:
        if not os.path.isabs(script_path):
            script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script_path)

        TASK_TIMEOUT = 14400
        process = subprocess.Popen(
            [sys.executable, script_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "CARDVAULT_TASK_EXECUTION_ID": str(exec_id)},
        )

        stdout_lines = []
        stderr_lines = []

        threads = [
            threading.Thread(target=_stream_output, args=(process.stdout, stdout_lines)),
            threading.Thread(target=_stream_output, args=(process.stderr, stderr_lines)),
        ]
        for t in threads:
            t.daemon = True
            t.start()

        POLL_INTERVAL = 5
        deadline = time.time() + TASK_TIMEOUT
        timed_out = False
        cancelled = False

        while time.time() < deadline:
            for t in threads:
                t.join(timeout=POLL_INTERVAL)
            if process.poll() is not None and not any(t.is_alive() for t in threads):
                break

            current = api_get(f"task-executions/{exec_id}")
            if current and current.get("status") == "cancelled":
                log.info("Execution %d cancelled by user", exec_id)
                cancelled = True
                break
        else:
            timed_out = True

        if cancelled or timed_out:
            try:
                process.kill()
            except Exception:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("Process %d did not die after kill, forcing", process.pid)

        for t in threads:
            t.join(timeout=5)

        output = "".join(stdout_lines)
        if stderr_lines:
            output += "\n--- stderr ---\n" + "".join(stderr_lines)

        if len(output) > MAX_OUTPUT_LENGTH:
            output = output[-MAX_OUTPUT_LENGTH:] + "\n... (truncated)"

        if cancelled:
            output += "\n\n--- Ejecucion cancelada por el usuario ---"
            update_execution(exec_id, "cancelled", finished_at=dt_to_str(datetime.now()), output=output)
            log.info("'%s' cancelled", task_name)
        elif timed_out:
            update_execution(exec_id, "error", finished_at=dt_to_str(datetime.now()),
                             output=output + f"\n\n--- Timeout after {TASK_TIMEOUT}s ---")
            log.error("'%s' timed out", task_name)
        elif process.returncode == 0:
            log.info("'%s' completed (exit %d)", task_name, process.returncode)
            update_execution(exec_id, "completed", finished_at=dt_to_str(datetime.now()), output=output)
        else:
            log.warning("'%s' failed (exit %d)", task_name, process.returncode)
            update_execution(exec_id, "error", finished_at=dt_to_str(datetime.now()), output=output)

    except Exception as e:
        try:
            process.kill()
        except Exception:
            pass
        update_execution(exec_id, "error", finished_at=dt_to_str(datetime.now()), output=str(e))
        log.exception("'%s' raised exception", task_name)


_lock = threading.Lock()
_running_task_ids: dict[int, int] = {}  # scheduled_task_id -> execution_id
_running_execs: dict[int, threading.Thread] = {}  # execution_id -> Thread


def _start_execution(execution):
    try:
        current = api_get(f"task-executions/{execution['id']}")
        if current and current.get("status") in ("cancelled",):
            log.info("Execution %d already cancelled, skipping", execution["id"])
            return
        run_execution(execution)
    finally:
        with _lock:
            exec_id = execution["id"]
            task_id = execution.get("scheduled_task_id")
            _running_task_ids.pop(task_id, None)
            _running_execs.pop(exec_id, None)


def recover_orphaned_executions():
    running = api_get("task-executions/running")
    if not running:
        return
    now_str = dt_to_str(datetime.now())
    for exc in running:
        exec_id = exc["id"]
        log.warning("Recovering orphaned execution %d", exec_id)
        update_execution(exec_id, "error", finished_at=now_str,
                         output="Orphaned execution — scheduler was restarted")
    log.info("Recovered %d orphaned execution(s)", len(running))


def main_loop(interval):
    recover_orphaned_executions()
    log.info("Scheduler started (poll every %ds)", interval)
    while True:
        try:
            now = datetime.now()

            tasks = get_enabled_tasks()
            for task in tasks:
                try:
                    process_task(task, now)
                except Exception as e:
                    log.error("Error processing task '%s': %s", task.get("name"), e)

            pending = get_pending_executions()

            with _lock:
                for execution in pending:
                    exec_id = execution["id"]
                    task_id = execution.get("scheduled_task_id")

                    if exec_id in _running_execs:
                        continue
                    if task_id in _running_task_ids:
                        log.info("Skipping execution %d: '%s' already running",
                                 exec_id, execution.get("scheduled_task", {}).get("name", task_id))
                        continue
                    if len(_running_execs) >= 3:
                        log.info("Max concurrent executions reached, skipping remaining pending")
                        break

                    _running_task_ids[task_id] = exec_id
                    t = threading.Thread(target=_start_execution, args=(execution,), daemon=True)
                    _running_execs[exec_id] = t
                    t.start()

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
