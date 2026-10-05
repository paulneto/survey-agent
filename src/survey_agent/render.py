"""Shared, dependency-free rendering helpers used by BOTH runtimes (the stdlib `server.py` and the
Django reference service). Pure functions, no side effects, stdlib only (`re` + `html.escape`).

- `image_html(media)` — a display-only <img> with sanitized size controls.
- `md_to_html(md)` — a SAFE Markdown subset → HTML (escape-first, allowlisted tags + URL schemes; no
  raw HTML passes). Used for `instructions` bodies and per-question descriptions.
"""
import re
from html import escape

_CSS_LEN = re.compile(r"^\d{1,4}(\.\d+)?(px|%|em|rem|vw|vh)?$|^auto$")


def css_len(v):
    """Allow only a safe CSS length (digits + unit, or 'auto'); blocks style injection. -> value|None."""
    v = str(v or "").strip()
    return v if _CSS_LEN.match(v) else None


def image_html(media):
    """Display-only image stimulus from a URL, with size controls. Responsive by default
    (max-width:100%); width/height/max_width are sanitized; url/alt/caption are escaped."""
    media = media or {}
    url = media.get("url", "")
    if not url:
        return ""
    style = [f"max-width:{css_len(media.get('max_width')) or '100%'}", "display:block", "border-radius:8px"]
    if css_len(media.get("width")):
        style.append(f"width:{css_len(media['width'])}")
    style.append(f"height:{css_len(media['height'])}" if css_len(media.get("height")) else "height:auto")
    alt = media.get("alt") or media.get("caption") or "survey image"
    cap = f'<div class="meta">{escape(media.get("caption"))}</div>' if media.get("caption") else ""
    return f'<img src="{escape(url)}" alt="{escape(alt)}" loading="lazy" style="{";".join(style)}">{cap}'


# ----------------------------------------------------------------- Markdown (a safe, dependency-free subset)
def safe_url(u):
    """Allow only safe URL schemes (blocks javascript: etc.); input is already HTML-escaped."""
    return u if re.match(r"^(https?:|mailto:|/|#|\./|\.\./)", u.strip(), re.I) else "#"


# Backslash escapes (`\*`, `\[`, …) show the character literally instead of formatting with it. The
# engine escapes respondent-supplied text piped into a title this way, so an answer can't become a link
# or image. Protected with placeholders while the formatting regexes run, then restored.
_MD_ESC = re.compile(r"\\([\\`*_\[\]()!#>-])")


def md_inline(text):
    """Inline formatting on a single line. HTML-escapes first, then adds a known-tag subset only."""
    kept = []

    def _hold(m):
        kept.append(m.group(1))
        return f"\x00{len(kept) - 1}\x00"
    t = escape(_MD_ESC.sub(_hold, str(text).replace("\x00", "")))   # NUL is our placeholder marker
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)",
               lambda m: f'<img src="{safe_url(m.group(2))}" alt="{m.group(1)}" loading="lazy" '
                         f'style="max-width:100%;display:block;border-radius:6px;margin:.5em 0">', t)
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
               lambda m: f'<a href="{safe_url(m.group(2))}" target="_blank" rel="noopener">{m.group(1)}</a>', t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])[*_]([^*_]+)[*_](?![\w*])", r"<em>\1</em>", t)
    return re.sub("\x00(\\d+)\x00", lambda m: escape(kept[int(m.group(1))]), t)


def _collect_list(lines, i, marker, strip, block):
    """Collect consecutive list items into one list, folding wrapped continuation lines into the item
    (lazy continuation) so a multi-line item doesn't split the list / restart numbering."""
    items = []
    while i < len(lines) and re.match(marker, lines[i]):
        text = re.sub(strip, "", lines[i]); i += 1
        while i < len(lines) and lines[i].strip() and not block.match(lines[i].strip()):
            text += " " + lines[i].strip(); i += 1
        items.append("<li>" + md_inline(text) + "</li>")
    return items, i


def md_to_html(md):
    """Render a safe Markdown subset → HTML: # ## ### headings, - / * and 1. lists, > blockquotes,
    --- rules, paragraphs, and inline bold/italic/code/links/images. Output is escaped + allowlisted."""
    if not md:
        return ""
    lines = str(md).replace("\r\n", "\n").split("\n")
    out, i = [], 0
    block = re.compile(r"(#{1,3}\s|[-*]\s|\d+\.\s|>)")
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        h = re.match(r"(#{1,3})\s+(.*)", s)
        if h:
            lvl = len(h.group(1)) + 1             # # -> h2, ## -> h3, ### -> h4 (fits inside a card)
            out.append(f"<h{lvl}>{md_inline(h.group(2))}</h{lvl}>"); i += 1; continue
        if s == "---":
            out.append("<hr>"); i += 1; continue
        if re.match(r"[-*]\s+", s):
            items, i = _collect_list(lines, i, r"\s*[-*]\s+", r"^\s*[-*]\s+", block)
            out.append("<ul>" + "".join(items) + "</ul>"); continue
        if re.match(r"\d+\.\s+", s):
            items, i = _collect_list(lines, i, r"\s*\d+\.\s+", r"^\s*\d+\.\s+", block)
            out.append("<ol>" + "".join(items) + "</ol>"); continue
        if s.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(md_inline(re.sub(r"^\s*>\s?", "", lines[i]))); i += 1
            out.append("<blockquote>" + "<br>".join(quote) + "</blockquote>"); continue
        para = []
        while i < len(lines) and lines[i].strip() and not block.match(lines[i].strip()):
            para.append(md_inline(lines[i].strip())); i += 1
        out.append("<p>" + "<br>".join(para) + "</p>")
    return "".join(out)
