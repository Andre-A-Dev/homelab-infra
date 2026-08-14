"""Carousel renderer: Markdown -> paged HTML -> PDF (WeasyPrint) -> PNG slides.

Layout is fully deterministic: WeasyPrint paginates the text across
1080x1440 px pages, so overflow is impossible by construction. Paragraphs
are kept together (break-inside: avoid) unless a single paragraph exceeds
one page, in which case CSS forces a clean break.

The title slide is special-cased: its text is word-wrapped and its font
size is computed by actually measuring the title against the real font
file (Pillow), so it fills the available panel — both width and height —
regardless of title length, the same way the original hand-tuned
PowerPoint titles did.

Markdown conventions:
  # Title            -> title slide (no page number)
  ## TW: <topic>     -> trigger title slide, following text on trigger pages
  ## ENDE TW         -> back to normal pages
  blank line         -> paragraph break; inline formatting: see _inline()
"""

import html
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import ImageFont
from weasyprint import HTML

PAGE_W, PAGE_H = 1080, 1440
MAX_SLIDES = 20  # Instagram carousel limit

# Absolute floor so a pathologically long title still produces something
# legible-ish rather than shrinking towards zero. This is a hard technical
# floor, not the "soft" title_min_size from template.json — see
# _fit_title_layout().
TITLE_HARD_MIN_SIZE = 24

TW_START = re.compile(r"^##\s*TW\s*:?\s*(.*)$", re.IGNORECASE)
TW_END = re.compile(r"^##\s*ENDE\s+TW\s*$", re.IGNORECASE)
TITLE = re.compile(r"^#\s+(.*)$")

# Strip markdown delimiters for width measurement only (display still uses
# the fully-formatted version via _inline()). Approximate: a bold/italic
# run measures very close to the plain glyph width at this stage, and any
# small residual error is absorbed by BOLD_WIDTH_SAFETY above.
_MD_STRIP = re.compile(r"\*\*\*|\*\*|\*|__|~~")


class ParseError(ValueError):
    pass


@dataclass
class Section:
    kind: str  # "normal" | "trigger"
    tw_topic: str = ""
    paragraphs: list = field(default_factory=list)


@dataclass
class Post:
    title: str
    sections: list


def parse_markdown(text: str) -> Post:
    """Parse the post markdown into title + normal/trigger sections."""
    lines = text.replace("\r\n", "\n").split("\n")
    title = None
    sections: list[Section] = []
    current = Section("normal")
    buf: list[str] = []

    def flush():
        para = " ".join(s.strip() for s in buf).strip()
        if para:
            current.paragraphs.append(para)
        buf.clear()

    def push_section():
        nonlocal current
        if current.paragraphs or current.kind == "trigger":
            sections.append(current)

    for line in lines:
        m = TITLE.match(line)
        if m and title is None:
            flush()
            title = m.group(1).strip()
            continue
        if TW_END.match(line.strip()):
            flush()
            push_section()
            current = Section("normal")
            continue
        m = TW_START.match(line.strip())
        if m:
            flush()
            push_section()
            current = Section("trigger", tw_topic=m.group(1).strip())
            continue
        if line.strip() == "":
            flush()
        else:
            buf.append(line)

    flush()
    push_section()

    if title is None:
        raise ParseError(
            "Kein Titel gefunden. Die erste Zeile muss mit '# ' beginnen, "
            "zum Beispiel: # Mein Titel"
        )
    if not any(s.paragraphs for s in sections):
        raise ParseError("Kein Text nach dem Titel gefunden.")
    return Post(title=title, sections=sections)


def _inline(text: str) -> str:
    """Escape HTML, then apply a small fixed set of inline markup.

    Supported, in this order (longest/most specific marker first so
    overlapping markers can't misparse each other):
      ***text***  -> bold + italic
      **text**    -> bold
      *text*      -> italic
      __text__    -> underline
      ~~text~~    -> strikethrough
    """
    t = html.escape(text)
    t = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"\*(.+?)\*", r"<em>\1</em>", t)
    t = re.sub(r"__(.+?)__", r"<u>\1</u>", t)
    t = re.sub(r"~~(.+?)~~", r"<s>\1</s>", t)
    return t


