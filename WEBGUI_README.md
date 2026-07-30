# Web GUI for the MCP Filter server

A small, separate FastAPI service for managing per-client PII filter
settings (the same things the `mcp-filter>` CLI can do: add/remove
entities, set replacement tokens, view/reset clients) — but usable by
people who don't want to touch a terminal.

## Why a separate process

You asked for isolation over sharing memory with the MCP server, so instead
of a plain in-memory dict, `filter_state.py` now persists client configs to
a small SQLite file (`filter_config.db` by default) with WAL mode enabled.
Both `mcp_server.py`/`cli.py` and `webgui.py` import the same
`runtime_config` object from `filter_state.py`, but because it's backed by
SQLite, changes made by one process (e.g. someone using the web GUI) are
immediately visible to the other (e.g. the MCP server enforcing the filter
on the next `read_file` call) — no shared memory or custom IPC needed.

If you run the two processes from different working directories, set the
`FILTER_DB_PATH` environment variable to the same absolute path for both,
or they'll each create their own separate database.

## Install

```bash
pip install fastapi "uvicorn[standard]" itsdangerous
```

(`itsdangerous` is required by Starlette's `SessionMiddleware`, used for
the optional login.)

## Run

In one terminal (your existing server):

```bash
python cli.py
```

In another terminal:

```bash
uvicorn webgui:app --host 0.0.0.0 --port 8421
```

Then open `http://<host>:8421` in a browser.

## Auth (per-user accounts, required)

The dashboard now has real accounts instead of one shared password — each
person logs in as themselves and only sees/edits the clients they created
or claimed. Accounts aren't self-service (no public signup page); provision
them from the server CLI:

```bash
python cli.py
mcp-filter> adduser alice
Password for 'alice': ********
Confirm password: ********
[+] Webgui account created: alice
```

Passwords are stored as salted PBKDF2-SHA256 hashes in the same SQLite DB
as the filter settings — no plaintext, no extra service.

```bash
export WEBGUI_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_hex(32))')"
uvicorn webgui:app --port 8421
```

Set `WEBGUI_SECRET_KEY` in production (signs the session cookie) — without
it a random key is generated at startup, so everyone gets logged out on
restart. There's no rate-limiting on login attempts yet, so if this is
reachable beyond localhost/your internal network, put it behind a reverse
proxy that can add that.

## What the GUI does

- Each logged-in user sees only the clients *they* created or claimed —
  not a global list of every client that's ever connected
- Add a client from the dashboard without needing a real MCP connection
  first — typing an id and clicking "Add client" creates it (or, if a real
  MCP connection already created that same id and nobody's claimed it yet,
  attaches it to you)
- Every client card shows the exact `x-client-id` header to paste into
  that MCP client's config, so the id you created matches what the real
  connection sends
- Add/remove which PII entities are actively filtered per client
- Edit the replacement token text per entity (e.g. what `PERSON` gets
  swapped with)
- Reset a client back to the default filter set
  (`PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`)

It's a thin UI over the same `runtime_config` API the CLI already uses —
no new business logic beyond ownership, just a friendlier way to reach it.

## Notes / things worth deciding next

- **Client IDs still have to match**: creating `alice-laptop` in the
  dashboard doesn't do anything to the real MCP connection on its own —
  Alice still has to point her MCP client at that same id, either via the
  `x-client-id` header or, for clients that only let you enter a URL and
  have no way to set custom headers (e.g. Odysseus), by appending
  `?client_id=alice-laptop` to the server URL instead. Both are shown on
  each client's card. If her client instead relies on `client_info.name`
  with neither option available, it may land under a different derived id
  and won't pick up the settings she configured.
- **One shared NextCloud folder, for now**: every client_id still reads
  from the same `HOSTNAME`/`SHARED_TOKEN` NextCloud share in `.env` — the
  per-user split here is about filter settings, not about who can see
  which files. If you later want different users to see different NextCloud
  folders, that's a separate change (keying `NextCloudClient` by user/token
  instead of one global instance) and doesn't require a separate MCP
  server or port, just per-user credentials.
- **No self-service signup**: accounts are created via `adduser` in the
  CLI. Fine for a small trusted group; if the user base grows, an
  open-signup route (or SSO) would be a small addition on top of the
  existing `users` table.
- **Reverse proxy / TLS**: `uvicorn` here serves plain HTTP. If this needs
  to be reachable outside localhost, put it behind nginx/Caddy with TLS
  rather than exposing port 8421 directly.
- **Entity validation**: the GUI doesn't currently check that a custom
  entity code is one Presidio's Swedish pipeline actually recognizes
  (it'll just silently never match). Happy to add a "known entities" check
  against the analyzer if useful.