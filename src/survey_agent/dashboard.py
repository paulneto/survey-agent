"""Static HTML management dashboard — the survey-operations index.

One self-contained HTML page (like `report.py`): every survey in a project as a card with its
**purpose** (description/tags/owner) and **lifecycle state** (draft → validated → tested → live),
the last test's incidence/LOI, and — when reports exist — a link straight into each survey's HTML
report. Pure-Python string building, inline CSS, no CDN, no server. Reuses `registry.catalog()`.

Used by three surfaces (same renderer):
  • CLI            `survey-agent dashboard`            → writes a file
  • MCP connector  `build_dashboard` tool              → writes a file, returns the path
  • runtime        `survey-agent serve` at `/manage`   → served live (links to live reports)
"""
from html import escape

# state → (badge label, colour). Order also drives the summary counts.
_STATE = {
    "draft":            ("draft",     "#9aa0a6"),
    "validated":        ("validated", "#4285f4"),
    "tested":           ("tested",    "#1e8e3e"),
    "tested(failing)":  ("tested ⚠",  "#e37400"),
    "approved":         ("approved",  "#12805c"),
    "live":             ("live",      "#0b8043"),
    "paused":           ("paused",    "#9aa0a6"),
    "closed":           ("closed",    "#5f6368"),
}

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1f2329;--muted:#6b7280;--border:#e4e7eb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:24px;margin:0 0 2px}.sub{color:var(--muted);margin:0 0 20px}
.summary{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 22px}
.pill{background:var(--card);border:1px solid var(--border);border-radius:999px;padding:4px 12px;font-size:13px}
.pill b{font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px 16px 14px;
 box-shadow:0 1px 2px rgba(0,0,0,.04)}
