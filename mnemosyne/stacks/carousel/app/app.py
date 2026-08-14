"""Carousel – accessible Instagram carousel generator.

Flask app following the Ghostwrite accessibility pattern:
semantic HTML, native form elements, ARIA live status, no JS required.
"""

import json
import shutil
import uuid
import zipfile
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

import renderer

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_IG = BASE_DIR / "templates_ig"
JOBS_DIR = Path("/data/jobs")
MAX_JOBS = 30  # keep the last N renders, prune older ones

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB text is plenty


def list_templates() -> list[dict]:
    """All template folders that contain a template.json, sorted by name."""
    out = []
    for d in sorted(TEMPLATES_IG.iterdir()):
        cfg_file = d / "template.json"
        if d.is_dir() and cfg_file.exists():
            cfg = json.loads(cfg_file.read_text())
            out.append({"id": d.name, "name": cfg.get("name", d.name)})
    return out


def prune_jobs():
    jobs = sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in jobs[MAX_JOBS:]:
        shutil.rmtree(old, ignore_errors=True)


@app.get("/")
def index():
    return render_template("index.html", templates=list_templates(), error=None)


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
    if not md_text:
        return render_template(
            "index.html", templates=list_templates(),
            error="Bitte Text eingeben oder eine Markdown-Datei auswählen.",
        ), 400

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    try:
        result = renderer.render(md_text, template_dir, job_dir)
    except renderer.ParseError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        return render_template(
            "index.html", templates=list_templates(), error=str(e),
        ), 400

    # Build the ZIP once, right after rendering
    zip_path = job_dir / "karussell.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in result["slides"] + ["alt-texte.txt"]:
            zf.write(job_dir / name, name)

    (job_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    prune_jobs()
    return redirect(url_for("show_result", job_id=job_id))


@app.get("/result/<job_id>")
def show_result(job_id):
    job_dir = JOBS_DIR / job_id
    result_file = job_dir / "result.json"
    if not result_file.exists():
        abort(404)
    result = json.loads(result_file.read_text())
    return render_template("result.html", job_id=job_id, **result)


@app.get("/jobs/<job_id>/<name>")
def job_file(job_id, name):
    path = (JOBS_DIR / job_id / name).resolve()
    if not path.is_file() or JOBS_DIR not in path.parents:
        abort(404)
    return send_file(path)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5000)
