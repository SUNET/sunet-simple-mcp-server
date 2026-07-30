"""
Web GUI for the SUNET MCP Filter server.

Runs as its OWN process, separate from mcp_server.py / cli.py. It shares
state with the MCP server only via the SQLite file that filter_state.py
points at (FILTER_DB_PATH env var, same value must be used by both
processes if you don't run them from the same directory).

Run with:
    uvicorn webgui:app --host 0.0.0.0 --port 8421

Accounts:
    Every visitor now logs in as themselves rather than sharing one
    password. Accounts aren't self-service on purpose (this keeps the
    dashboard's "list of clients" meaningful per person) - provision them
    from the CLI:

        python cli.py
        mcp-filter> adduser alice

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
def require_login(request: Request) -> str:
    """Returns the logged-in username, or redirects to /login."""
    username = request.session.get("user")
    if not username:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return username


@app.exception_handler(HTTPException)
async def redirect_to_login(request: Request, exc: HTTPException):
    if exc.status_code == status.HTTP_303_SEE_OTHER and exc.headers and exc.headers.get("Location") == "/login":
        return RedirectResponse(url="/login", status_code=303)
    raise exc


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if request.session.get("user"):
        return RedirectResponse(url="/", status_code=303)
    error = request.query_params.get("error")
    error_html = '<p class="error">Wrong username or password.</p>' if error else ""
    return HTMLResponse(PAGE_LOGIN.replace("__ERROR__", error_html))


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip().lower()
    password = str(form.get("password", ""))
    if username and runtime_config.verify_user(username, password):
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=303)
    return RedirectResponse(url="/login?error=1", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, username: str = Depends(require_login)):
    page = PAGE_DASHBOARD.replace("__COMMON_ENTITIES__", str(COMMON_ENTITIES))
    page = page.replace("__USERNAME__", username)
    return HTMLResponse(page)


# --------------------------------------------------------------------------
# JSON API consumed by the dashboard's JS
# All client-scoped routes check ownership: a client_id only shows up for
# / can be edited by the user who created or claimed it.
# --------------------------------------------------------------------------
class EntityBody(BaseModel):
    entity: str


class TokenBody(BaseModel):
    entity: str
    token: str


def _owned_or_403(client_id: str, username: str):
    owner = runtime_config.get_owner(client_id)
    if owner != username:
        raise HTTPException(403, "You don't have access to this client.")


@app.get("/api/clients")
async def api_list_clients(username: str = Depends(require_login)):
    result = {}
    for cid in runtime_config.get_clients_for_owner(username):
        entities, tokens = runtime_config.get_current(cid)
        result[cid] = {"entities": entities, "tokens": tokens}
    return JSONResponse(result)


@app.post("/api/clients/{client_id}")
async def api_claim_client(client_id: str, username: str = Depends(require_login)):
    """Create a brand new client_id owned by the caller, or attach the
    caller to one that already exists but is unclaimed (e.g. it showed up
    from a real MCP connection before anyone had added it in the GUI)."""
    client_id = client_id.strip()
    if not client_id:
        raise HTTPException(400, "Client id can't be empty.")
    if not runtime_config.claim_client(client_id, username):
        raise HTTPException(409, "That client id is already claimed by another user.")
    entities, tokens = runtime_config.get_current(client_id)
    return {"entities": entities, "tokens": tokens}


@app.get("/api/clients/{client_id}")
async def api_get_client(client_id: str, username: str = Depends(require_login)):
    _owned_or_403(client_id, username)
    entities, tokens = runtime_config.get_current(client_id)
    return {"entities": entities, "tokens": tokens}


@app.post("/api/clients/{client_id}/entities")
async def api_add_entity(client_id: str, body: EntityBody, username: str = Depends(require_login)):
    _owned_or_403(client_id, username)
    entity = body.entity.strip().upper()
    if not entity:
        raise HTTPException(400, "Entity name can't be empty.")
    entities, tokens = runtime_config.get_current(client_id)
    if entity not in entities:
        entities.append(entity)
        runtime_config.update(client_id, entities, tokens)
    return {"entities": entities, "tokens": tokens}


@app.delete("/api/clients/{client_id}/entities/{entity}")
async def api_remove_entity(client_id: str, entity: str, username: str = Depends(require_login)):
    _owned_or_403(client_id, username)
    entity = entity.strip().upper()
    entities, tokens = runtime_config.get_current(client_id)
    if entity in entities:
        entities.remove(entity)
        runtime_config.update(client_id, entities, tokens)
    return {"entities": entities, "tokens": tokens}


@app.post("/api/clients/{client_id}/tokens")
async def api_set_token(client_id: str, body: TokenBody, username: str = Depends(require_login)):
    _owned_or_403(client_id, username)
    entity = body.entity.strip().upper()
    entities, tokens = runtime_config.get_current(client_id)
    tokens[entity] = body.token
    runtime_config.update(client_id, entities, tokens)
    return {"entities": entities, "tokens": tokens}


@app.delete("/api/clients/{client_id}")
async def api_reset_client(client_id: str, username: str = Depends(require_login)):
    _owned_or_403(client_id, username)
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
  .who { display: flex; align-items: center; gap: 12px; }
  .who span { color: var(--muted); font-size: 13px; }
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
  .config-snippet {
    background: #10131e; border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 10px; font-family: ui-monospace, Menlo, monospace; font-size: 12px;
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    margin-top: 10px;
  }
  .config-snippet code { color: var(--ok); }
  .config-snippet button { padding: 3px 9px; font-size: 11px; }
  .login-box {
    max-width: 340px; margin: 12vh auto; background: var(--panel);
    border: 1px solid var(--border); border-radius: 10px; padding: 28px;
  }
  .login-box h1 { font-size: 16px; margin-bottom: 4px; }
  .login-box p.hint { color: var(--muted); font-size: 12.5px; margin: 0 0 16px; }
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
    <p class="hint">Don't have an account? Ask an admin to run <code>adduser</code> in the server CLI.</p>
    <form method="post" action="/login">
      __ERROR__
      <input type="text" name="username" placeholder="Username" autofocus required autocapitalize="off">
      <input type="password" name="password" placeholder="Password" required>
      <button type="submit">Log in</button>
    </form>
  </div>
</body></html>"""

PAGE_DASHBOARD = """<!doctype html><html><head><meta charset="utf-8">
<title>MCP Filter - Dashboard</title>""" + BASE_STYLE + """</head>
<body>
<header>
  <h1>SUNET MCP Filter <span>&mdash; PII filter settings</span></h1>
  <div class="who">
    <span>__USERNAME__</span>
    <button class="secondary" onclick="logout()">Log out</button>
  </div>
</header>
<main>
  <div class="toolbar">
    <input id="new-client-id" placeholder="New client id, e.g. my-laptop" style="flex:1">
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
    container.innerHTML = '<div class="empty-state">You haven\\'t added any clients yet. Add one above, then point your MCP client at the id it gives you.</div>';
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
      <div class="config-snippet">
        <code>x-client-id: ${id}</code>
        <button class="secondary" onclick="copySnippet('${id}')">Copy header</button>
      </div>
      <div class="muted" style="margin-top:6px">Send this header from your MCP client so its requests land under this id.</div>
      <div class="row" style="margin-top:12px">
        <button class="secondary" onclick="resetClient('${id}')">Reset to defaults</button>
      </div>
    </div>
  `;
}

function copySnippet(id) {
  navigator.clipboard.writeText(`x-client-id: ${id}`);
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
  const result = await api(`/api/clients/${encodeURIComponent(id)}`, {method: 'POST'});
  if (result) input.value = '';
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