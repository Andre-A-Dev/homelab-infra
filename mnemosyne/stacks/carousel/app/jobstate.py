"""Job status persistence shared by the web app and the job process.

Status lives in <job_dir>/status.json so that any gunicorn worker can
answer a poll or cancel request, not just the one that spawned the job.
Writes are atomic (temp file + os.replace), so a reader never sees a
half-written file.
"""

import json
import os
from pathlib import Path

FINAL_STATES = {"done", "error", "cancelled"}


def write_status(job_dir: Path, state: str, label: str, percent: int, detail: str = "") -> None:
    data = {"state": state, "label": label, "percent": percent, "detail": detail}
    tmp = job_dir / f".status.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, job_dir / "status.json")


def read_status(job_dir: Path) -> dict:
    try:
        return json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"state": "queued", "label": "Wird gestartet", "percent": 0, "detail": ""}