TITLE_LINE_HEIGHT = 1.15  # keep in sync with the h1 CSS below


def _measure_width(text: str, font_path: Path, size: int) -> float:
    font = ImageFont.truetype(str(font_path), size)
    return font.getlength(text)


def _greedy_wrap_counts(words: list[str], font: "ImageFont.FreeTypeFont", max_width: float) -> list[int]:
    """Greedy word-wrap: how many of `words` fit on each line at this
    font, given max_width. Returns a list of word-counts, one per line.
    A single word wider than max_width still gets its own line (can't
    split a word) — that line will simply overrun, same as a hard-floor
    overflow elsewhere in this module.
    """
    space_w = font.getlength(" ")
    counts: list[int] = []
    current_n = 0
    current_w = 0.0
    for word in words:
        w = font.getlength(word)
        added_w = w if current_n == 0 else current_w + space_w + w
        if current_n > 0 and added_w > max_width:
            counts.append(current_n)
            current_n, current_w = 1, w
        else:
            current_n += 1
            current_w = added_w
    if current_n:
        counts.append(current_n)
    return counts


def _fit_title_layout(title: str, font_path: Path, max_width: float, max_height: float) -> dict:
    """Find the largest font size at which `title` — word-wrapped as
    needed — fills the panel: total block height <= max_height, every
    line's width <= max_width. Scans font sizes from a generous upper
    bound down to TITLE_HARD_MIN_SIZE and takes the first (= largest)
    size that fits, then wraps the actual (formatted) words accordingly.

    Returns {size, lines, hit_floor}. `lines` are already HTML-inline-
    formatted strings ready to join with <br>. hit_floor=True means even
    the minimum legible size doesn't fully fit (rendered text may
    slightly overrun) — surfaced to the caller as a warning.
    """
    words = title.split()
    plain_words = (_MD_STRIP.sub("", title)).split()
    if len(plain_words) != len(words):
        # Extremely unlikely (markdown stripping can't change whitespace),
        # but fall back to the unformatted words rather than risk a
        # misaligned line/word mapping.
        plain_words = words

    upper_bound = max(TITLE_HARD_MIN_SIZE, round(max_height / TITLE_LINE_HEIGHT) + 10)
    chosen_size = TITLE_HARD_MIN_SIZE
    chosen_counts = None
    for size in range(upper_bound, TITLE_HARD_MIN_SIZE - 1, -1):
        font = ImageFont.truetype(str(font_path), size)
        counts = _greedy_wrap_counts(plain_words, font, max_width)
        n_lines = len(counts)
        if n_lines * size * TITLE_LINE_HEIGHT > max_height:
            continue
        # _greedy_wrap_counts only enforces max_width while *adding a
        # second-or-later word* to a line; a single word forced onto an
        # otherwise-empty line (typical for short titles, or one long
        # word) is never checked against max_width. Verify explicitly.
        idx = 0
        widest = 0.0
        for n in counts:
            line = " ".join(plain_words[idx : idx + n])
            widest = max(widest, font.getlength(line))
            idx += n
        if widest <= max_width:
            chosen_size, chosen_counts = size, counts
            break

    hit_floor = chosen_counts is None
    if chosen_counts is None:
        # Nothing in the whole scanned range fit height-wise even at the
        # floor size; wrap at the floor size anyway so we still have
        # something sane to render (it will slightly overrun).
        font = ImageFont.truetype(str(font_path), TITLE_HARD_MIN_SIZE)
        chosen_counts = _greedy_wrap_counts(plain_words, font, max_width)

    # Regroup the *original* (formatted) words using the same counts,
    # so markdown markers survive into the display lines.
    lines, idx = [], 0
    for n in chosen_counts:
        lines.append(_inline(" ".join(words[idx : idx + n])))
        idx += n

    return {"size": chosen_size, "lines": lines, "hit_floor": hit_floor}