.card .top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px}
.badge{font-size:11px;font-weight:600;color:#fff;border-radius:6px;padding:2px 8px;white-space:nowrap;text-transform:uppercase;letter-spacing:.02em}
.id{font-weight:600;font-size:16px;word-break:break-word}
.id a{color:inherit;text-decoration:none;border-bottom:1px solid var(--border)}
.id a:hover{border-color:var(--ink)}
.title{color:var(--muted);font-size:13px;margin:0 0 8px}
.meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.tag{font-size:11px;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:1px 7px;color:var(--muted)}
.stat{font-size:12px;color:var(--muted);margin-top:8px}
.stat b{color:var(--ink);font-weight:600}
.live{font-size:12px;margin-top:8px;word-break:break-all}
.live a{color:#1a73e8;text-decoration:none}.live a:hover{text-decoration:underline}
.params{font-size:12px;color:var(--muted);margin-top:4px}
.params code{background:var(--bg);border:1px solid var(--border);border-radius:5px;padding:0 5px;font-size:11px}
.counts{font-size:13px;margin-top:8px;background:#f1f8f4;border:1px solid #cfe8d8;border-radius:8px;padding:6px 10px}
.counts b{font-size:16px;color:#0b8043;font-weight:700}
.quota{font-size:12px;color:var(--muted);margin-top:4px}
.note{font-size:12px;color:var(--muted);margin-top:8px;font-style:italic}
.foot{color:var(--muted);font-size:12px;margin-top:28px;border-top:1px solid var(--border);padding-top:14px}
a.report{display:inline-block;margin-top:10px;font-size:13px;color:#1a73e8;text-decoration:none}
a.report:hover{text-decoration:underline}
.empty{background:var(--card);border:1px dashed var(--border);border-radius:12px;padding:32px;text-align:center;color:var(--muted)}
"""


def _card(e, report_href=None, info=None):
    state = e.get("state", "draft")
    label, colour = _STATE.get(state, (state, "#9aa0a6"))
    sid = escape(str(e.get("id", "?")))
    id_html = f'<a href="{escape(report_href)}">{sid}</a>' if report_href else sid
    parts = [f'<div class="card"><div class="top"><span class="id">{id_html}</span>'
             f'<span class="badge" style="background:{colour}">{escape(label)}</span></div>']
    if e.get("title"):
        parts.append(f'<div class="title">{escape(str(e["title"]))}</div>')
    if e.get("description"):
        parts.append(f'<div style="font-size:13px">{escape(str(e["description"]))}</div>')
    # Live operations block — drop-in URL, accepted params, and counts pulled from the store.
    # Only rendered when `info` is supplied (the CLI's --live/--reports/--simulate paths).
    if info:
        if info.get("url"):
            u = escape(str(info["url"]))
            parts.append(f'<div class="live">🔗 <a href="{u}">{u}</a></div>')
        if info.get("params"):
            ps = " · ".join(f'<code>{escape(str(p))}</code>' for p in info["params"])
            parts.append(f'<div class="params">params: {ps} '
                         f'<span style="opacity:.7">(+ any ?param → url_variables)</span></div>')
        if info.get("completed") is not None:
            cbits = [f'<b>{info["completed"]}</b> completed']
            if info.get("n"):
                cbits.append(f'{info["n"]} started')
            if info.get("incidence") is not None:
                cbits.append(f'{info["incidence"]}% incidence')
            if info.get("loi_median"):
                cbits.append(f'LOI {info["loi_median"]}m')
            parts.append(f'<div class="counts">📥 {" · ".join(cbits)}</div>')
            for q in info.get("quotas") or []:
                lim = q.get("limit")
                parts.append(f'<div class="quota">quota <b>{escape(str(q.get("id")))}</b> '
                             f'{q.get("count", 0)}/{lim if lim is not None else "∞"}</div>')
    t = e.get("tested") or {}
    bits = []
    if t.get("incidence") is not None:
        bits.append(f'incidence <b>{round(100 * t["incidence"])}%</b>')
    if t.get("loi_min"):
        bits.append(f'LOI <b>{t["loi_min"]}m</b>')
    if t.get("at"):
        bits.append(f'tested <b>{escape(str(t["at"])[:10])}</b>')
    if (e.get("live") or {}).get("url"):
        bits.append(f'<b><a href="{escape(e["live"]["url"])}" style="color:inherit">live ↗</a></b>')
    if e.get("owner"):
        bits.append(f'owner <b>{escape(str(e["owner"]))}</b>')
    bits.append(f'v{e.get("version", 1)}')
    parts.append(f'<div class="stat">{" · ".join(bits)}</div>')
    if e.get("tags"):
        parts.append('<div class="meta">' + "".join(f'<span class="tag">{escape(str(x))}</span>'
                                                     for x in e["tags"]) + '</div>')
    if e.get("note"):
        parts.append(f'<div class="note">“{escape(str(e["note"]))}”</div>')
    if e.get("missing_file"):
        parts.append('<div class="note">⚠ no survey file (registry entry only)</div>')
    if report_href:
        parts.append(f'<a class="report" href="{escape(report_href)}">open report →</a>')
    parts.append("</div>")
    return "".join(parts)


def render_html(catalog, report_links=None, live=None, title="survey-agent — surveys", subtitle=None):
    """Render the management grid. `report_links` maps survey_id → href (a relative file for the CLI,
    or `/manage/<id>/report` for the served console); a card with a link deep-links to that report.
    `live` (optional) maps survey_id → {url, params, n, completed, incidence, loi_median, quotas}; when
    given, each card shows that survey's drop-in URL, accepted params, and live counts from the store."""
    report_links = report_links or {}
    live = live or {}
    counts = {}
    for e in catalog:
        counts[e.get("state", "draft")] = counts.get(e.get("state", "draft"), 0) + 1
    pills = "".join(
        f'<span class="pill"><b>{counts[s]}</b> {escape(_STATE.get(s, (s, ""))[0])}</span>'
        for s in _STATE if s in counts)
    total_completed = sum(v.get("completed") or 0 for v in live.values() if v.get("completed") is not None)
    if any(v.get("completed") is not None for v in live.values()):
        pills = (f'<span class="pill" style="background:#f1f8f4;border-color:#cfe8d8">'
                 f'<b>{total_completed}</b> completes (live)</span>') + pills
    sub = subtitle if subtitle is not None else f"{len(catalog)} survey(s) · purpose + lifecycle state"
    if catalog:
        cards = "".join(_card(e, report_links.get(e.get("id")), live.get(e.get("id"))) for e in catalog)
        grid = f'<div class="summary">{pills}</div><div class="grid">{cards}</div>'
    else:
        grid = ('<div class="empty">No surveys yet. Create one with '
                '<code>survey-agent author "&lt;brief&gt;"</code>.</div>')
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><style>{_CSS}</style></head><body><div class="wrap">'
            f'<h1>{escape(title)}</h1><p class="sub">{escape(sub)}</p>{grid}'
            f'<div class="foot">survey-agent dashboard · state lives in <code>.registry.json</code> '
            f'(it never touches the immutable survey definitions).</div>'
            f'</div></body></html>')
