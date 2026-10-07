"""Carousel renderer: Markdown -> paged HTML -> PDF (WeasyPrint) -> PNG slides.

Layout is fully deterministic: WeasyPrint paginates the text across
1080x1440 px pages, so overflow is impossible by construction. Paragraphs
are kept together (break-inside: avoid) unless a single paragraph exceeds
one page, in which case CSS forces a clean break.

The title slide is special-cased: its text is word-wrapped and its font
size is computed by actually measuring the title against the real font
file (Pillow), so it fills the available panel — both width and height —
regardless of title length. The title never spills onto a second slide.

Markdown conventions:
  # Title            -> title slide (no page number)
  ## TW: <topic>     -> trigger title slide, following text on trigger pages
  ## ENDE TW         -> back to normal pages
  ---                -> force the next paragraph onto a new slide
  blank line         -> paragraph break; inline formatting: see Formatter
"""

import html
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageFont
from weasyprint import HTML

PAGE_W, PAGE_H = 1080, 1440
MAX_SLIDES = 20  # Instagram carousel limit

# Absolute floor so a pathologically long title still produces something
# legible-ish rather than shrinking towards zero.
TITLE_HARD_MIN_SIZE = 24
TITLE_LINE_HEIGHT = 1.15  # keep in sync with the h1 CSS below

TW_START = re.compile(r"^##\s*TW\s*:?\s*(.*)$", re.IGNORECASE)
TW_END = re.compile(r"^##\s*ENDE\s+TW\s*$", re.IGNORECASE)
TITLE = re.compile(r"^#\s+(.*)$")
PAGE_BREAK = re.compile(r"^\s*-{3,}\s*$")

# Color highlight: {name:text}, name defined per template in "highlights".
HIGHLIGHT = re.compile(r"\{([A-Za-zÄÖÜäöüß]+)\s*:\s*(.+?)\}")
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Emphasis delimiters, removed for width measurement only.
_MD_STRIP = re.compile(r"\*\*\*|\*\*|\*|__|~~")


def _plain(text: str) -> str:
    """Visible text of a formatted line: markup removed exactly the way
    Formatter consumes it, so word boundaries line up with the HTML."""
    return _MD_STRIP.sub("", HIGHLIGHT.sub(r"\2", text))

# progress(stage_label, percent, detail)
ProgressFn = Callable[[str, int, str], None]


class ParseError(ValueError):
    pass


@dataclass
class RenderOptions:
    hyphenate: bool = False  # off by default: words move whole to the next line


@dataclass
class Paragraph:
    text: str
    break_before: bool = False  # set by a preceding '---' line


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
    pending_break = False

    def flush():
        nonlocal pending_break
        para = " ".join(s.strip() for s in buf).strip()
        if para:
            current.paragraphs.append(Paragraph(para, break_before=pending_break))
            pending_break = False
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
            pending_break = False  # a new section starts on a new slide anyway
            continue
        m = TW_START.match(line.strip())
        if m:
            flush()
            push_section()
            current = Section("trigger", tw_topic=m.group(1).strip())
            pending_break = False
            continue
        if PAGE_BREAK.match(line):
            flush()
            # Only meaningful once there is body text; a '---' directly
            # after the title is a no-op because the text starts on a
            # new slide regardless.
            pending_break = title is not None
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


class Formatter:
    """Inline markup -> HTML for one template.

    Supported (longest/most specific marker first so overlapping markers
    can't misparse each other):
      {name:text} -> color highlight, name from template "highlights"
      ***text***  -> bold + italic
      **text**    -> bold
      *text*      -> italic
      __text__    -> underline
      ~~text~~    -> strikethrough

    Unknown color names render the text uncolored (never literal braces)
    and are collected in `unknown_colors` for a warning.
    """

    def __init__(self, highlights: dict):
        self.highlights = {
            name.lower(): color for name, color in (highlights or {}).items()
            if HEX_COLOR.match(color or "")
        }
        self.unknown_colors: set[str] = set()

    def _color(self, m: re.Match) -> str:
        name, inner = m.group(1).lower(), m.group(2)
        color = self.highlights.get(name)
        if color is None:
            self.unknown_colors.add(m.group(1))
            return inner
        return f'<span style="color:{color}">{inner}</span>'

    def __call__(self, text: str) -> str:
        t = html.escape(text)
        t = HIGHLIGHT.sub(self._color, t)
        t = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"\*(.+?)\*", r"<em>\1</em>", t)
        t = re.sub(r"__(.+?)__", r"<u>\1</u>", t)
        t = re.sub(r"~~(.+?)~~", r"<s>\1</s>", t)
        return t