def _title_layout(title: str, cfg: dict) -> dict:
    """Compute the fitted, word-wrapped title lines and vertical
    centering offset that make the title fill the panel.
    """
    p, pad = cfg["panel"], cfg["content_padding"]
    content_w = p["x1"] - p["x0"] - 2 * pad
    content_h = p["y1"] - p["y0"] - 2 * pad
    font_dir = (Path(cfg["_template_dir"]) / "assets").resolve()
    font_path = font_dir / cfg["font"]["regular"]

    # Small safety margin: leave a sliver of breathing room rather than
    # letting glyphs touch the panel edge exactly, and reserve headroom
    # for synthetic bold (no dedicated bold face is registered).
    layout = _fit_title_layout(title, font_path, content_w * 0.96, content_h * 0.96)
    total_height = len(layout["lines"]) * layout["size"] * TITLE_LINE_HEIGHT
    margin_top = max(0, round((content_h - total_height) / 2))
    layout["margin_top"] = margin_top
    return layout


def build_html(post: Post, template_dir: Path) -> tuple[str, bool]:
    cfg = json.loads((template_dir / "template.json").read_text())
    cfg["_template_dir"] = str(template_dir)
    assets = (template_dir / "assets").resolve().as_uri()
    f = cfg["font"]
    p = cfg["panel"]
    pad = cfg["content_padding"]
    # Content box = panel interior; footer strip lives below the panel.
    margin_top = p["y0"] + pad
    margin_side = p["x0"] + pad
    margin_bottom = PAGE_H - p["y1"] + pad
    footer_h = PAGE_H - p["y1"]

    title_layout = _title_layout(post.title, cfg)

    body_parts = []
    body_parts.append(
        f'<h1 style="font-size:{title_layout["size"]}px; '
        f'margin-top:{title_layout["margin_top"]}px">'
        + "<br>".join(title_layout["lines"])
        + "</h1>"
    )
    for sec in post.sections:
        if sec.kind == "trigger":
            topic = _inline(sec.tw_topic) if sec.tw_topic else ""
            body_parts.append(
                f'<h2 class="tw"><span class="tw-label">{html.escape(cfg["trigger_title_prefix"])}</span>'
                + (f"<br>{topic}" if topic else "")
                + "</h2>"
            )
            body_parts.append('<div class="trigger">')
            body_parts.extend(f"<p>{_inline(par)}</p>" for par in sec.paragraphs)
            body_parts.append("</div>")
        else:
            body_parts.append('<div class="normal">')
            body_parts.extend(f"<p>{_inline(par)}</p>" for par in sec.paragraphs)
            body_parts.append("</div>")

    css = f"""
    @font-face {{
      font-family: 'Carousel';
      src: url('{assets}/{f["regular"]}');
    }}
    @font-face {{
      font-family: 'Carousel';
      font-style: italic;
      src: url('{assets}/{f["italic"]}');
    }}
    @page {{
      size: {PAGE_W}px {PAGE_H}px;
      margin: {margin_top}px {margin_side}px {margin_bottom}px {margin_side}px;
      background: url('{assets}/page_normal.png') no-repeat;
      background-size: {PAGE_W}px {PAGE_H}px;
      background-position: -{margin_side}px -{margin_top}px;
      @bottom-center {{
        content: '{cfg["account"]}';
        font-family: 'Carousel';
        font-size: {f["footer_size"]}px;
        letter-spacing: 0.35em;
        color: {cfg["colors"]["footer"]};
        margin-bottom: {footer_h / 2 - f["footer_size"]}px;
      }}
      @bottom-right {{
        content: counter(page);
        font-family: 'Carousel';
        font-size: {f["footer_size"]}px;
        color: {cfg["colors"]["footer"]};
        text-align: right;
        margin-bottom: {footer_h / 2 - f["footer_size"]}px;
        margin-right: -70px;
      }}
    }}
    @page title {{
      @bottom-right {{ content: none; }}
    }}
    @page trigger-title {{
      background: url('{assets}/page_trigger_title.png') no-repeat;
      background-size: {PAGE_W}px {PAGE_H}px;
      background-position: -{margin_side}px -{margin_top}px;
      @bottom-right {{ content: none; }}
    }}
    @page trigger {{
      background: url('{assets}/page_trigger.png') no-repeat;
      background-size: {PAGE_W}px {PAGE_H}px;
      background-position: -{margin_side}px -{margin_top}px;
    }}
    html {{ -weasy-hyphens: auto; hyphens: auto; }}
    body {{
      font-family: 'Carousel';
      color: {cfg["colors"]["text"]};
      font-size: {f["body_size"]}px;
      line-height: {f["body_line_height"]};
      margin: 0;
    }}
    h1 {{
      page: title;
      page-break-after: always;
      text-align: center;
      line-height: {TITLE_LINE_HEIGHT};
      font-weight: 700;
      overflow-wrap: break-word;
      margin: 0;
    }}
    h2.tw {{
      page: trigger-title;
      page-break-before: always;
      page-break-after: always;
      text-align: center;
      margin: {round((p["y1"] - p["y0"] - 2 * pad) * 0.30)}px 0 0 0;
      font-size: {round(f["title_size"] * 0.75)}px;
      font-weight: 700;
      overflow-wrap: break-word;
    }}
    h2.tw .tw-label {{
      display: block;
      font-size: {round(f["title_size"] * 0.45)}px;
      margin-bottom: 30px;
      font-weight: 400;
    }}
    div.trigger {{ page: trigger; }}
    div.normal {{ page: auto; page-break-before: always; }}
    p {{
      break-inside: avoid;
      margin: 0 0 0.9em 0;
      text-align: left;
      overflow-wrap: break-word;
    }}
    strong {{ font-weight: 700; }}
    em {{ font-style: italic; }}
    u {{ text-decoration: underline; text-decoration-thickness: 2px; text-underline-offset: 4px; }}
    s {{ text-decoration: line-through; text-decoration-thickness: 2px; }}
    """
    return (
        f'<!DOCTYPE html><html lang="{cfg["language"]}"><head><meta charset="utf-8">'
        f"<style>{css}</style></head><body>{''.join(body_parts)}</body></html>",
        title_layout["hit_floor"],
    )


