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

## Auth (optional, off by default)

You said you're not sure yet whether you need login, so it's opt-in via
an environment variable — no code changes needed either way:

```bash
# no auth (fine for a trusted internal network / localhost only)
uvicorn webgui:app --port 8421

# with a shared password
export WEBGUI_PASSWORD="something-not-guessable"
export WEBGUI_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_hex(32))')"
uvicorn webgui:app --port 8421
```

If `WEBGUI_PASSWORD` is unset, the dashboard is open to anyone who can
reach the port. If you expose this beyond localhost/your internal network,
set a password — there's currently no per-user accounts, just a single
shared one, and no rate-limiting on login attempts, so treat it as a
"keep casual snoopers out" measure rather than strong access control.

## What the GUI does

- Lists every client that has connected (or been added manually)
- Add/remove which PII entities are actively filtered per client
- Edit the replacement token text per entity (e.g. what `PERSON` gets
  swapped with)
- Reset a client back to the default filter set
  (`PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`)

It's a thin UI over the same `runtime_config` API the CLI already uses —
no new business logic, just a friendlier way to reach it.

## Notes / things worth deciding next

- **Client IDs**: the MCP server currently derives `client_id` from MCP
  `client_info.name` or an `x-client-id` header, falling back to
  `"default_client"`. If most of your real clients end up as
  `default_client`, the per-client filtering won't be very useful in
  practice — worth checking what your actual MCP clients send.
- **Reverse proxy / TLS**: `uvicorn` here serves plain HTTP. If this needs
  to be reachable outside localhost, put it behind nginx/Caddy with TLS
  rather than exposing port 8421 directly.
- **Entity validation**: the GUI doesn't currently check that a custom
  entity code is one Presidio's Swedish pipeline actually recognizes
  (it'll just silently never match). Happy to add a "known entities" check
  against the analyzer if useful.
