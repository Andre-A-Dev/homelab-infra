"""Carousel – accessible Instagram carousel generator.

Flow: form -> POST /render (validates, spawns a background job) ->
waiting page /job/<id> (progress + cancel) -> result page /result/<id>.

Accessibility: semantic HTML, native form elements, works without JS
(the waiting page falls back to a meta refresh), status changes are
announced via ARIA live regions.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

import renderer
from jobstate import FINAL_STATES, read_status, write_status

APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR.parent
TEMPLATES_IG = BASE_DIR / "templates_ig"
JOBS_DIR = Path(os.environ.get("CAROUSEL_JOBS_DIR", "/data/jobs")).resolve()
MAX_JOBS = 30  # finished jobs kept on disk; older ones are pruned
JOB_ID = re.compile(r"^[0-9a-f]{12}$")

app = Flask(__name__)


@app.context_processor
def asset_version():
    # Cache-busting for style.css: changes whenever the file changes.
    try:
        return {"asset_version": int((APP_DIR / "static" / "style.css").stat().st_mtime)}
    except FileNotFoundError:
        return {"asset_version": 0}
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB text is plenty


def list_templates() -> list[dict]:
    """All template folders that contain a template.json, sorted by name."""
    out = []
    for d in sorted(TEMPLATES_IG.iterdir()):
        cfg_file = d / "template.json"
        if d.is_dir() and cfg_file.exists():
            cfg = json.loads(cfg_file.read_text())
            # `or` (not a .get default): an empty "name" must fall back too,
            # otherwise the dropdown shows a blank entry.
            out.append({
                "id": d.name,
                "name": (cfg.get("name") or "").strip() or d.name,
                "colors": sorted((cfg.get("highlights") or {}).keys()),
            })
    return out


def job_dir_or_404(job_id: str) -> Path:
    if not JOB_ID.match(job_id) or is_legacy_job(JOBS_DIR / job_id):
        abort(404)
    return JOBS_DIR / job_id


def job_pid(job_dir: Path):
    try:
        return int((job_dir / "pid").read_text())
    except (FileNotFoundError, ValueError):
        return None


def current_status(job_dir: Path) -> dict:
    """Read status; mark a job as failed if its process died silently."""
    status = read_status(job_dir)
    if status["state"] not in FINAL_STATES:
        pid = job_pid(job_dir)
        if pid is not None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                status = read_status(job_dir)  # it may have just finished
                if status["state"] not in FINAL_STATES:
                    write_status(job_dir, "error", "Fehler", 0, "Der Erzeugungsprozess wurde unerwartet beendet.")
                    status = read_status(job_dir)
    return status


def spawn_job(job_dir: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, str(APP_DIR / "worker.py"), str(job_dir)],
        cwd=APP_DIR,
        start_new_session=True,  # own process group -> cancel kills all children
    )
    (job_dir / "pid").write_text(str(proc.pid))
    threading.Thread(target=proc.wait, daemon=True).start()  # reap, no zombies


def is_legacy_job(job_dir: Path) -> bool:
    """Job folder from the pre-async version (no job.json / status.json)."""
    return not (job_dir / "job.json").exists()


def prune_jobs() -> None:
    finished = [
        d for d in JOBS_DIR.iterdir()
        if d.is_dir() and (is_legacy_job(d) or read_status(d)["state"] in FINAL_STATES)
    ]
    finished.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old in finished[MAX_JOBS:]:
        shutil.rmtree(old, ignore_errors=True)


def form_page(error=None, message=None, prefill=None, status_code=200):
    templates = list_templates()
    prefill = prefill or {}
    prefill.setdefault("markdown", "")
    prefill.setdefault("template", templates[0]["id"] if templates else "")
    prefill.setdefault("hyphenate", False)
    return render_template(
        "index.html", templates=templates, error=error, message=message, prefill=prefill,
    ), status_code


@app.get("/")
def index():
    # Coming back from a cancelled/failed job: restore the author's text.
    from_id = request.args.get("from", "")
    if JOB_ID.match(from_id) and not is_legacy_job(JOBS_DIR / from_id):
        job_dir = JOBS_DIR / from_id
        meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        prefill = {
            "markdown": (job_dir / "input.md").read_text(encoding="utf-8"),
            "template": meta["template_id"],
            "hyphenate": meta["hyphenate"],
        }
        status = read_status(job_dir)
        if status["state"] == "cancelled":
            return form_page(message="Die Erzeugung wurde abgebrochen. Dein Text ist unten noch vorhanden.",
                             prefill=prefill)
        if status["state"] == "error":
            return form_page(error=f"Die Erzeugung ist fehlgeschlagen: {status['detail']}", prefill=prefill)
        return form_page(prefill=prefill)
    return form_page()


@app.post("/render")
def do_render():
    template_id = request.form.get("template", "")
    template_dir = TEMPLATES_IG / template_id
    if not (template_dir / "template.json").exists():
        abort(400, "Unbekannte Vorlage.")

    md_text = request.form.get("markdown", "").strip()
    upload = request.files.get("mdfile")
    if upload and upload.filename:
        md_text = upload.read().decode("utf-8", errors="replace").strip()
    hyphenate = request.form.get("hyphenate") == "1"
    prefill = {"markdown": md_text, "template": template_id, "hyphenate": hyphenate}

    if not md_text:
        return form_page(error="Bitte Text eingeben oder eine Markdown-Datei auswählen.",
                         prefill=prefill, status_code=400)
    try:
        renderer.parse_markdown(md_text)  # fast; fail early with a clear message
    except renderer.ParseError as e:
        return form_page(error=str(e), prefill=prefill, status_code=400)

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir()
    (job_dir / "input.md").write_text(md_text, encoding="utf-8")
    (job_dir / "job.json").write_text(json.dumps({
        "template_id": template_id,
        "template_dir": str(template_dir.resolve()),
        "hyphenate": hyphenate,
    }), encoding="utf-8")
    write_status(job_dir, "queued", "Wird gestartet", 0)
    spawn_job(job_dir)
    prune_jobs()
    return redirect(url_for("job_page", job_id=job_id), code=303)


@app.get("/job/<job_id>")
def job_page(job_id):
    job_dir = job_dir_or_404(job_id)
    status = current_status(job_dir)
    if status["state"] == "done":
        return redirect(url_for("show_result", job_id=job_id))
    if status["state"] == "cancelled":
        # never show a dead progress page: back to the form, text restored
        return redirect(url_for("index", **{"from": job_id}))
    return render_template("job.html", job_id=job_id, status=status)


@app.get("/job/<job_id>/status")
def job_status(job_id):
    return jsonify(current_status(job_dir_or_404(job_id)))


@app.post("/job/<job_id>/cancel")
def job_cancel(job_id):
    job_dir = job_dir_or_404(job_id)
    if current_status(job_dir)["state"] not in FINAL_STATES:
        pid = job_pid(job_dir)
        if pid is not None:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if read_status(job_dir)["state"] not in FINAL_STATES:
            write_status(job_dir, "cancelled", "Abgebrochen", 0)
    if read_status(job_dir)["state"] == "done":
        # Finished in the same instant the cancel arrived: just show it.
        return redirect(url_for("show_result", job_id=job_id), code=303)
    return redirect(url_for("index", **{"from": job_id}), code=303)


@app.get("/result/<job_id>")
def show_result(job_id):
    job_dir = job_dir_or_404(job_id)
    result_file = job_dir / "out" / "result.json"
    if not result_file.exists():
        return redirect(url_for("job_page", job_id=job_id))
    result = json.loads(result_file.read_text(encoding="utf-8"))
    return render_template("result.html", job_id=job_id, **result)


@app.get("/jobs/<job_id>/<name>")
def job_file(job_id, name):
    out_dir = (job_dir_or_404(job_id) / "out").resolve()
    path = (out_dir / name).resolve()
    if path.parent != out_dir or not path.is_file():
        abort(404)
    return send_file(path)


@app.get("/favicon.ico")
def favicon():
    return send_file(APP_DIR / "static" / "favicon-32.png", mimetype="image/png")


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5000)
