"""Archivio locale SQLite delle riunioni."""
from __future__ import annotations

import sqlite3
import threading
import time

from .config import DATA_DIR, DB_PATH

_SCHEMA = """
create table if not exists meetings(
  id integer primary key, title text default '', created real, ended real,
  notes text default '', enhanced text default '', language text);
create table if not exists segments(
  id integer primary key, meeting_id integer references meetings(id) on delete cascade,
  speaker text, t0 real, text text);
"""


class Store:
    def __init__(self, path=DB_PATH):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.lock = threading.Lock()

    def _x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def create(self, title="") -> int:
        return self._x("insert into meetings(title, created) values(?,?)", (title, time.time())).lastrowid

    def list(self):
        return [dict(r) for r in self._x("select id,title,created,ended from meetings order by created desc")]

    def get(self, mid: int):
        r = self._x("select * from meetings where id=?", (mid,)).fetchone()
        if not r:
            return None
        m = dict(r)
        m["segments"] = self.segments(mid)
        return m

    def segments(self, mid: int):
        return [dict(r) for r in self._x("select speaker,t0,text from segments where meeting_id=? order by t0", (mid,))]

    def add_segment(self, mid: int, speaker: str, t0: float, text: str):
        self._x("insert into segments(meeting_id,speaker,t0,text) values(?,?,?,?)", (mid, speaker, t0, text))

    def update(self, mid: int, **fields):
        keys = [k for k in fields if k in {"title", "notes", "enhanced", "ended", "language"}]
        if keys:
            self._x(f"update meetings set {', '.join(k + '=?' for k in keys)} where id=?",
                    [fields[k] for k in keys] + [mid])

    def delete(self, mid: int):
        self._x("delete from segments where meeting_id=?", (mid,))
        self._x("delete from meetings where id=?", (mid,))
