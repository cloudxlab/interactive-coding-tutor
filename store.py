"""SQLite storage for multi-user mode: users and each user's tutor state."""
import json
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,          -- the provider's stable user id (never the email)
    email TEXT,
    name TEXT,
    avatar TEXT,
    jupyter_token TEXT NOT NULL,    -- secret used by the app to talk to this user's Jupyter
    created_at INTEGER NOT NULL,
    last_login INTEGER NOT NULL,
    UNIQUE (provider, subject)
);
CREATE TABLE IF NOT EXISTS user_llm (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    key_enc TEXT NOT NULL,          -- the user's OpenRouter key, encrypted at rest (see byo.py)
    model TEXT,
    connected_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with closing(self._conn()) as c:
            c.executescript(_SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    # --- users -----------------------------------------------------------------
    def upsert_user(self, provider, subject, email=None, name=None, avatar=None):
        """Finds the user for (provider, subject), creating one on first login. Returns a dict."""
        now = int(time.time())
        with self._write_lock, closing(self._conn()) as c, c:
            row = c.execute("SELECT * FROM users WHERE provider=? AND subject=?", (provider, subject)).fetchone()
            if row:
                c.execute("UPDATE users SET email=?, name=?, avatar=?, last_login=? WHERE id=?",
                          (email, name, avatar, now, row["id"]))
                uid = row["id"]
            else:
                cur = c.execute(
                    "INSERT INTO users (provider, subject, email, name, avatar, jupyter_token, created_at, last_login) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (provider, subject, email, name, avatar, secrets.token_urlsafe(32), now, now))
                uid = cur.lastrowid
        return self.get_user(uid)

    def get_user(self, uid):
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return None
        with closing(self._conn()) as c:
            row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

    def count_users(self):
        with closing(self._conn()) as c:
            return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    # --- tutor state -------------------------------------------------------------
    def load_state(self, uid):
        with closing(self._conn()) as c:
            row = c.execute("SELECT json FROM state WHERE user_id=?", (int(uid),)).fetchone()
        return json.loads(row["json"]) if row else None

    def save_state(self, uid, state):
        with self._write_lock, closing(self._conn()) as c, c:
            c.execute("INSERT INTO state (user_id, json, updated_at) VALUES (?,?,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET json=excluded.json, updated_at=excluded.updated_at",
                      (int(uid), json.dumps(state), int(time.time())))

    def delete_state(self, uid):
        with self._write_lock, closing(self._conn()) as c, c:
            c.execute("DELETE FROM state WHERE user_id=?", (int(uid),))

    # --- bring-your-own LLM (OpenRouter) -------------------------------------------------------
    def get_llm(self, uid):
        with closing(self._conn()) as c:
            row = c.execute("SELECT key_enc, model, connected_at FROM user_llm WHERE user_id=?", (int(uid),)).fetchone()
        return dict(row) if row else None

    def set_llm_key(self, uid, key_enc):
        """Connect (or reconnect): stores the new key and keeps the user's model choice, if any."""
        with self._write_lock, closing(self._conn()) as c, c:
            c.execute("INSERT INTO user_llm (user_id, key_enc, model, connected_at) VALUES (?,?,NULL,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET key_enc=excluded.key_enc, connected_at=excluded.connected_at",
                      (int(uid), key_enc, int(time.time())))

    def set_llm_model(self, uid, model):
        with self._write_lock, closing(self._conn()) as c, c:
            c.execute("UPDATE user_llm SET model=? WHERE user_id=?", (model, int(uid)))

    def delete_llm(self, uid):
        with self._write_lock, closing(self._conn()) as c, c:
            c.execute("DELETE FROM user_llm WHERE user_id=?", (int(uid),))
