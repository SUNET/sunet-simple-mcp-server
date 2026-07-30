"""
Web GUI for the SUNET MCP Filter server.

Runs as its OWN process, separate from mcp_server.py / cli.py. It shares
state with the MCP server only via the SQLite file that filter_state.py
points at (FILTER_DB_PATH env var, same value must be used by both
processes if you don't run them from the same directory).

Run with:
    uvicorn webgui:app --host 0.0.0.0 --port 8421

Optional auth:
    Set WEBGUI_PASSWORD to require a login before the dashboard is usable.
    If it's unset, the GUI is open to anyone who can reach the port -- fine
    for a trusted internal network, not fine for the public internet.
    Set WEBGUI_SECRET_KEY to a random string in production (used to sign
    the session cookie). A default is generated at startup otherwise, which
    means sessions won't survive a restart.
"""

import os
import secrets

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

from filter_state import runtime_config

WEBGUI_PASSWORD = os.getenv("WEBGUI_PASSWORD")  # None => auth disabled
WEBGUI_SECRET_KEY = os.getenv("WEBGUI_SECRET_KEY", secrets.token_hex(32))

# A curated shortlist of Presidio entity types relevant to this deployment.
# Users can still type a custom entity code the UI doesn't list.
COMMON_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "LOCATION",
    "IP_ADDRESS",
    "URL",
    "CREDIT_CARD",
    "IBAN_CODE",
    "DATE_TIME",
    "NRP",
]

app = FastAPI(title="SUNET MCP Filter - Web GUI")
app.add_middleware(SessionMiddleware, secret_key=WEBGUI_SECRET_KEY)


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------
def auth_enabled() -> bool:
    return bool(WEBGUI_PASSWORD)


def require_login(request: Request):
    if not auth_enabled():
        return
    if not request.session.get("authed"):
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})


@app.exception_handler(HTTPException)
async def redirect_to_login(request: Request, exc: HTTPException):
    if exc.status_code == status.HTTP_303_SEE_OTHER and exc.headers and exc.headers.get("Location") == "/login":
        return RedirectResponse(url="/login", status_code=303)
    raise exc


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if not auth_enabled():
        return RedirectResponse(url="/", status_code=303)
    error = request.query_params.get("error")
    error_html = '<p class="error">Wrong password.</p>' if error else ""
    return HTMLResponse(PAGE_LOGIN.replace("__ERROR__", error_html))


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    if form.get("password") == WEBGUI_PASSWORD:
        request.session["authed"] = True
        return RedirectResponse(url="/", status_code=303)
    return RedirectResponse(url="/login?error=1", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login" if auth_enabled() else "/", status_code=303)


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _=Depends(require_login)):
    logout_button = '<button onclick="logout()">Log out</button>' if auth_enabled() else ""
    page = PAGE_DASHBOARD.replace("__COMMON_ENTITIES__", str(COMMON_ENTITIES))
    page = page.replace("__LOGOUT_BUTTON__", logout_button)
    return HTMLResponse(page)


# --------------------------------------------------------------------------
# JSON API consumed by the dashboard's JS
# --------------------------------------------------------------------------
class EntityBody(BaseModel):
    entity: str


class TokenBody(BaseModel):
    entity: str
    token: str


@app.get("/api/clients")
async def api_list_clients(_=Depends(require_login)):
    clients = runtime_config.get_current_clients()
    result = {}
    for cid in clients:
        entities, tokens = runtime_config.get_current(cid)
        result[cid] = {"entities": entities, "tokens": tokens}
    return JSONResponse(result)


@app.get("/api/clients/{client_id}")
async def api_get_client(client_id: str, _=Depends(require_login)):
    entities, tokens = runtime_config.get_current(client_id)
    return {"entities": entities, "tokens": tokens}


@app.post("/api/clients/{client_id}/entities")
async def api_add_entity(client_id: str, body: EntityBody, _=Depends(require_login)):
    entity = body.entity.strip().upper()
    if not entity:
        raise HTTPException(400, "Entity name can't be empty.")
    entities, tokens = runtime_config.get_current(client_id)
    if entity not in entities:
        entities.append(entity)
        runtime_config.update(client_id, entities, tokens)
    return {"entities": entities, "tokens": tokens}