def _break_lines(formatted: str, counts: list[int]) -> str:
    """Insert <br> into already-formatted HTML after the given word counts.

    Formatting is applied to the whole title first and line breaks are
    placed afterwards, so markup spanning a line break (e.g. a bold or
    colored phrase across two lines) stays intact. Words are counted in
    text nodes only, never inside tags.
    """
    break_after = set()
    total = 0
    for n in counts[:-1]:
        total += n
        break_after.add(total)

    out, words_seen, in_word = [], 0, False
    for part in re.split(r"(<[^>]+>)", formatted):
        if part.startswith("<"):
            out.append(part)
            continue
        for tok in re.split(r"(\s+)", part):
            if not tok:
                continue
            if tok.isspace():
                if in_word and words_seen in break_after:
                    out.append("<br>")
                else:
                    out.append(tok)
                in_word = False
            else:
                if not in_word:
                    words_seen += 1
                    in_word = True
                out.append(tok)
    return "".join(out)


def _title_font(font_path: Path, size: int) -> "ImageFont.FreeTypeFont":
    """Title font as WeasyPrint renders it: the h1 is bold, and for a
    variable font WeasyPrint uses the real wght=700 instance, which is up
    to ~2.5 % wider than the default (regular) instance Pillow loads.
    Static fonts keep their regular outlines; the 4 % fit margin in
    _title_layout covers synthetic bold there.
    """
    font = ImageFont.truetype(str(font_path), size)
    try:
        axes = font.get_variation_axes()
    except OSError:  # not a variable font
        return font
    values = []
    for axis in axes:
        name = axis["name"].decode() if isinstance(axis["name"], bytes) else str(axis["name"])
        values.append(min(700, axis["maximum"]) if name.lower() == "weight" else axis["default"])
    font.set_variation_by_axes(values)
    return font


def _greedy_wrap_counts(words: list[str], font: "ImageFont.FreeTypeFont", max_width: float) -> list[int]:
    """Greedy word-wrap: number of words per line at this font size."""
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
    """Largest font size at which the word-wrapped title fits the panel.

    Scans font sizes top-down; a size is accepted only if the total block
    height fits AND every resulting line fits the width (a single long
    word alone on a line is otherwise never width-checked).
    Returns {size, counts, hit_floor}; counts = words per line.
    """
    plain_words = _plain(title).split()

    upper_bound = max(TITLE_HARD_MIN_SIZE, round(max_height / TITLE_LINE_HEIGHT) + 10)
    chosen_size = TITLE_HARD_MIN_SIZE
    chosen_counts = None
    for size in range(upper_bound, TITLE_HARD_MIN_SIZE - 1, -1):
        font = _title_font(font_path, size)
        counts = _greedy_wrap_counts(plain_words, font, max_width)
        if len(counts) * size * TITLE_LINE_HEIGHT > max_height:
            continue
        idx, widest = 0, 0.0
        for n in counts:
            widest = max(widest, font.getlength(" ".join(plain_words[idx : idx + n])))
            idx += n
        if widest <= max_width:
            chosen_size, chosen_counts = size, counts
            break

    hit_floor = chosen_counts is None
    if chosen_counts is None:
        font = _title_font(font_path, TITLE_HARD_MIN_SIZE)
        chosen_counts = _greedy_wrap_counts(plain_words, font, max_width)

    return {"size": chosen_size, "counts": chosen_counts, "hit_floor": hit_floor}


def _title_layout(title: str, cfg: dict, fmt: Formatter) -> dict:
    p, pad = cfg["panel"], cfg["content_padding"]
    content_w = p["x1"] - p["x0"] - 2 * pad
    content_h = p["y1"] - p["y0"] - 2 * pad
    font_path = (Path(cfg["_template_dir"]) / "assets").resolve() / cfg["font"]["regular"]
    # 4 % breathing room (and synthetic-bold headroom for static fonts)
    layout = _fit_title_layout(title, font_path, content_w * 0.96, content_h * 0.96)
    layout["html"] = _break_lines(fmt(title), layout["counts"])
    total_height = len(layout["counts"]) * layout["size"] * TITLE_LINE_HEIGHT
    layout["margin_top"] = max(0, round((content_h - total_height) / 2))
    return layout


def _snippet(text: str, words: int = 5) -> str:
    """First few visible words of a paragraph, to identify it by ear."""
    w = _plain(text).split()
    return " ".join(w[:words]) + (" …" if len(w) > words else "")


def _walk(box):
    yield box
    for child in getattr(box, "children", None) or []:
        yield from _walk(child)


