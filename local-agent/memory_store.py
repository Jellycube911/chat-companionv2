import json
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
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _init_db(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
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
                """
                CREATE TABLE IF NOT EXISTS goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    priority INTEGER NOT NULL DEFAULT 5,
                    source TEXT NOT NULL DEFAULT 'self',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    progress TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    problem TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    lesson TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS learned_skills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    intent TEXT NOT NULL UNIQUE,
                    procedure TEXT NOT NULL,
                    successes INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    source TEXT NOT NULL DEFAULT 'self',
                    last_outcome TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_used REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS skill_trials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_id INTEGER,
                    intent TEXT NOT NULL,
                    hypothesis TEXT NOT NULL,
                    actions TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(skill_id) REFERENCES learned_skills(id)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS skill_edges (
                    parent_skill_id INTEGER NOT NULL,
                    child_skill_id INTEGER NOT NULL,
                    relation TEXT NOT NULL DEFAULT 'subskill',
                    weight REAL NOT NULL DEFAULT 1.0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(parent_skill_id, child_skill_id, relation),
                    FOREIGN KEY(parent_skill_id) REFERENCES learned_skills(id),
                    FOREIGN KEY(child_skill_id) REFERENCES learned_skills(id)
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status, priority DESC, updated_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at ASC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_learning_status ON learning_requests(status, created_at ASC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_learning_topic ON learning_requests(topic, updated_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_learned_skills_updated ON learned_skills(updated_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_skill_trials_intent ON skill_trials(intent, created_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_skill_edges_parent ON skill_edges(parent_skill_id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_skill_edges_child ON skill_edges(child_skill_id)"
            )

            now = time.time()
            db.execute(
                """
                INSERT OR IGNORE INTO memories(
                    kind, memory_key, content, importance,
                    created_at, updated_at, last_used
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "fact",
                    "self.identity",
                    "I am Chat, a persistent Minecraft companion with my own body, inventory, senses, memories, goals, and relationship with Alik. I am not Alik's player character.",
                    10,
                    now,
                    now,
                    now,
                ),
            )
            db.execute(
                """
                UPDATE tasks
                SET status='queued', progress='resuming after restart', updated_at=?
                WHERE status='running'
                """,
                (now,),
            )
            db.execute(
                """
                UPDATE learning_requests
                SET status='queued', error='', updated_at=?
                WHERE status='running'
                """,
                (now,),
            )
            db.execute(
                """
                DELETE FROM memories
                WHERE kind IN ('lesson','skill')
                  AND (
                    lower(content) LIKE '%player is not connected to a world%'
                    OR lower(content) LIKE '%owner player is unavailable%'
                    OR lower(content) LIKE '%companion server service is not ready%'
                    OR lower(content) LIKE '%no loaded companion found%'
                  )
                """
            )
            migration = db.execute(
                """
                SELECT 1 FROM memories
                WHERE kind='fact' AND memory_key='system.self_learning_v1'
                LIMIT 1
                """
            ).fetchone()
            if migration is None:
                db.execute(
                    """
                    UPDATE tasks
                    SET status='cancelled',
                        progress='retired by self-learning upgrade',
                        error='',
                        updated_at=?
                    WHERE status IN ('queued','running')
                      AND skill IN (
                        'gather_logs',
                        'make_stone_pickaxe',
                        'build_basic_house'
                      )
                    """,
                    (now,),
                )
                db.execute(
                    """
                    INSERT INTO memories(
                        kind, memory_key, content, importance,
                        created_at, updated_at, last_used
                    )
                    VALUES ('fact', 'system.self_learning_v1', ?, 8, ?, ?, ?)
                    """,
                    (
                        "High-level Minecraft procedures are now learned from experiments; legacy deterministic skills are fallback scaffolding only.",
                        now,
                        now,
                        now,
                    ),
                )

            goal_count = db.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
            if goal_count == 0:
                db.execute(
                    """
                    INSERT INTO goals(title, description, status, priority, source, created_at, updated_at)
                    VALUES (?, ?, 'active', ?, 'system', ?, ?)
                    """,
                    (
                        "Stay aware and useful",
                        "Stay aware of Alik and the surroundings. React to immediate danger, remain nearby enough to help, and when safely idle observe or explore the nearby area instead of behaving like a statue.",
                        4,
                        now,
                        now,
                    ),
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

            matched = not query_tokens or overlap > 0 or (query_lower and query_lower in haystack)
            if not matched:
                continue

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

    def create_goal(self, title, description="", priority=5, source="self"):
        title = str(title).strip()[:160]
        description = str(description).strip()[:1200]
        priority = max(1, min(10, int(priority)))
        source = str(source).strip()[:40] or "self"
        if not title:
            raise ValueError("goal title is required")
        now = time.time()
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT INTO goals(title, description, status, priority, source, created_at, updated_at)
                VALUES (?, ?, 'active', ?, ?, ?, ?)
                """,
                (title, description, priority, source, now, now),
            )
            goal_id = cursor.lastrowid
        return {
            "ok": True,
            "id": goal_id,
            "title": title,
            "status": "active",
            "priority": priority,
        }

    def list_goals(self, status="active", limit=6):
        status = str(status).strip().lower()
        limit = max(1, min(20, int(limit)))
        with self._connect() as db:
            if status == "all":
                rows = db.execute(
                    """
                    SELECT id, title, description, status, priority, source, updated_at
                    FROM goals
                    ORDER BY
                        CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                        priority DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT id, title, description, status, priority, source, updated_at
                    FROM goals
                    WHERE status=?
                    ORDER BY priority DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "description": row["description"],
                "status": row["status"],
                "priority": row["priority"],
                "source": row["source"],
            }
            for row in rows
        ]

    def update_goal(self, goal_id, status=None, description=None, priority=None):
        goal_id = int(goal_id)
        fields = []
        values = []
        if status is not None:
            status = str(status).strip().lower()
            if status not in {"active", "paused", "completed", "cancelled"}:
                raise ValueError("invalid goal status")
            fields.append("status=?")
            values.append(status)
        if description is not None:
            fields.append("description=?")
            values.append(str(description).strip()[:1200])
        if priority is not None:
            fields.append("priority=?")
            values.append(max(1, min(10, int(priority))))
        if not fields:
            raise ValueError("no goal changes supplied")
        fields.append("updated_at=?")
        values.append(time.time())
        values.append(goal_id)
        with self._connect() as db:
            changed = db.execute(
                f"UPDATE goals SET {', '.join(fields)} WHERE id=?",
                values,
            ).rowcount
        return {"ok": changed == 1, "id": goal_id}

    def record_event(self, kind, summary):
        kind = str(kind).strip().lower()[:40] or "event"
        summary = str(summary).strip()[:1200]
        if not summary:
            return
        with self._connect() as db:
            db.execute(
                "INSERT INTO events(kind, summary, created_at) VALUES (?, ?, ?)",
                (kind, summary, time.time()),
            )
            db.execute(
                """
                DELETE FROM events
                WHERE id NOT IN (
                    SELECT id FROM events ORDER BY created_at DESC LIMIT 1000
                )
                """
            )

    def recent_events(self, limit=8):
        limit = max(1, min(20, int(limit)))
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT kind, summary, created_at
                FROM events
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {"kind": row["kind"], "summary": row["summary"]}
            for row in reversed(rows)
        ]

    def create_task(self, skill, args=None):
        skill = str(skill).strip().lower()[:80]
        if not skill:
            raise ValueError("skill is required")
        payload = json.dumps(args or {}, separators=(",", ":"))
        now = time.time()
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT INTO tasks(skill, args_json, status, progress, error, created_at, updated_at)
                VALUES (?, ?, 'queued', '', '', ?, ?)
                """,
                (skill, payload, now, now),
            )
            task_id = cursor.lastrowid
        return {"ok": True, "id": task_id, "skill": skill, "status": "queued"}

    def task(self, task_id):
        with self._connect() as db:
            row = db.execute(
                """
                SELECT id, skill, args_json, status, progress, error, created_at, updated_at
                FROM tasks WHERE id=?
                """,
                (int(task_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "skill": row["skill"],
            "args": json.loads(row["args_json"]),
            "status": row["status"],
            "progress": row["progress"],
            "error": row["error"],
        }

    def list_tasks(self, limit=8):
        limit = max(1, min(20, int(limit)))
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, skill, args_json, status, progress, error
                FROM tasks
                ORDER BY
                    CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                    updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "skill": row["skill"],
                "args": json.loads(row["args_json"]),
                "status": row["status"],
                "progress": row["progress"],
                "error": row["error"],
            }
            for row in rows
        ]

    def next_queued_task(self):
        with self._connect() as db:
            row = db.execute(
                """
                SELECT id FROM tasks
                WHERE status='queued'
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            task_id = row["id"]
            changed = db.execute(
                """
                UPDATE tasks
                SET status='running', progress='starting', updated_at=?
                WHERE id=? AND status='queued'
                """,
                (time.time(), task_id),
            ).rowcount
        return self.task(task_id) if changed == 1 else None

    def update_task(self, task_id, status=None, progress=None, error=None):
        fields = []
        values = []
        if status is not None:
            if status not in {"queued", "running", "completed", "failed", "cancelled"}:
                raise ValueError("invalid task status")
            fields.append("status=?")
            values.append(status)
        if progress is not None:
            fields.append("progress=?")
            values.append(str(progress).strip()[:1200])
        if error is not None:
            fields.append("error=?")
            values.append(str(error).strip()[:2000])
        if not fields:
            return {"ok": True, "id": int(task_id)}
        fields.append("updated_at=?")
        values.append(time.time())
        values.append(int(task_id))
        with self._connect() as db:
            changed = db.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE id=?",
                values,
            ).rowcount
        return {"ok": changed == 1, "id": int(task_id)}

    def cancel_tasks(self):
        with self._connect() as db:
            count = db.execute(
                """
                UPDATE tasks
                SET status='cancelled', progress='cancelled by user', updated_at=?
                WHERE status IN ('queued', 'running')
                """,
                (time.time(),),
            ).rowcount
        return {"ok": True, "cancelled": count}

    def request_learning(self, topic, problem, cooldown_seconds=1800):
        topic = str(topic).strip().lower()[:160]
        problem = str(problem).strip()[:2400]
        if not topic or not problem:
            raise ValueError("topic and problem are required")
        now = time.time()
        cooldown_seconds = max(60, int(cooldown_seconds))

        with self._connect() as db:
            active = db.execute(
                """
                SELECT id, status, updated_at
                FROM learning_requests
                WHERE topic=? AND status IN ('queued','running')
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (topic,),
            ).fetchone()
            if active is not None:
                return {
                    "ok": True,
                    "id": active["id"],
                    "status": active["status"],
                    "reused": True,
                }

            recent = db.execute(
                """
                SELECT id, status, updated_at
                FROM learning_requests
                WHERE topic=?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (topic,),
            ).fetchone()
            if recent is not None and now - recent["updated_at"] < cooldown_seconds:
                return {
                    "ok": False,
                    "deferred": True,
                    "id": recent["id"],
                    "status": recent["status"],
                    "retry_after_seconds": int(
                        cooldown_seconds - (now - recent["updated_at"])
                    ),
                }

            cursor = db.execute(
                """
                INSERT INTO learning_requests(
                    topic, problem, status, lesson, error, model,
                    input_tokens, output_tokens, created_at, updated_at
                )
                VALUES (?, ?, 'queued', '', '', '', 0, 0, ?, ?)
                """,
                (topic, problem, now, now),
            )
            request_id = cursor.lastrowid

        return {
            "ok": True,
            "id": request_id,
            "status": "queued",
            "topic": topic,
        }

    def next_learning_request(self):
        with self._connect() as db:
            row = db.execute(
                """
                SELECT id FROM learning_requests
                WHERE status='queued'
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            request_id = row["id"]
            changed = db.execute(
                """
                UPDATE learning_requests
                SET status='running', updated_at=?
                WHERE id=? AND status='queued'
                """,
                (time.time(), request_id),
            ).rowcount
        return self.learning_request(request_id) if changed == 1 else None

    def learning_request(self, request_id):
        with self._connect() as db:
            row = db.execute(
                """
                SELECT id, topic, problem, status, lesson, error, model,
                       input_tokens, output_tokens, created_at, updated_at
                FROM learning_requests
                WHERE id=?
                """,
                (int(request_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "topic": row["topic"],
            "problem": row["problem"],
            "status": row["status"],
            "lesson": row["lesson"],
            "error": row["error"],
            "model": row["model"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
        }

    def finish_learning(
        self,
        request_id,
        *,
        status,
        lesson="",
        error="",
        model="",
        input_tokens=0,
        output_tokens=0,
    ):
        if status not in {"completed", "failed"}:
            raise ValueError("learning status must be completed or failed")
        with self._connect() as db:
            changed = db.execute(
                """
                UPDATE learning_requests
                SET status=?, lesson=?, error=?, model=?,
                    input_tokens=?, output_tokens=?, updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    str(lesson).strip()[:8000],
                    str(error).strip()[:2400],
                    str(model).strip()[:120],
                    max(0, int(input_tokens or 0)),
                    max(0, int(output_tokens or 0)),
                    time.time(),
                    int(request_id),
                ),
            ).rowcount
        return {"ok": changed == 1, "id": int(request_id), "status": status}

    def list_learning(self, limit=8):
        limit = max(1, min(20, int(limit)))
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, topic, status, lesson, error, model,
                       input_tokens, output_tokens, updated_at
                FROM learning_requests
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "topic": row["topic"],
                "status": row["status"],
                "lesson": row["lesson"],
                "error": row["error"],
                "model": row["model"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
            }
            for row in rows
        ]

    def recent_task_failures(self, skill, limit=3):
        skill = str(skill).strip().lower()
        limit = max(1, min(10, int(limit)))
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, error, updated_at
                FROM tasks
                WHERE skill=? AND status='failed'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (skill, limit),
            ).fetchall()
        return [
            {"id": row["id"], "error": row["error"], "updated_at": row["updated_at"]}
            for row in rows
        ]

    def save_learned_skill(
        self,
        name,
        intent,
        procedure,
        source="self",
    ):
        name = str(name).strip()[:120]
        intent = str(intent).strip().lower()[:240]
        procedure = str(procedure).strip()[:8000]
        source = str(source).strip().lower()[:40] or "self"
        if not name or not intent or not procedure:
            raise ValueError("name, intent and procedure are required")
        now = time.time()
        with self._connect() as db:
            row = db.execute(
                "SELECT id FROM learned_skills WHERE intent=?",
                (intent,),
            ).fetchone()
            if row is None:
                cursor = db.execute(
                    """
                    INSERT INTO learned_skills(
                        name, intent, procedure, successes, failures,
                        confidence, source, last_outcome,
                        created_at, updated_at, last_used
                    )
                    VALUES (?, ?, ?, 0, 0, 0.5, ?, '', ?, ?, ?)
                    """,
                    (name, intent, procedure, source, now, now, now),
                )
                skill_id = cursor.lastrowid
            else:
                skill_id = row["id"]
                db.execute(
                    """
                    UPDATE learned_skills
                    SET name=?, procedure=?, source=?, updated_at=?, last_used=?
                    WHERE id=?
                    """,
                    (name, procedure, source, now, now, skill_id),
                )
        return self.learned_skill(skill_id)

    def learned_skill(self, skill_id):
        with self._connect() as db:
            row = db.execute(
                """
                SELECT id, name, intent, procedure, successes, failures,
                       confidence, source, last_outcome, updated_at
                FROM learned_skills
                WHERE id=?
                """,
                (int(skill_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "intent": row["intent"],
            "procedure": row["procedure"],
            "successes": row["successes"],
            "failures": row["failures"],
            "confidence": round(float(row["confidence"]), 3),
            "source": row["source"],
            "last_outcome": row["last_outcome"],
        }

    def find_learned_skills(self, query, limit=5):
        query = str(query).strip().lower()
        limit = max(1, min(12, int(limit)))
        tokens = set(TOKEN_RE.findall(query))
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, name, intent, procedure, successes, failures,
                       confidence, source, last_outcome, updated_at
                FROM learned_skills
                ORDER BY confidence DESC, updated_at DESC
                LIMIT 200
                """
            ).fetchall()

        ranked = []
        for row in rows:
            haystack = f"{row['name']} {row['intent']} {row['procedure']}".lower()
            overlap = len(tokens & set(TOKEN_RE.findall(haystack)))
            score = overlap * 4.0 + float(row["confidence"]) * 5.0
            if query and query in haystack:
                score += 8.0
            if query and overlap == 0 and query not in haystack:
                continue
            ranked.append((score, row))

        ranked.sort(key=lambda item: item[0], reverse=True)
        selected = ranked[:limit]
        if selected:
            now = time.time()
            ids = [row["id"] for _, row in selected]
            placeholders = ",".join("?" for _ in ids)
            with self._connect() as db:
                db.execute(
                    f"UPDATE learned_skills SET last_used=? WHERE id IN ({placeholders})",
                    [now, *ids],
                )
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "intent": row["intent"],
                "procedure": row["procedure"],
                "successes": row["successes"],
                "failures": row["failures"],
                "confidence": round(float(row["confidence"]), 3),
                "source": row["source"],
                "last_outcome": row["last_outcome"],
            }
            for _, row in selected
        ]

    def list_learned_skills(self, limit=12):
        limit = max(1, min(50, int(limit)))
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id FROM learned_skills
                ORDER BY confidence DESC, updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self.learned_skill(row["id"]) for row in rows]

    def record_skill_trial(
        self,
        intent,
        hypothesis,
        actions,
        outcome,
        success,
        skill_id=None,
    ):
        intent = str(intent).strip().lower()[:240]
        hypothesis = str(hypothesis).strip()[:2400]
        actions = str(actions).strip()[:5000]
        outcome = str(outcome).strip()[:3000]
        success = bool(success)
        if not intent or not hypothesis or not actions or not outcome:
            raise ValueError("intent, hypothesis, actions and outcome are required")
        now = time.time()

        with self._connect() as db:
            resolved_skill_id = None
            if skill_id is not None:
                row = db.execute(
                    "SELECT id FROM learned_skills WHERE id=?",
                    (int(skill_id),),
                ).fetchone()
                resolved_skill_id = row["id"] if row is not None else None
            if resolved_skill_id is None:
                row = db.execute(
                    "SELECT id FROM learned_skills WHERE intent=?",
                    (intent,),
                ).fetchone()
                resolved_skill_id = row["id"] if row is not None else None

            db.execute(
                """
                INSERT INTO skill_trials(
                    skill_id, intent, hypothesis, actions, outcome, success, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_skill_id,
                    intent,
                    hypothesis,
                    actions,
                    outcome,
                    1 if success else 0,
                    now,
                ),
            )

            if resolved_skill_id is not None:
                db.execute(
                    """
                    UPDATE learned_skills
                    SET successes=successes+?,
                        failures=failures+?,
                        last_outcome=?,
                        confidence=CAST(successes + ? + 1 AS REAL)
                            / CAST(successes + failures + 3 AS REAL),
                        updated_at=?,
                        last_used=?
                    WHERE id=?
                    """,
                    (
                        1 if success else 0,
                        0 if success else 1,
                        outcome,
                        1 if success else 0,
                        now,
                        now,
                        resolved_skill_id,
                    ),
                )

            db.execute(
                """
                DELETE FROM skill_trials
                WHERE id NOT IN (
                    SELECT id FROM skill_trials ORDER BY created_at DESC LIMIT 2000
                )
                """
            )

        skill = (
            self.learned_skill(resolved_skill_id)
            if resolved_skill_id is not None
            else None
        )
        return {
            "ok": True,
            "success": success,
            "skill": skill,
        }

    def recent_skill_trials(self, intent, limit=6):
        intent = str(intent).strip().lower()[:240]
        limit = max(1, min(20, int(limit)))
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT skill_id, hypothesis, actions, outcome, success, created_at
                FROM skill_trials
                WHERE intent=?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (intent, limit),
            ).fetchall()
        return [
            {
                "skill_id": row["skill_id"],
                "hypothesis": row["hypothesis"],
                "actions": row["actions"],
                "outcome": row["outcome"],
                "success": bool(row["success"]),
            }
            for row in rows
        ]

    def recent_trials(self, limit=8):
        limit = max(1, min(20, int(limit)))
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT skill_id, intent, hypothesis, actions, outcome, success, created_at
                FROM skill_trials
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "skill_id": row["skill_id"],
                "intent": row["intent"],
                "hypothesis": row["hypothesis"],
                "actions": row["actions"],
                "outcome": row["outcome"],
                "success": bool(row["success"]),
            }
            for row in reversed(rows)
        ]

    def link_learned_skills(
        self,
        parent_skill_id,
        child_skill_id,
        relation="subskill",
        weight=1.0,
    ):
        parent_skill_id = int(parent_skill_id)
        child_skill_id = int(child_skill_id)
        relation = str(relation).strip().lower()[:40] or "subskill"
        weight = max(0.05, min(5.0, float(weight)))
        if parent_skill_id == child_skill_id:
            raise ValueError("a skill cannot depend on itself")
        if self.learned_skill(parent_skill_id) is None:
            raise ValueError("parent skill not found")
        if self.learned_skill(child_skill_id) is None:
            raise ValueError("child skill not found")
        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO skill_edges(
                    parent_skill_id, child_skill_id, relation,
                    weight, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(parent_skill_id, child_skill_id, relation)
                DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at
                """,
                (
                    parent_skill_id,
                    child_skill_id,
                    relation,
                    weight,
                    now,
                    now,
                ),
            )
        return {
            "ok": True,
            "parent_skill_id": parent_skill_id,
            "child_skill_id": child_skill_id,
            "relation": relation,
            "weight": weight,
        }

    def skill_graph(self, skill_id):
        skill_id = int(skill_id)
        root = self.learned_skill(skill_id)
        if root is None:
            return None
        with self._connect() as db:
            children = db.execute(
                """
                SELECT child_skill_id, relation, weight
                FROM skill_edges
                WHERE parent_skill_id=?
                ORDER BY weight DESC
                """,
                (skill_id,),
            ).fetchall()
            parents = db.execute(
                """
                SELECT parent_skill_id, relation, weight
                FROM skill_edges
                WHERE child_skill_id=?
                ORDER BY weight DESC
                """,
                (skill_id,),
            ).fetchall()
        return {
            "skill": root,
            "children": [
                {
                    "skill": self.learned_skill(row["child_skill_id"]),
                    "relation": row["relation"],
                    "weight": row["weight"],
                }
                for row in children
            ],
            "parents": [
                {
                    "skill": self.learned_skill(row["parent_skill_id"]),
                    "relation": row["relation"],
                    "weight": row["weight"],
                }
                for row in parents
            ],
        }

    def stats(self):
        with self._connect() as db:
            memory_count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            episode_count = db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            active_goals = db.execute(
                "SELECT COUNT(*) FROM goals WHERE status='active'"
            ).fetchone()[0]
            event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            active_tasks = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running')"
            ).fetchone()[0]
            queued_learning = db.execute(
                "SELECT COUNT(*) FROM learning_requests WHERE status IN ('queued','running')"
            ).fetchone()[0]
            learned_skills = db.execute(
                "SELECT COUNT(*) FROM learned_skills"
            ).fetchone()[0]
            skill_trials = db.execute(
                "SELECT COUNT(*) FROM skill_trials"
            ).fetchone()[0]
            skill_edges = db.execute(
                "SELECT COUNT(*) FROM skill_edges"
            ).fetchone()[0]
        return {
            "memories": memory_count,
            "episodes": episode_count,
            "active_goals": active_goals,
            "events": event_count,
            "active_tasks": active_tasks,
            "queued_learning": queued_learning,
            "learned_skills": learned_skills,
            "skill_trials": skill_trials,
            "skill_edges": skill_edges,
            "database": str(self.path),
        }


store = MemoryStore()
