"""Persistent SQLite catalogue of presence episodes (one row per episode, JSON payload)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading


class EventStore:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "wifi-events.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, epoch REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_epoch ON events(epoch DESC);
        """)
        self.db.commit()

    def add(self, event: dict):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO events(id,epoch,payload) VALUES (?,?,?)",
                (event["id"], datetime.fromisoformat(event["timestamp"]).timestamp(), json.dumps(event)),
            )

    def update(self, event_id: str, **changes):
        with self.lock, self.db:
            row = self.db.execute("SELECT payload FROM events WHERE id=?", (event_id,)).fetchone()
            if row:
                event = json.loads(row[0])
                event.update(changes)
                self.db.execute("UPDATE events SET payload=? WHERE id=?", (json.dumps(event), event_id))

    def get(self, event_id: str):
        with self.lock:
            row = self.db.execute("SELECT payload FROM events WHERE id=?", (event_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_events(self, limit: int = 20, offset: int = 0):
        with self.lock:
            rows = self.db.execute(
                "SELECT payload FROM events ORDER BY epoch DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def count_since(self, epoch: float) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM events WHERE epoch>=?", (epoch,)).fetchone()[0]

    def close_interrupted(self):
        """Episodes still open after a restart ended when the process stopped; settle them."""
        with self.lock:
            rows = self.db.execute("SELECT id FROM events WHERE json_extract(payload, '$.state')='active'").fetchall()
        for row in rows:
            self.update(row[0], state="interrupted")
        return len(rows)

    def cleanup(self, retention_days: float, now: datetime | None = None) -> int:
        limit = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
        with self.lock, self.db:
            return self.db.execute("DELETE FROM events WHERE epoch<?", (limit.timestamp(),)).rowcount

    def close(self):
        with self.lock:
            self.db.close()