def _describe_pages(document, paragraphs: list[dict], tw_prefix: str) -> list[str]:
    """One plain-language line per slide saying what is on it, including
    where a paragraph was split across slides. Read from WeasyPrint's
    finished layout, so it reflects the actual pagination.

    Uses WeasyPrint's page box tree (a semi-internal API); if that ever
    changes, the overview is simply omitted instead of breaking the render.
    """
    try:
        per_page = []
        for page in document.pages:
            items, seen = [], set()
            for box in _walk(page._page_box):
                el = getattr(box, "element", None)
                if el is None:
                    continue
                tag = getattr(box, "element_tag", None)
                if tag == "h1" and "title" not in seen:
                    items.append(("title", None)); seen.add("title")
                elif tag == "h2" and "tw" not in seen:
                    items.append(("tw", el.get("data-tw") or "")); seen.add("tw")
                elif tag == "p" and el.get("data-para"):
                    n = int(el.get("data-para"))
                    if n not in seen:
                        items.append(("p", n)); seen.add(n)
            per_page.append(items)
    except Exception:
        return []

    first, last = {}, {}
    for i, items in enumerate(per_page, 1):
        for kind, n in items:
            if kind == "p":
                first.setdefault(n, i)
                last[n] = i

    lines = []
    for i, items in enumerate(per_page, 1):
        parts, trigger = [], False
        for kind, n in items:
            if kind == "title":
                parts.append("Titel")
            elif kind == "tw":
                parts.append(f"{tw_prefix}: {n}" if n else tw_prefix)
            else:
                info = paragraphs[n - 1]
                trigger = trigger or info["trigger"]
                label = f"Absatz {n} („{info['snippet']}“)"
                if first[n] == last[n]:
                    parts.append(label)
                elif i == first[n]:
                    parts.append(f"{label}: Anfang, geht auf Folie {i + 1} weiter")
                elif i == last[n]:
                    parts.append(f"{label}: Ende, Anfang auf Folie {first[n]}")
                else:
                    parts.append(f"{label}: Mittelteil")
        where = " (Trigger-Bereich)" if trigger else ""
        lines.append(f"Folie {i}{where}: " + ("; ".join(parts) if parts else "leer"))
    return lines


