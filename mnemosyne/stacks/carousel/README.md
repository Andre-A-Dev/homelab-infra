# Carousel

Accessible Instagram carousel generator. Turns a Markdown post into
ready-to-upload 1080x1440 px (3:4) PNG slides — title slide, auto-paginated
text slides, and trigger-warning slides with their own background — plus a
matching alt-text file. Built for independent use by a blind author with
VoiceOver on iOS (semantic HTML, native form elements, no JavaScript).

## How it works

```
Markdown -> parser -> paged HTML/CSS -> WeasyPrint (PDF) -> pdftoppm (PNGs)
                                                         -> pdftotext (alt texts)
```

Layout is deterministic: WeasyPrint paginates the text across fixed-size
pages, so text overflow is impossible by construction. No browser engine,
no Playwright — runs light on a Raspberry Pi.

## Markdown conventions

```markdown
# Post title                -> title slide (no page number)
Paragraphs separated by     -> text slides, split automatically
blank lines.
## TW: topic                -> trigger title slide (own background)
Trigger content...          -> trigger text slides
## ENDE TW                  -> back to normal slides
```

`**bold**` and `*italic*` are supported inline. Everything else is treated
as plain text.

## Inline formatting

A deliberately small, fixed set — enough for emphasis, not a full Markdown
dialect:

| Markdown | Result |
|---|---|
| `**text**` | bold |
| `*text*` | italic |
| `***text***` | bold + italic |
| `__text__` | underlined |
| `~~text~~` | strikethrough |

No nested combinations beyond bold+italic, no links, no lists — the goal is
predictable rendering the author can reason about without a preview.

## Templates

Each design lives in `templates_ig/<id>/`:

```
templates_ig/wolfkin/
├── template.json      # colors, panel geometry, fonts, footer, account name
└── assets/
    ├── bg_normal_src.png / bg_trigger_src.png   # source artwork
    ├── logo_*.png                               # decorations
    ├── fonts/                                   # TTFs (OFL-licensed Cabin)
    └── page_*.png                               # generated page backgrounds
```

Adding a new template: create the folder, drop in artwork + `template.json`,
then bake the page backgrounds once:

```bash
python3 scripts/build_backgrounds.py templates_ig/<id>
```

The frontend picks up every folder containing a `template.json` automatically.

## Visual template editor

`tools/template-editor.html` is a standalone, offline design tool for
iterating on a template without touching Python or regenerating slides.
Open it directly in a browser (no server needed):

```bash
open tools/template-editor.html   # or just double-click it
```

It re-implements `scripts/build_backgrounds.py`'s compositing (cover-crop,
rounded panel with alpha, logo placement) on an HTML canvas, and
`app/renderer.py`'s text-overlay math (title auto-shrink, margins, footer
position) as absolutely-positioned HTML — both verified pixel/formula-exact
against the Python originals.

Workflow:

1. Load raw source art (background photos, logo PNGs, the two font files)
   in the **Assets** panel — or **Projekt → template.json laden** to start
   from an existing template (re-upload its assets by the filenames shown).
2. Adjust panel geometry, colors, fonts and logo placement in the sidebar;
   the four page types (Titel / Text / TW-Titel / TW-Text) update live.
3. **Export → template.json** plus the three `page_*.png` backgrounds —
   drop all four straight into `templates_ig/<id>/assets/`, no
   `build_backgrounds.py` run required.

The footer position is a close approximation of WeasyPrint's print-margin
box model (documented in the tool itself) — everything else is exact.
Do one real render after exporting to confirm the footer sits right.

## Deployment (Mnemosyne)

```bash
cd ~/stacks/carousel
docker compose up -d --build
```

Caddyfile block:

```
carousel.home {
    tls internal
    reverse_proxy carousel:5000
}
```

Pi-hole local DNS: `carousel.home -> 192.168.1.x`. For access from the
Fuchsbau network, add the same proxy block to the Zephyros Caddyfile
(pointing at Mnemosyne's Tailscale IP) and a Pi-hole record on Zephyros.

Job output is kept under `/mnt/codex/carousel/jobs/` (last 30 renders,
older ones are pruned automatically).

## Instagram format notes

- 1080x1440 (3:4) matches the current feed and profile-grid display;
  upload via the Instagram app ('Original' crop) to keep the full frame.
- Carousel limit is 20 slides; the app warns when a post exceeds it.
- The Meta API still caps at 4:5, so scheduled/API posting would require
  a 1080x1350 variant (`PAGE_W/PAGE_H` in `app/renderer.py`).

## Font

Candara is a Microsoft font and cannot be redistributed; the template uses
[Cabin](https://fonts.google.com/specimen/Cabin) (SIL Open Font License),
a humanist sans-serif close in character.
