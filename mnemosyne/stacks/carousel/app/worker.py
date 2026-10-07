"""Background job runner: renders one carousel job and reports progress.

Started by app.py as a separate process in its own session, so a cancel
request can kill the whole process group — including a WeasyPrint layout
or a pdftoppm call that is still running.

Usage: python3 worker.py <job_dir>
"""

import json
import sys
import traceback
import zipfile
from pathlib import Path

import renderer
from jobstate import write_status


def main(job_dir: Path) -> None:
    meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    md_text = (job_dir / "input.md").read_text(encoding="utf-8")
    out_dir = job_dir / "out"

    def progress(label: str, percent: int, detail: str) -> None:
        write_status(job_dir, "running", label, percent, detail)

    try:
        result = renderer.render(
            md_text,
            Path(meta["template_dir"]),
            out_dir,
            renderer.RenderOptions(hyphenate=meta["hyphenate"]),
            progress,
        )
        progress("ZIP wird gepackt", 95, "")
        # The author's original text incl. formatting tags, so typos can be
        # fixed and the same file re-uploaded for a new render.
        (out_dir / "beitrag.md").write_text(md_text.rstrip("\n") + "\n", encoding="utf-8")
        with zipfile.ZipFile(out_dir / "karussell.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            for name in result["slides"] + ["alt-texte.txt", "beitrag.md"]:
                zf.write(out_dir / name, name)
        (out_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
        write_status(job_dir, "done", "Fertig", 100)
    except Exception as exc:  # report any failure to the waiting page
        traceback.print_exc()
        write_status(job_dir, "error", "Fehler", 0, str(exc) or exc.__class__.__name__)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
