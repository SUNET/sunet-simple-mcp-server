import hashlib
import json
import os
import secrets
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
                    tokens    TEXT NOT NULL,
                    owner     TEXT
                )
                """
            )
            # Migrate DBs created before the owner column existed.
            cols = [row[1] for row in conn.execute("PRAGMA table_info(client_configs)")]
            if "owner" not in cols:
                conn.execute("ALTER TABLE client_configs ADD COLUMN owner TEXT")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username      TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    salt          TEXT NOT NULL,
                    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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

    # ----------------------------------------------------------------
    # Ownership (webgui multi-user support)
    # ----------------------------------------------------------------
    def get_owner(self, client_id: str):
        """Returns the owning username, or None if unclaimed / nonexistent."""
        with self._lock, self._get_conn() as conn:
            cur = conn.execute(
                "SELECT owner FROM client_configs WHERE client_id = ?", (client_id,)
            )
            row = cur.fetchone()
            return row[0] if row else None

    def get_clients_for_owner(self, owner: str) -> list:
        """Clients a given webgui user has created or claimed."""
        with self._lock, self._get_conn() as conn:
            cur = conn.execute(
                "SELECT client_id FROM client_configs WHERE owner = ?", (owner,)
            )
            return [r[0] for r in cur.fetchall()]

    def claim_client(self, client_id: str, owner: str) -> bool:
        """Assign client_id to owner. Creates it with defaults if it doesn't
        exist yet (this is how the webgui lets a user pre-create a client
        before any real MCP connection has used that id). Also lets a user
        claim a client that already exists but has no owner yet - e.g. one
        that showed up from a real MCP connection before this user ever
        touched the dashboard, as long as the id matches exactly.

        Returns False (and makes no change) if the client is already owned
        by someone else.
        """
        with self._lock, self._get_conn() as conn:
            self._ensure_client(conn, client_id)
            cur = conn.execute(
                "SELECT owner FROM client_configs WHERE client_id = ?", (client_id,)
            )
            current_owner = cur.fetchone()[0]
            if current_owner is not None and current_owner != owner:
                return False
            conn.execute(
                "UPDATE client_configs SET owner = ? WHERE client_id = ?",
                (owner, client_id),
            )
            conn.commit()
            return True

    # ----------------------------------------------------------------
    # User accounts (webgui login)
    # ----------------------------------------------------------------
    @staticmethod
    def _hash_password(password: str, salt: bytes = None) -> tuple:
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return digest.hex(), salt.hex()

    def create_user(self, username: str, password: str) -> bool:
        """Returns False if the username is already taken."""
        username = username.strip().lower()
        if not username or not password:
            raise ValueError("username and password are both required")
        digest_hex, salt_hex = self._hash_password(password)
        with self._lock, self._get_conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)",
                    (username, digest_hex, salt_hex),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def verify_user(self, username: str, password: str) -> bool:
        username = username.strip().lower()
        with self._lock, self._get_conn() as conn:
            cur = conn.execute(
                "SELECT password_hash, salt FROM users WHERE username = ?", (username,)
            )
            row = cur.fetchone()
            if row is None:
                # Hash anyway so a nonexistent-username response takes
                # roughly as long as a wrong-password one.
                self._hash_password(password, salt=b"0" * 16)
                return False
            stored_hash, salt_hex = row
            digest_hex, _ = self._hash_password(password, salt=bytes.fromhex(salt_hex))
            return secrets.compare_digest(digest_hex, stored_hash)

    def list_users(self) -> list:
        with self._lock, self._get_conn() as conn:
            cur = conn.execute("SELECT username FROM users ORDER BY username")
            return [r[0] for r in cur.fetchall()]


# Single source of truth instance. Because it's SQLite-backed, this is safe
# to import from both cli.py/mcp_server.py AND webgui.py running as a
# separate process, as long as FILTER_DB_PATH points at the same file
# (default: filter_config.db next to this module).
runtime_config = RuntimeConfig()