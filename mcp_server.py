import hashlib
import re
import secrets

from mcp.server.fastmcp import FastMCP, Context
from dotenv import load_dotenv
import os
from nextcloud_client import NextCloudClient
from presidio_filter import PII_filter

# Load environment variables
load_dotenv()

mcp = FastMCP("Simple NextCloud MCP Server", stateless_http=True,
              json_response=True, port=8420, log_level="CRITICAL")

# Global clients to be populated during runtime setup
nextcloud_client: NextCloudClient = None

def init_server():
    """Initializes the backend configuration pipeline before server execution."""
    global nextcloud_client
    hostname = os.getenv("HOSTNAME")
    share_token = os.getenv("SHARED_TOKEN")
    
    pii_filter = PII_filter()
    nextcloud_client = NextCloudClient(hostname, share_token, pii_filter)


def _slugify(name: str) -> str:
    """Turn an arbitrary client-supplied name into a short, readable, safe id."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip()).strip("-").lower()
    return slug[:40] or "client"


def _short_hash(*parts: str, length: int = 6) -> str:
    """Deterministic short fingerprint, used to disambiguate or key anonymous ids."""
    joined = "|".join(p for p in parts if p)
    return hashlib.sha256(joined.encode("utf-8", errors="ignore")).hexdigest()[:length]


def _extract_headers(request_context) -> dict:
    """Best-effort header lookup across the couple of shapes FastMCP/transports expose."""
    headers = getattr(request_context, "headers", None)
    if headers:
        return dict(headers)
    raw_request = getattr(request_context, "request", None)
    if raw_request is not None and hasattr(raw_request, "headers"):
        try:
            return dict(raw_request.headers)
        except Exception:
            pass
    return {}


def _extract_client_host(request_context):
    """Best-effort remote address lookup, used only as a last-resort fingerprint."""
    raw_request = getattr(request_context, "request", None)
    client = getattr(raw_request, "client", None) if raw_request is not None else None
    return getattr(client, "host", None)


def _extract_query_params(request_context) -> dict:
    """Best-effort query-string lookup on the underlying request.

    Exists for MCP clients whose UI only accepts a server URL and gives no
    way to set custom headers (e.g. Odysseus). Those clients can still pass
    an id by putting it directly in the URL: .../mcp?client_id=alice-laptop
    """
    raw_request = getattr(request_context, "request", None)
    query_params = getattr(raw_request, "query_params", None) if raw_request is not None else None
    if query_params is None:
        return {}
    try:
        return dict(query_params)
    except Exception:
        return {}


def _get_client_id(ctx: Context) -> str:
    """Resolve a readable, per-client id from the FastMCP Context.

    Priority, highest first:
      1. An explicit `x-client-id` header - used verbatim (sanitized). This is
         the reliable option: point any client at this if you want its
         filter settings to be stable and easy to find in the web GUI/CLI.
      2. A `client_id` query parameter on the server URL itself, e.g.
         `.../mcp?client_id=alice-laptop` - same intent as the header above,
         for clients whose UI only lets you enter a URL and offers no way
         to set custom headers.
      3. The MCP `client_info.name` sent during initialization - turned into
         a readable slug (e.g. "claude-desktop"). A short hash suffix is
         appended only when needed to keep two different real connections
         that happen to share a name from colliding onto one config.
      4. No name, but the transport gives us *something* stable to key off
         (a session id header, or failing that the remote address) -
         "anon-<hash>", stable for that connection/session.
      5. Nothing identifiable at all - a fresh random "anon-<random>" id.

    Caveat: this server runs with stateless_http=True, so a fully anonymous
    client with no session header and no client_info name will get a NEW
    random id on every tool call (case 4), meaning per-client settings for
    it won't persist between calls. That's an inherent limit of having no
    session/identity signal to correlate calls by, not something this
    function can work around - if you need persistence for such clients,
    have them send `x-client-id`.
    """
    if not (ctx and hasattr(ctx, "request_context") and ctx.request_context):
        return f"anon-{secrets.token_hex(3)}"

    request_context = ctx.request_context
    headers = _extract_headers(request_context)
    query_params = _extract_query_params(request_context)

    # 1. Explicit header always wins.
    explicit_id = headers.get("x-client-id")
    if explicit_id:
        return _slugify(explicit_id)

    # 2. Explicit query param - same intent as the header, for clients that
    # can only be pointed at a URL (see _extract_query_params docstring).
    explicit_id_qp = query_params.get("client_id") or query_params.get("x-client-id")
    if explicit_id_qp:
        return _slugify(explicit_id_qp)

    # Whatever connection fingerprint is available, for disambiguation/stability.
    session_id = headers.get("mcp-session-id") or getattr(request_context, "session_id", None)
    client_host = _extract_client_host(request_context)

    # 3. Named client -> readable slug, disambiguated only if needed.
    meta = getattr(request_context, "meta", None)
    client_info = getattr(meta, "client_info", None) if meta else None
    client_name = getattr(client_info, "name", None) if client_info else None
    if client_name:
        base = _slugify(client_name)
        fingerprint = session_id or client_host
        return f"{base}-{_short_hash(str(fingerprint))}" if fingerprint else base

    # 4. No name, but something stable to key an anonymous id off of.
    if session_id:
        return f"anon-{_short_hash(str(session_id))}"
    if client_host:
        return f"anon-{_short_hash(client_host)}"

    # 5. Truly nothing to go on.
    return f"anon-{secrets.token_hex(3)}"

@mcp.tool()
async def list_all_files() -> dict:
    """List available files in the NextCloud shared folder.
    WARNING: Do not pass any arguments or parameters to this tool. 
    Leave the arguments object completely empty."""
    files = await nextcloud_client.list_files()
    return {"files": files}

@mcp.tool()
async def list_all_files_detailed() -> dict:
    """A more detaild list of the files in the folder. Includes things like 
        size, modification date, etag, type, and absolute path.
        WARNING: Do not pass any arguments or parameters to this tool. 
        Leave the arguments object completely empty."""
    files = await nextcloud_client.list_files_metadata()
    return {"files": files}

@mcp.tool()
async def read_file(filename: str, ctx: Context = None) -> str:
    """Reads the contents of a single file.
    
    Args:
        filename: Full file name (e.g. 2025-08050_3.1 Beslut.pdf)
    """
    client_id = _get_client_id(ctx)
    return await nextcloud_client.read_file(filename, client_id=client_id)
    

@mcp.tool()
async def folder_context(ctx: Context = None) -> dict[str, str]:
    """List all available files in the NextCloud shared folder. then reads 
     all of the files and sends it back in a dict with strings in order to 
     build context for the folder and all included items.
     """
    client_id = _get_client_id(ctx)
    return await nextcloud_client.build_context(client_id=client_id)

def main():
    # Initialize and run the server, this is a fallback for no cli
    init_server()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()