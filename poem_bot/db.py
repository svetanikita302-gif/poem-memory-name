import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .analysis import PoemAnalysis
from .config import DATABASE_PATH, DEFAULT_TIMEZONE
from .planning import build_schedule, utc_now


class Database:
    def __init__(self, path: Path | str = DATABASE_PATH):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    timezone TEXT NOT NULL,
                    sleep_time TEXT NOT NULL DEFAULT '23:00',
                    wake_time TEXT NOT NULL DEFAULT '07:00',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS poems (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source_url TEXT,
                    analysis_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    current_level INTEGER NOT NULL DEFAULT 25,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    poem_id INTEGER NOT NULL UNIQUE REFERENCES poems(id) ON DELETE CASCADE,
                    chunk_size INTEGER NOT NULL,
                    session_minutes INTEGER NOT NULL,
                    total_parts INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedule (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    poem_id INTEGER NOT NULL REFERENCES poems(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    part_number INTEGER NOT NULL DEFAULT 0,
                    scheduled_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    sent_at TEXT,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS schedule_due_idx
                    ON schedule(status, scheduled_at);
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    poem_id INTEGER NOT NULL REFERENCES poems(id) ON DELETE CASCADE,
                    mode TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    score REAL NOT NULL,
                    errors_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    def ensure_user(self, user_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO users(user_id, timezone, created_at) VALUES (?, ?, ?)",
                (user_id, DEFAULT_TIMEZONE, utc_now().isoformat()),
            )

    def get_user(self, user_id: int) -> sqlite3.Row:
        self.ensure_user(user_id)
        with self.connect() as connection:
            return connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

    def set_sleep_schedule(self, user_id: int, sleep_time: str, wake_time: str) -> None:
        self.ensure_user(user_id)
        with self.connect() as connection:
            connection.execute(
                "UPDATE users SET sleep_time = ?, wake_time = ? WHERE user_id = ?",
                (sleep_time, wake_time, user_id),
            )

    def create_poem(
        self,
        user_id: int,
        title: str,
        author: str,
        text: str,
        source_url: str | None,
        analysis: PoemAnalysis,
        sleep_time: str,
        wake_time: str,
    ) -> int:
        from .planning import parse_clock
        from .config import get_zone

        self.set_sleep_schedule(user_id, sleep_time, wake_time)
        user = self.get_user(user_id)
        schedule, parts = build_schedule(
            analysis,
            utc_now(),
            get_zone(user["timezone"]),
            parse_clock(sleep_time),
            parse_clock(wake_time),
        )
        now = utc_now().isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO poems(
                    user_id, title, author, text, source_url, analysis_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, title, author, text, source_url, json.dumps(analysis.to_dict(), ensure_ascii=False), now),
            )
            poem_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO plans(poem_id, chunk_size, session_minutes, total_parts, created_at) VALUES (?, ?, ?, ?, ?)",
                (poem_id, analysis.chunk_size, analysis.session_minutes, parts, now),
            )
            connection.executemany(
                "INSERT INTO schedule(poem_id, kind, part_number, scheduled_at) VALUES (?, ?, ?, ?)",
                [(poem_id, item["kind"], item["part_number"], item["scheduled_at"]) for item in schedule],
            )
        return poem_id

    def get_poem(self, poem_id: int, user_id: int | None = None) -> sqlite3.Row | None:
        query = "SELECT * FROM poems WHERE id = ?"
        params: tuple[Any, ...] = (poem_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            params = (poem_id, user_id)
        with self.connect() as connection:
            return connection.execute(query, params).fetchone()

    def get_active_poem(self, user_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM poems WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()

    def get_plan(self, poem_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM plans WHERE poem_id = ?", (poem_id,)).fetchone()

    def get_schedule(self, poem_id: int, include_done: bool = False) -> list[sqlite3.Row]:
        query = "SELECT * FROM schedule WHERE poem_id = ?"
        if not include_done:
            query += " AND status IN ('pending', 'sent')"
        query += " ORDER BY scheduled_at, id"
        with self.connect() as connection:
            return list(connection.execute(query, (poem_id,)).fetchall())

    def get_next_schedule(self, poem_id: int, kind: str | None = None) -> sqlite3.Row | None:
        query = "SELECT * FROM schedule WHERE poem_id = ? AND status IN ('pending', 'sent')"
        params: list[Any] = [poem_id]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY scheduled_at, id LIMIT 1"
        with self.connect() as connection:
            return connection.execute(query, params).fetchone()

    def get_schedule_item(self, schedule_id: int, poem_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM schedule WHERE id = ? AND poem_id = ?",
                (schedule_id, poem_id),
            ).fetchone()

    def due_reminders(self, now: datetime) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute(
                """SELECT s.*, p.user_id, p.title, u.timezone, u.sleep_time, u.wake_time
                   FROM schedule s
                   JOIN poems p ON p.id = s.poem_id
                   JOIN users u ON u.user_id = p.user_id
                   WHERE s.status = 'pending' AND s.scheduled_at <= ? AND p.status = 'active'
                   ORDER BY s.scheduled_at LIMIT 100""",
                (now.isoformat(),),
            ).fetchall())

    def mark_sent(self, schedule_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE schedule SET status = 'sent', sent_at = ? WHERE id = ? AND status = 'pending'",
                (utc_now().isoformat(), schedule_id),
            )

    def complete_schedule(self, schedule_id: int, poem_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE schedule SET status = 'done', completed_at = ?
                   WHERE id = ? AND poem_id = ? AND status IN ('pending', 'sent')""",
                (utc_now().isoformat(), schedule_id, poem_id),
            )

    def reschedule(self, schedule_id: int, when: datetime) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE schedule SET scheduled_at = ?, status = 'pending', sent_at = NULL WHERE id = ?",
                (when.isoformat(), schedule_id),
            )

    def add_review(self, poem_id: int, when: datetime) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO schedule(poem_id, kind, part_number, scheduled_at) VALUES (?, 'review', 0, ?)",
                (poem_id, when.isoformat()),
            )
            return int(cursor.lastrowid)

    def record_attempt(self, poem_id: int, mode: str, level: int, score: float, errors: list[str]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO attempts(poem_id, mode, level, score, errors_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (poem_id, mode, level, score, json.dumps(errors, ensure_ascii=False), utc_now().isoformat()),
            )

    def set_level(self, poem_id: int, level: int) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE poems SET current_level = ? WHERE id = ?", (level, poem_id))

    def cancel_poem(self, poem_id: int, user_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE poems SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'active'",
                (poem_id, user_id),
            )
            connection.execute(
                "UPDATE schedule SET status = 'skipped' WHERE poem_id = ? AND status IN ('pending', 'sent')",
                (poem_id,),
            )
            return cursor.rowcount > 0

    def progress(self, user_id: int) -> dict:
        with self.connect() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM poems WHERE user_id = ? GROUP BY status",
                    (user_id,),
                )
            }
            stats = connection.execute(
                """SELECT COUNT(a.id) AS attempts, ROUND(AVG(a.score) * 100, 1) AS average,
                          MAX(a.created_at) AS last_attempt
                   FROM attempts a JOIN poems p ON p.id = a.poem_id WHERE p.user_id = ?""",
                (user_id,),
            ).fetchone()
            recent = list(connection.execute(
                """SELECT p.title, a.mode, a.level, a.score, a.created_at
                   FROM attempts a JOIN poems p ON p.id = a.poem_id
                   WHERE p.user_id = ? ORDER BY a.id DESC LIMIT 5""",
                (user_id,),
            ).fetchall())
        return {"counts": counts, "stats": stats, "recent": recent}

    def next_after_rating(self, poem_id: int, rating: str) -> datetime | None:
        now = utc_now()
        next_item = self.get_next_schedule(poem_id)
        if rating == "easy":
            if next_item:
                old = datetime.fromisoformat(next_item["scheduled_at"])
                remaining = max(old - now, timedelta(minutes=15))
                when = now + remaining * 1.5
                self.reschedule(next_item["id"], when)
            else:
                when = now + timedelta(days=3)
                self.add_review(poem_id, when)
        elif rating == "forgot":
            when = now + timedelta(minutes=20)
            if next_item:
                self.reschedule(next_item["id"], when)
            else:
                self.add_review(poem_id, when)
        else:
            if next_item:
                when = datetime.fromisoformat(next_item["scheduled_at"])
            else:
                when = now + timedelta(days=1)
                self.add_review(poem_id, when)
        return when