@app.delete("/api/clients/{client_id}/entities/{entity}")
async def api_remove_entity(client_id: str, entity: str, _=Depends(require_login)):
    entity = entity.strip().upper()
    entities, tokens = runtime_config.get_current(client_id)
    if entity in entities:
        entities.remove(entity)
        runtime_config.update(client_id, entities, tokens)
    return {"entities": entities, "tokens": tokens}


@app.post("/api/clients/{client_id}/tokens")
async def api_set_token(client_id: str, body: TokenBody, _=Depends(require_login)):
    entity = body.entity.strip().upper()
    entities, tokens = runtime_config.get_current(client_id)
    tokens[entity] = body.token
    runtime_config.update(client_id, entities, tokens)
    return {"entities": entities, "tokens": tokens}


@app.delete("/api/clients/{client_id}")
async def api_reset_client(client_id: str, _=Depends(require_login)):
    runtime_config.delete_client(client_id)
    return {"status": "reset"}


# --------------------------------------------------------------------------
# Templates (kept inline so this is a single self-contained file)
# --------------------------------------------------------------------------
BASE_STYLE = """
<style>
  :root {
    --bg: #0f1420; --panel: #171d2b; --border: #2a3247;
    --text: #e7eaf2; --muted: #8891a7; --accent: #5b8cff;
    --danger: #e5584f; --ok: #3fbf7f;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); min-height: 100vh;
  }
  header {
    padding: 20px 32px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }
  header h1 { font-size: 17px; font-weight: 600; margin: 0; letter-spacing: .3px; }
  header h1 span { color: var(--muted); font-weight: 400; }
  main { padding: 28px 32px; max-width: 920px; margin: 0 auto; }
  button {
    background: var(--accent); color: white; border: none; border-radius: 6px;
    padding: 7px 14px; font-size: 13px; cursor: pointer; font-weight: 500;
  }
  button.secondary { background: transparent; border: 1px solid var(--border); color: var(--text); }
  button.danger { background: var(--danger); }
  button:hover { filter: brightness(1.1); }
  input, select {
    background: #10131e; border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 7px 10px; font-size: 13px;
  }
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 18px 20px; margin-bottom: 16px;
  }
  .client-name { font-size: 15px; font-weight: 600; margin-bottom: 10px; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
  .tag {
    display: inline-flex; align-items: center; gap: 6px; background: #1d2536;
    border: 1px solid var(--border); border-radius: 999px; padding: 4px 6px 4px 12px;
    font-size: 12px;
  }
  .tag button { padding: 2px 7px; font-size: 11px; border-radius: 999px; }
  .muted { color: var(--muted); font-size: 12.5px; }
  .section-label { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 12px 0 6px; }
  .token-row { display: grid; grid-template-columns: 1fr 1fr auto; gap: 6px; margin-bottom: 6px; align-items: center; }
  .empty-state { color: var(--muted); text-align: center; padding: 60px 0; }
  .toolbar { display: flex; gap: 8px; margin-bottom: 20px; }
  .login-box {
    max-width: 340px; margin: 12vh auto; background: var(--panel);
    border: 1px solid var(--border); border-radius: 10px; padding: 28px;
  }
  .login-box h1 { font-size: 16px; margin-bottom: 16px; }
  .login-box input { width: 100%; margin-bottom: 12px; }
  .login-box button { width: 100%; }
  .error { color: var(--danger); font-size: 12.5px; margin-top: -4px; margin-bottom: 12px; }
</style>
"""

PAGE_LOGIN = """<!doctype html><html><head><meta charset="utf-8">
<title>MCP Filter - Login</title>""" + BASE_STYLE + """</head>
<body>
  <div class="login-box">
    <h1>SUNET MCP Filter</h1>
    <form method="post" action="/login">
      __ERROR__
      <input type="password" name="password" placeholder="Password" autofocus required>
      <button type="submit">Log in</button>
    </form>
  </div>
</body></html>"""

