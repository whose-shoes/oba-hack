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
    from . import account

    fid = extra.pop("family_id", None) or account.log_family.get()
    ev = {"ts": time.time(), "kind": kind, "ref": ref, "text": text, **extra}
    if fid:
        ev["family_id"] = fid
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


def events(limit: int = 200, family_id: str | None = None) -> list[dict]:
    """최근 이벤트. family_id를 주면 그 계정 것만."""
    with _lock:
        rows = _db().execute("select seq, body from events order by seq desc limit ?",
                             (limit * 20 if family_id else limit,)).fetchall()
    out = [{**json.loads(b), "seq": s} for s, b in rows]
    if family_id:
        out = [e for e in out if e.get("family_id") == family_id][:limit]
    return out
