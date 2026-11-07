"""SQLite 기반 단순 문서 저장소 + 이벤트 로그."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .config import data_dir

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_path: Path | None = None


def _db() -> sqlite3.Connection:
    global _conn, _path
    path = data_dir() / "oba.db"
    if _conn is None or _path != path:
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _path = path
        _conn.executescript(
            """
            create table if not exists docs(
              kind text, id text, body text, updated real, primary key(kind, id));
            create table if not exists events(
              seq integer primary key autoincrement, ts real, kind text, ref text, body text);
            """
        )
    return _conn


def put(kind: str, doc_id: str, doc: dict) -> dict:
    with _lock:
        db = _db()
        db.execute(
            "insert or replace into docs values (?, ?, ?, ?)",
            (kind, doc_id, json.dumps(doc, ensure_ascii=False), time.time()),
        )
        db.commit()
    return doc


def get(kind: str, doc_id: str) -> dict | None:
    with _lock:
        row = _db().execute("select body from docs where kind=? and id=?", (kind, doc_id)).fetchone()
    return json.loads(row[0]) if row else None


def all_docs(kind: str) -> list[dict]:
    with _lock:
        rows = _db().execute("select body from docs where kind=? order by updated desc", (kind,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def log(kind: str, ref: str, text: str, **extra) -> dict:
    ev = {"ts": time.time(), "kind": kind, "ref": ref, "text": text, **extra}
    with _lock:
        db = _db()
        cur = db.execute(
            "insert into events(ts, kind, ref, body) values (?, ?, ?, ?)",
            (ev["ts"], kind, ref, json.dumps(ev, ensure_ascii=False)),
        )
        db.commit()
        ev["seq"] = cur.lastrowid
    print(f"[{kind}] {ref}: {text}", flush=True)
    return ev


def events(limit: int = 200) -> list[dict]:
    with _lock:
        rows = _db().execute("select seq, body from events order by seq desc limit ?", (limit,)).fetchall()
    return [{**json.loads(b), "seq": s} for s, b in rows]