def render(md_text: str, template_dir: Path, out_dir: Path) -> dict:
    """Render markdown to PNG slides + alt texts. Returns a result dict."""
    post = parse_markdown(md_text)
    doc_html, title_hit_floor = build_html(post, template_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / "carousel.pdf"
    HTML(string=doc_html).write_pdf(pdf_path)

    # PDF pages -> PNG at exactly 1080x1440
    subprocess.run(
        ["pdftoppm", "-png", "-scale-to-x", str(PAGE_W), "-scale-to-y", str(PAGE_H),
         str(pdf_path), str(out_dir / "folie")],
        check=True,
    )
    slides = sorted(out_dir.glob("folie-*.png"))

    # Alt texts: extract the text of each PDF page, then strip footer noise
    cfg = json.loads((template_dir / "template.json").read_text())
    account = cfg["account"]
    alt_texts = []
    for i in range(1, len(slides) + 1):
        res = subprocess.run(
            ["pdftotext", "-f", str(i), "-l", str(i), str(pdf_path), "-"],
            capture_output=True, text=True, check=True,
        )
        txt = res.stdout.replace("\u2010\n", "").replace("-\n", "")
        txt = " ".join(txt.split())
        txt = txt.replace(account, "").strip()
        txt = re.sub(rf"\s{i}$", "", txt).strip()
        alt_texts.append(txt)

    (out_dir / "alt-texte.txt").write_text(
        "\n\n".join(f"Folie {i + 1}:\n{t}" for i, t in enumerate(alt_texts)),
        encoding="utf-8",
    )
    pdf_path.unlink()

    warnings = []
    if len(slides) > MAX_SLIDES:
        warnings.append(
            f"Achtung: {len(slides)} Folien erzeugt – Instagram erlaubt maximal "
            f"{MAX_SLIDES} pro Karussell. Bitte Text kürzen oder aufteilen."
        )
    if title_hit_floor:
        warnings.append(
            "Achtung: Der Titel ist so lang, dass er auch bei kleinstmöglicher "
            "Schriftgröße nicht vollständig ins Panel passt und leicht übersteht. "
            "Bitte Titel kürzen."
        )
    warning = "\n".join(warnings) if warnings else None
    return {
        "title": post.title,
        "slides": [s.name for s in slides],
        "alt_texts": alt_texts,
        "warning": warning,
    }
