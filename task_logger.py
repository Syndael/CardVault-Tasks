import os
from datetime import datetime


class TaskLogger:
    def __init__(self, log_dir, task_name):
        self.log_dir = os.path.normpath(log_dir)
        self.task_name = task_name
        self.buffer = []
        os.makedirs(self.log_dir, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        self.daily_path = os.path.join(self.log_dir, f"{task_name}_{date_str}.log")

    def __call__(self, msg=""):
        self.log(msg)

    def log(self, msg=""):
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}" if msg else ""
        print(msg, flush=True)
        self.buffer.append(line)
        try:
            with open(self.daily_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def get_execution_log(self):
        return "\n".join(self.buffer)


def finalize_log(logger, task_name, api_root, api_request_func):
    if not logger:
        return
    exec_id = os.environ.get("CARDVAULT_TASK_EXECUTION_ID")
    if exec_id:
        exec_log_name = f"{task_name}_exec_{exec_id}.log"
        exec_log_path = os.path.join(logger.log_dir, exec_log_name)
        try:
            with open(exec_log_path, "w", encoding="utf-8") as f:
                f.write(logger.get_execution_log())
            rel_path = os.path.join(
                os.path.relpath(logger.log_dir, api_root), exec_log_name
            )
            api_request_func("PATCH", f"task-executions/{exec_id}", {"log_file_path": rel_path})
        except Exception as e:
            print(f"  [log error] {e}", flush=True)