def build_html(post: Post, template_dir: Path, options: RenderOptions) -> tuple[str, dict]:
    cfg = json.loads((template_dir / "template.json").read_text())
    cfg["_template_dir"] = str(template_dir)
    assets = (template_dir / "assets").resolve().as_uri()
    f = cfg["font"]
    p = cfg["panel"]
    pad = cfg["content_padding"]
    margin_top = p["y0"] + pad
    margin_side = p["x0"] + pad
    margin_bottom = PAGE_H - p["y1"] + pad
    footer_h = PAGE_H - p["y1"]

    fmt = Formatter(cfg.get("highlights", {}))
    title_layout = _title_layout(post.title, cfg, fmt)

    paragraphs = []  # [{"snippet", "trigger"}], index + 1 == data-para

    def para_html(par: Paragraph, trigger: bool) -> str:
        paragraphs.append({"snippet": _snippet(par.text), "trigger": trigger})
        cls = ' class="break"' if par.break_before else ""
        return f'<p{cls} data-para="{len(paragraphs)}">{fmt(par.text)}</p>'

    body_parts = [
        f'<h1 style="font-size:{title_layout["size"]}px; margin-top:{title_layout["margin_top"]}px">'
        + title_layout["html"]
        + "</h1>"
    ]
    for sec in post.sections:
        if sec.kind == "trigger":
            topic = fmt(sec.tw_topic) if sec.tw_topic else ""
            body_parts.append(
                f'<h2 class="tw" data-tw="{html.escape(_plain(sec.tw_topic), quote=True)}"><span class="tw-label">{html.escape(cfg["trigger_title_prefix"])}</span>'
                + (f"<br>{topic}" if topic else "")
                + "</h2>"
            )
            body_parts.append('<div class="trigger">')
        else:
            body_parts.append('<div class="normal">')
        body_parts.extend(para_html(par, sec.kind == "trigger") for par in sec.paragraphs)
        body_parts.append("</div>")

    hyphens = "auto" if options.hyphenate else "manual"

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
      hyphens: manual;
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
      hyphens: manual;
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
      hyphens: {hyphens};
    }}
    p.break {{ break-before: page; }}
    strong {{ font-weight: 700; }}
    em {{ font-style: italic; }}
    u {{ text-decoration: underline; text-decoration-thickness: 2px; text-underline-offset: 4px; }}
    s {{ text-decoration: line-through; text-decoration-thickness: 2px; }}
    """
    return (
        f'<!DOCTYPE html><html lang="{cfg["language"]}"><head><meta charset="utf-8">'
        f"<style>{css}</style></head><body>{''.join(body_parts)}</body></html>",
        {"title_hit_floor": title_layout["hit_floor"],
         "unknown_colors": sorted(fmt.unknown_colors),
         "known_colors": sorted(fmt.highlights),
         "paragraphs": paragraphs},
    )


def render(
    md_text: str,
    template_dir: Path,
    out_dir: Path,
    options: Optional[RenderOptions] = None,
    progress: Optional[ProgressFn] = None,
) -> dict:
    """Render markdown to PNG slides + alt texts. Returns a result dict."""
    options = options or RenderOptions()

    def report(label: str, percent: int, detail: str = "") -> None:
        if progress:
            progress(label, percent, detail)

    report("Text wird gelesen", 2)
    post = parse_markdown(md_text)
    doc_html, info = build_html(post, template_dir, options)

    report("Layout wird berechnet", 8)
    out_dir.mkdir(parents=True, exist_ok=True)
    document = HTML(string=doc_html).render()
    n_pages = len(document.pages)
    tw_prefix = json.loads((template_dir / "template.json").read_text())["trigger_title_prefix"]
    layout = _describe_pages(document, info["paragraphs"], tw_prefix)
    pdf_path = out_dir / "carousel.pdf"
    document.write_pdf(pdf_path)

    # Rasterize page by page so progress can be reported per slide.
    # Zero-padded names keep alphabetical order == slide order (>9 slides).
    digits = max(2, len(str(n_pages)))
    slides = []
    for i in range(1, n_pages + 1):
        report("Folien werden gezeichnet", 15 + round(70 * (i - 1) / n_pages), f"Folie {i} von {n_pages}")
        stem = out_dir / f"folie-{i:0{digits}d}"
        # Rasterize to uncompressed PPM, then encode the PNG with Pillow:
        # pdftoppm's own PNG writer uses maximum zlib compression, which
        # takes ~20x longer than the rasterizing itself on photographic
        # backgrounds (3.6 s vs 0.2 s per slide). Level 3 is lossless too,
        # just ~5-10 % larger.
        subprocess.run(
            ["pdftoppm", "-singlefile", "-f", str(i), "-l", str(i),
             "-scale-to-x", str(PAGE_W), "-scale-to-y", str(PAGE_H),
             str(pdf_path), str(stem)],
            check=True,
        )
        ppm = stem.with_name(stem.name + ".ppm")
        with Image.open(ppm) as im:
            im.save(stem.with_name(stem.name + ".png"), compress_level=3)
        ppm.unlink()
        slides.append(f"{stem.name}.png")

    report("Alternativtexte werden erstellt", 88)
    cfg = json.loads((template_dir / "template.json").read_text())
    account = cfg["account"]
    alt_texts = []
    for i in range(1, n_pages + 1):
        res = subprocess.run(
            ["pdftotext", "-f", str(i), "-l", str(i), str(pdf_path), "-"],
            capture_output=True, text=True, check=True,
        )
        # Re-join words split by hyphenation (U+2010 or '-' at line end)
        txt = res.stdout.replace("\u2010\n", "").replace("-\n", "")
        txt = " ".join(txt.split())
        txt = txt.replace(account, "").strip()
        txt = re.sub(rf"\s{i}$", "", txt).strip()  # trailing page number
        alt_texts.append(txt)

    (out_dir / "alt-texte.txt").write_text(
        "\n\n".join(f"Folie {i + 1}:\n{t}" for i, t in enumerate(alt_texts)),
        encoding="utf-8",
    )
    pdf_path.unlink()

    warnings = []
    if n_pages > MAX_SLIDES:
        warnings.append(
            f"{n_pages} Folien erzeugt – Instagram erlaubt maximal {MAX_SLIDES} pro Beitrag. "
            f"Die Folien {MAX_SLIDES + 1} bis {n_pages} sind unten markiert. "
            f"Bitte Text kürzen oder auf zwei Beiträge aufteilen."
        )
    if info["title_hit_floor"]:
        warnings.append(
            "Der Titel ist so lang, dass er auch bei kleinstmöglicher Schriftgröße "
            "nicht vollständig ins Panel passt und leicht übersteht. Bitte Titel kürzen."
        )
    if info["unknown_colors"]:
        known = ", ".join(info["known_colors"]) or "keine"
        warnings.append(
            "Unbekannte Farbe(n): " + ", ".join(info["unknown_colors"])
            + f". Diese Stellen sind ohne Farbe gedruckt. Verfügbar in dieser Vorlage: {known}."
        )
    return {
        "title": _plain(post.title),
        "slides": slides,
        "alt_texts": alt_texts,
        "warnings": warnings,
        "limit": MAX_SLIDES,
        "layout": layout,
    }