PAGE_DASHBOARD = """<!doctype html><html><head><meta charset="utf-8">
<title>MCP Filter - Dashboard</title>""" + BASE_STYLE + """</head>
<body>
<header>
  <h1>SUNET MCP Filter <span>&mdash; PII filter settings</span></h1>
  __LOGOUT_BUTTON__
</header>
<main>
  <div class="toolbar">
    <input id="new-client-id" placeholder="New client id" style="flex:1">
    <button onclick="addClient()">Add client</button>
    <button class="secondary" onclick="loadClients()">Refresh</button>
  </div>
  <div id="clients"></div>
</main>

<script>
const COMMON_ENTITIES = __COMMON_ENTITIES__;

async function api(path, options={}) {
  const res = await fetch(path, {
    headers: {'Content-Type': 'application/json'},
    ...options,
  });
  if (res.status === 303) { window.location = '/login'; return null; }
  if (!res.ok) { alert('Request failed: ' + (await res.text())); return null; }
  return res.status === 204 ? null : res.json();
}

function logout() {
  fetch('/logout', {method: 'POST'}).then(() => window.location = '/login');
}

async function loadClients() {
  const data = await api('/api/clients');
  if (!data) return;
  const container = document.getElementById('clients');
  const ids = Object.keys(data);
  if (ids.length === 0) {
    container.innerHTML = '<div class="empty-state">No clients have connected yet. Defaults will apply the moment one does, or add one manually above.</div>';
    return;
  }
  container.innerHTML = ids.map(id => renderClient(id, data[id])).join('');
}

function renderClient(id, cfg) {
  const entityTags = cfg.entities.map(e => `
    <span class="tag">${e}
      <button class="danger" onclick="removeEntity('${id}','${e}')">&times;</button>
    </span>`).join('');

  const options = COMMON_ENTITIES.map(e => `<option value="${e}">${e}</option>`).join('');

  const tokenRows = cfg.entities.map(e => `
    <div class="token-row">
      <span class="muted">${e}</span>
      <input type="text" value="${(cfg.tokens[e] || '').replace(/"/g,'&quot;')}" id="token-${id}-${e}">
      <button class="secondary" onclick="saveToken('${id}','${e}')">Save</button>
    </div>`).join('');

  return `
    <div class="card">
      <div class="client-name">${id}</div>
      <div class="section-label">Active entities</div>
      <div class="row">${entityTags || '<span class="muted">None active</span>'}</div>
      <div class="row">
        <select id="entity-select-${id}">${options}<option value="__custom__">Custom...</option></select>
        <input type="text" id="entity-custom-${id}" placeholder="Custom entity code" style="display:none;width:160px">
        <button onclick="addEntity('${id}')">Add entity</button>
      </div>
      <div class="section-label">Replacement tokens</div>
      ${tokenRows || '<span class="muted">Add an entity to configure its token</span>'}
      <div class="row" style="margin-top:12px">
        <button class="secondary" onclick="resetClient('${id}')">Reset to defaults</button>
      </div>
    </div>
  `;
}

function toggleCustomEntity(id) {
  const sel = document.getElementById('entity-select-' + id);
  const custom = document.getElementById('entity-custom-' + id);
  custom.style.display = sel.value === '__custom__' ? 'inline-block' : 'none';
}

async function addClient() {
  const input = document.getElementById('new-client-id');
  const id = input.value.trim();
  if (!id) return;
  // Fetching the client seeds it with defaults via get_current on the backend.
  await api(`/api/clients/${encodeURIComponent(id)}`);
  input.value = '';
  loadClients();
}

async function addEntity(id) {
  const sel = document.getElementById('entity-select-' + id);
  let entity = sel.value;
  if (entity === '__custom__') {
    entity = document.getElementById('entity-custom-' + id).value.trim();
  }
  if (!entity) return;
  await api(`/api/clients/${encodeURIComponent(id)}/entities`, {
    method: 'POST', body: JSON.stringify({entity})
  });
  loadClients();
}

async function removeEntity(id, entity) {
  await api(`/api/clients/${encodeURIComponent(id)}/entities/${encodeURIComponent(entity)}`, {method: 'DELETE'});
  loadClients();
}

async function saveToken(id, entity) {
  const val = document.getElementById(`token-${id}-${entity}`).value;
  await api(`/api/clients/${encodeURIComponent(id)}/tokens`, {
    method: 'POST', body: JSON.stringify({entity, token: val})
  });
}

async function resetClient(id) {
  if (!confirm(`Reset "${id}" to default filter settings?`)) return;
  await api(`/api/clients/${encodeURIComponent(id)}`, {method: 'DELETE'});
  loadClients();
}

document.addEventListener('change', (e) => {
  if (e.target.id && e.target.id.startsWith('entity-select-')) {
    toggleCustomEntity(e.target.id.replace('entity-select-', ''));
  }
});

loadClients();
</script>
</body></html>"""
