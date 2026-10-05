"""UI for API keys — create / list / revoke, at /keys.

Server-rendered (a small form page, no SPA). Sign-in-only: every route requires a signed-in Google
session (NOT an API key), so a key can't be used to mint more keys. A newly-created key is shown
exactly once — we only store its hash.
"""
import datetime
import html

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from survey_agent.api.auth import apikeys, sessions
from survey_agent.api.auth.deps import get_key_store, require_user

router = APIRouter(prefix="/keys", tags=["api-keys"])


def _fmt(ts):
    if not ts:
        return "—"
    try:
        return datetime.datetime.utcfromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


def _row(k) -> str:
    kid = html.escape(str(k.get("id")))
    disabled = bool(k.get("disabled"))
    if disabled:
        action = ""
    else:
        confirm = "return confirm('Revoke this key? Anything using it stops working.')"
        action = (
            f'<form method=post action="/keys/{kid}/revoke" onsubmit="{confirm}">'
            f'<button class=rv>Revoke</button></form>'
        )
    return (
        f"<tr class='{'rev' if disabled else ''}'>"
        f"<td><code>{kid}</code></td>"
        f"<td>{html.escape(str(k.get('label') or ''))}</td>"
        f"<td>{_fmt(k.get('created'))}</td>"
        f"<td>{_fmt(k.get('last_used'))}</td>"
        f"<td>{'revoked' if disabled else 'active'}</td>"
        f"<td>{action}</td>"
        f"</tr>"
    )


def _page(keys, email, new_key=None) -> str:
    rows = "".join(_row(k) for k in keys) or "<tr><td colspan=6 class=muted>No keys yet.</td></tr>"

    banner = ""
    if new_key:
        banner = (
            "<div class=newkey><div class=nk-h>New API key — copy it now, it won't be shown again</div>"
            f"<div class=nk-v><code id=k>{html.escape(new_key)}</code> "
            "<button onclick=\"navigator.clipboard.writeText(document.getElementById('k').innerText)\">Copy</button></div>"
            "<div class=nk-n>Use it as <code>Authorization: Bearer &lt;key&gt;</code> (or <code>X-API-Key</code>) against <code>/v1/*</code>.</div></div>"
        )

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>API keys · survey-agent</title>
<style>
  body{{margin:0;background:#eceef7;color:#171a2b;font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif}}
  .wrap{{max-width:840px;margin:0 auto;padding:32px 20px 80px}}
  h1{{font-size:23px;margin:0 0 2px;letter-spacing:-.3px}}
  .top{{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px}}
  a.nav{{color:#5b3dfe;text-decoration:none;font-size:14px}} a.nav:hover{{text-decoration:underline}}
  .muted{{color:#6b6f85}}
  .card{{background:#fff;border:1px solid #e1e4f2;border-radius:14px;padding:18px 20px;box-shadow:0 6px 26px rgba(23,26,43,.07);margin:16px 0}}
  table{{width:100%;border-collapse:collapse;font-size:14px}}
  th,td{{text-align:left;padding:9px 10px;border-bottom:1px solid #eef0f7}}
  th{{font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;color:#6b6f85}}
  tr.rev{{opacity:.5}}
  code{{background:#f1f0fb;color:#2a2740;padding:1.5px 6px;border-radius:6px;font-family:ui-monospace,Menlo,monospace;font-size:12.5px}}
  input[type=text]{{padding:9px 11px;border:1px solid #d2d6e8;border-radius:9px;font-size:14px;min-width:240px}}
  button{{background:#5b3dfe;color:#fff;border:0;border-radius:9px;padding:9px 16px;font-size:14px;font-weight:600;cursor:pointer}}
  button.rv{{background:#fff;color:#e5484d;border:1px solid #f0c4c6;padding:5px 12px;font-weight:500}}
  form.inline{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
  .newkey{{background:#edfaf2;border:1px solid #bfe8d2;border-radius:12px;padding:14px 16px;margin:8px 0 18px}}
  .nk-h{{font-weight:700;color:#1c8a52;margin-bottom:6px}}
  .nk-v{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}} .nk-v code{{font-size:13.5px}}
  .nk-n{{font-size:12.5px;color:#6b6f85;margin-top:6px}}
</style></head><body><div class=wrap>
  <div class=top><div><h1>API keys</h1><div class=muted>Programmatic / agent access to the survey-agent API.</div></div>
    <div><span class=muted>{html.escape(email)}</span> · <a class=nav href="/auth/logout">Sign out</a></div></div>
  {banner}
  <div class=card>
    <form class=inline method=post action="/keys">
      <input type=text name=label placeholder="Label (e.g. 'authoring-agent', 'jane-cli')" required maxlength=80>
      <button type=submit>Create key</button>
    </form>
    <div class=muted style="font-size:12.5px;margin-top:8px">A key is shown once at creation; only its hash is stored. Give each agent/person its own labelled key so you can revoke individually.</div>
  </div>
  <div class=card>
    <table><thead><tr><th>ID</th><th>Label</th><th>Created</th><th>Last used</th><th>Status</th><th></th></tr></thead>
    <tbody>{rows}</tbody></table>
  </div>
</div></body></html>"""


@router.get("", response_class=HTMLResponse)
def keys_page(request: Request, store: apikeys.KeyStore = Depends(get_key_store)):
    user = sessions.principal_from_request(request)
    if not user:
        return RedirectResponse(url="/auth/google/login", status_code=302)
    return HTMLResponse(_page(store.list(), user["email"]))


@router.post("", response_class=HTMLResponse)
def create_key(label: str = Form(...), user: dict = Depends(require_user),
               store: apikeys.KeyStore = Depends(get_key_store)):
    new_key = store.add(label.strip() or "unlabelled")
    return HTMLResponse(_page(store.list(), user["email"], new_key=new_key))


@router.post("/{key_id}/revoke")
def revoke_key(key_id: str, user: dict = Depends(require_user),
               store: apikeys.KeyStore = Depends(get_key_store)):
    store.revoke(key_id)
    return RedirectResponse(url="/keys", status_code=302)
