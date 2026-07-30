import json
import os
import sqlite3
import threading

# The DB file is the shared state between the MCP server process and the
# web GUI process. Both processes import `runtime_config` from this module,
# but they run separately, so file-backed storage (instead of a plain dict)
# is what lets changes made in one process show up in the other.
DB_PATH = os.getenv(
    "FILTER_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "filter_config.db"),
)


class RuntimeConfig:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        # Guards against races within a single process. SQLite's own
        # locking (with WAL mode + busy_timeout) handles cross-process safety.
        self._lock = threading.Lock()

        self.default_entities = ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER"]
        self.default_tokens = {
            "PERSON": "[NAMN_REDAKTERAT]",
            "EMAIL_ADDRESS": "[EPOST_REDAKTERAT]",
            "PHONE_NUMBER": "[TELEFON_REDAKTERAT]",
        }

        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        return conn

    def _init_db(self):
        with self._lock, self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS client_configs (
                    client_id TEXT PRIMARY KEY,
                    entities  TEXT NOT NULL,
                    tokens    TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _ensure_client(self, conn: sqlite3.Connection, client_id: str):
        """If a client has no row yet, seed it with the default filter rules."""
        cur = conn.execute(
            "SELECT 1 FROM client_configs WHERE client_id = ?", (client_id,)
        )
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO client_configs (client_id, entities, tokens) VALUES (?, ?, ?)",
                (
                    client_id,
                    json.dumps(self.default_entities),
                    json.dumps(self.default_tokens),
                ),
            )

    def update(self, client_id: str, entities: list, tokens: dict):
        with self._lock, self._get_conn() as conn:
            self._ensure_client(conn, client_id)
            conn.execute(
                "UPDATE client_configs SET entities = ?, tokens = ? WHERE client_id = ?",
                (json.dumps(entities), json.dumps(tokens), client_id),
            )
            conn.commit()

    def get_current(self, client_id: str):
        with self._lock, self._get_conn() as conn:
            self._ensure_client(conn, client_id)
            conn.commit()
            cur = conn.execute(
                "SELECT entities, tokens FROM client_configs WHERE client_id = ?",
                (client_id,),
            )
            row = cur.fetchone()
            entities = json.loads(row[0])
            tokens = json.loads(row[1])
            return entities, tokens

    def get_current_clients(self) -> list:
        """Helper for the CLI/web GUI to show active/known clients."""
        with self._lock, self._get_conn() as conn:
            cur = conn.execute("SELECT client_id FROM client_configs")
            return [r[0] for r in cur.fetchall()]

    def delete_client(self, client_id: str):
        """Reset a client back to defaults by dropping its row entirely."""
        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM client_configs WHERE client_id = ?", (client_id,))
            conn.commit()


# Single source of truth instance. Because it's SQLite-backed, this is safe
# to import from both cli.py/mcp_server.py AND webgui.py running as a
# separate process, as long as FILTER_DB_PATH points at the same file
# (default: filter_config.db next to this module).
runtime_config = RuntimeConfig()
