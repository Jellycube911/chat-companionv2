import math
import re
import sqlite3
import time
from pathlib import Path


TOKEN_RE = re.compile(r"[a-z0-9_:\-]{2,}", re.IGNORECASE)
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "companion_memory.sqlite3"


class MemoryStore:
    def __init__(self, path=DB_PATH):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self._init_db()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self):
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    memory_key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 5,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_used REAL NOT NULL,
                    UNIQUE(kind, memory_key)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at DESC)"
            )

    def remember(self, kind, key, content, importance=5):
        kind = str(kind).strip().lower()[:40] or "fact"
        key = str(key).strip().lower()[:120]
        content = str(content).strip()[:4000]
        importance = max(1, min(10, int(importance)))
        if not key or not content:
            raise ValueError("key and content are required")

        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO memories(
                    kind, memory_key, content, importance,
                    created_at, updated_at, last_used
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, memory_key) DO UPDATE SET
                    content=excluded.content,
                    importance=excluded.importance,
                    updated_at=excluded.updated_at,
                    last_used=excluded.last_used
                """,
                (kind, key, content, importance, now, now, now),
            )

        return {
            "ok": True,
            "kind": kind,
            "key": key,
            "importance": importance,
        }

    def recall(self, query, limit=6):
        query = str(query).strip()
        limit = max(1, min(12, int(limit)))
        query_tokens = set(TOKEN_RE.findall(query.lower()))

        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, kind, memory_key, content, importance,
                       created_at, updated_at, last_used
                FROM memories
                ORDER BY updated_at DESC
                LIMIT 1000
                """
            ).fetchall()

        now = time.time()
        ranked = []
        query_lower = query.lower()

        for row in rows:
            haystack = (
                row["kind"] + " " + row["memory_key"] + " " + row["content"]
            ).lower()
            tokens = set(TOKEN_RE.findall(haystack))
            overlap = len(query_tokens & tokens)

            score = row["importance"] * 0.7
            if query_lower and query_lower in haystack:
                score += 8
            score += overlap * 3.5

            age_days = max(0.0, (now - row["updated_at"]) / 86400.0)
            score += 2.0 / (1.0 + math.log1p(age_days))

            if query_tokens and overlap == 0 and query_lower not in haystack:
                score -= 2

            ranked.append((score, row))

        ranked.sort(key=lambda item: item[0], reverse=True)
        selected = ranked[:limit]

        if selected:
            ids = [row["id"] for _, row in selected]
            placeholders = ",".join("?" for _ in ids)
            with self._connect() as db:
                db.execute(
                    f"UPDATE memories SET last_used=? WHERE id IN ({placeholders})",
                    [now, *ids],
                )

        return [
            {
                "kind": row["kind"],
                "key": row["memory_key"],
                "content": row["content"],
                "importance": row["importance"],
            }
            for _, row in selected
        ]

    def record_episode(self, user_text, assistant_text):
        user_text = str(user_text).strip()[:4000]
        assistant_text = str(assistant_text).strip()[:4000]
        if not user_text and not assistant_text:
            return

        with self._connect() as db:
            db.execute(
                """
                INSERT INTO episodes(user_text, assistant_text, created_at)
                VALUES (?, ?, ?)
                """,
                (user_text, assistant_text, time.time()),
            )
            db.execute(
                """
                DELETE FROM episodes
                WHERE id NOT IN (
                    SELECT id FROM episodes ORDER BY created_at DESC LIMIT 500
                )
                """
            )

    def recent_episodes(self, limit=2):
        limit = max(0, min(6, int(limit)))
        if limit == 0:
            return []

        with self._connect() as db:
            rows = db.execute(
                """
                SELECT user_text, assistant_text, created_at
                FROM episodes
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "user": row["user_text"],
                "assistant": row["assistant_text"],
            }
            for row in reversed(rows)
        ]

    def stats(self):
        with self._connect() as db:
            memory_count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            episode_count = db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        return {
            "memories": memory_count,
            "episodes": episode_count,
            "database": str(self.path),
        }


store = MemoryStore()
