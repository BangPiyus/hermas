"""SQLite storage with WAL, append-only conversation records, audit hash chain."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def as_json(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self.directory.chmod(0o700)
        except OSError:
            pass
        self.file = self.directory / "hermas.sqlite3"
        self._audit_mutex = threading.RLock()
        with self.connection() as db:
            schema_version = db.execute("PRAGMA user_version").fetchone()[0]
            if schema_version > 6:
                raise RuntimeError("Database dibuat oleh versi Hermas yang lebih baru; upgrade aplikasi dulu")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL, content TEXT, tool_call_id TEXT,
                    tool_calls_json TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id,id);
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    tool_call_id TEXT NOT NULL UNIQUE, tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL, preview TEXT NOT NULL,
                    expected_sha256 TEXT, state TEXT NOT NULL,
                    result TEXT, created_at TEXT NOT NULL, decided_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(session_id,state);
                CREATE TABLE IF NOT EXISTS memories (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL,
                    source_session TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS skills (
                    slug TEXT PRIMARY KEY, content TEXT NOT NULL,
                    source_session TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    model TEXT NOT NULL, prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    request_key TEXT,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    elapsed_ms INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency
                    ON tasks(session_id,request_key) WHERE request_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(status,created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id,created_at);
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    event TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id,id);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL, details TEXT NOT NULL,
                    previous_hash TEXT NOT NULL, row_hash TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
            if "kind" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS verified_memories (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, key TEXT NOT NULL,
                    value TEXT NOT NULL, evidence TEXT NOT NULL, confidence REAL NOT NULL,
                    status TEXT NOT NULL, source_session TEXT NOT NULL,
                    expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(project,key)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_project ON verified_memories(project,status,updated_at);
                CREATE TABLE IF NOT EXISTS orchestration_steps (
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    role TEXT NOT NULL, result TEXT NOT NULL, duration_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(task_id,role)
                );
            """)
            # Old memories were explicitly approved by a human in v0.1/v0.2.
            db.execute("""INSERT OR IGNORE INTO verified_memories
                (id,project,key,value,evidence,confidence,status,source_session,expires_at,created_at,updated_at)
                SELECT 'legacy:'||key,'global',key,value,'Migrasi dari memory yang telah disetujui di versi sebelumnya',
                       0.70,'verified',source_session,NULL,updated_at,updated_at FROM memories""")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    name TEXT NOT NULL, steps_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflows(id),
                    session_id TEXT NOT NULL REFERENCES sessions(id), status TEXT NOT NULL,
                    step_index INTEGER NOT NULL, last_task_id TEXT NOT NULL REFERENCES tasks(id),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    name TEXT NOT NULL, cron TEXT NOT NULL, target TEXT NOT NULL,
                    message TEXT NOT NULL, workflow_id TEXT REFERENCES workflows(id),
                    enabled INTEGER NOT NULL DEFAULT 0, next_run_at TEXT NOT NULL,
                    last_run_at TEXT, last_task_id TEXT, last_error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled,next_run_at);
                CREATE TABLE IF NOT EXISTS integrations (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, manifest_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gateway_state (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS vps_hosts (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, hostname TEXT NOT NULL,
                    ssh_user TEXT NOT NULL, port INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                    retired INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vps_checks (
                    id TEXT PRIMARY KEY, host_id TEXT NOT NULL REFERENCES vps_hosts(id),
                    kind TEXT NOT NULL, observed_at TEXT NOT NULL, status TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vps_checks_host ON vps_checks(host_id,observed_at);
                CREATE TABLE IF NOT EXISTS vps_plans (
                    id TEXT PRIMARY KEY, host_id TEXT NOT NULL REFERENCES vps_hosts(id),
                    kind TEXT NOT NULL, root TEXT NOT NULL, notes TEXT NOT NULL,
                    status TEXT NOT NULL, steps_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS auth_users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('viewer','operator')),
                    enabled INTEGER NOT NULL DEFAULT 1, session_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vps_monitors (
                    id TEXT PRIMARY KEY,host_id TEXT NOT NULL REFERENCES vps_hosts(id),
                    kind TEXT NOT NULL CHECK(kind IN ('health','wordpress')),root TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                    next_run_at TEXT NOT NULL,last_run_at TEXT,last_status TEXT,last_check_id TEXT,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vps_monitors_queue ON vps_monitors(enabled,next_run_at);
                CREATE TABLE IF NOT EXISTS op_alerts (
                    id TEXT PRIMARY KEY,fingerprint TEXT UNIQUE NOT NULL,
                    host_id TEXT NOT NULL REFERENCES vps_hosts(id),kind TEXT NOT NULL,
                    severity TEXT NOT NULL,status TEXT NOT NULL,count INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alert_deliveries (
                    id TEXT PRIMARY KEY,alert_id TEXT NOT NULL REFERENCES op_alerts(id),
                    status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_alert_deliveries_status ON alert_deliveries(status,created_at);
                CREATE TABLE IF NOT EXISTS workspace_snapshots (
                    id TEXT PRIMARY KEY,archive_name TEXT NOT NULL,archive_sha256 TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,files_count INTEGER NOT NULL,total_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            db.execute("PRAGMA user_version=6")
        try:
            self.file.chmod(0o600)
        except OSError:
            pass

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.file), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def audit(self, event: str, details: dict[str, Any]):
        # Do not audit raw file contents, passwords, or unredacted tool arguments.
        stamp = now()
        detail = as_json(details)
        with self._audit_mutex, self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prev = db.execute("SELECT row_hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            last = prev["row_hash"] if prev else "0" * 64
            digest = hashlib.sha256((last + "|" + stamp + "|" + event + "|" + detail).encode()).hexdigest()
            db.execute("INSERT INTO audit(event,details,previous_hash,row_hash,created_at) VALUES(?,?,?,?,?)",
                       (event, detail, last, digest, stamp))

    def audit_verified(self) -> bool:
        previous = "0" * 64
        with self.connection() as db:
            for row in db.execute("SELECT * FROM audit ORDER BY id"):
                digest = hashlib.sha256((previous + "|" + row["created_at"] + "|" + row["event"] + "|" + row["details"]).encode()).hexdigest()
                if row["previous_hash"] != previous or row["row_hash"] != digest:
                    return False
                previous = digest
        return True

    def create_session(self) -> dict:
        identity = str(uuid.uuid4())
        stamp = now()
        with self.connection() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (identity, "Obrolan baru", stamp, stamp))
        self.audit("session_created", {"session": identity})
        return {"id": identity, "title": "Obrolan baru", "created_at": stamp, "updated_at": stamp}

    def sessions(self) -> list[dict]:
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 100")]

    def exists(self, session_id: str) -> bool:
        with self.connection() as db:
            return db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is not None

    def add_message(self, session_id: str, role: str, content: str | None,
                    tool_call_id: str | None = None, tool_calls: list | None = None):
        with self.connection() as db:
            db.execute("INSERT INTO messages(session_id,role,content,tool_call_id,tool_calls_json,created_at) VALUES(?,?,?,?,?,?)",
                       (session_id, role, content, tool_call_id, as_json(tool_calls) if tool_calls is not None else None, now()))
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), session_id))
            if role == "user":
                count = db.execute("SELECT COUNT(1) FROM messages WHERE session_id=? AND role='user'", (session_id,)).fetchone()[0]
                if count == 1:
                    db.execute("UPDATE sessions SET title=? WHERE id=?", (content[:65], session_id))

    def messages(self, session_id: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        output = []
        for row in rows:
            record = dict(row)
            record["tool_calls"] = json.loads(record.pop("tool_calls_json")) if record["tool_calls_json"] else None
            output.append(record)
        return output

    def pending(self, session_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM approvals WHERE state IN ('pending','unknown')"
        args: tuple = ()
        if session_id is not None:
            sql += " AND session_id=?"
            args = (session_id,)
        with self.connection() as db:
            return [dict(r) for r in db.execute(sql + " ORDER BY created_at,id", args)]

    def create_approval(self, session_id: str, tool_call_id: str, name: str,
                        arguments: dict, preview: str, expected_sha256: str | None) -> str:
        identity = str(uuid.uuid4())
        with self.connection() as db:
            db.execute("INSERT INTO approvals(id,session_id,tool_call_id,tool_name,arguments_json,preview,expected_sha256,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (identity, session_id, tool_call_id, name, as_json(arguments), preview[:14000], expected_sha256, "pending", now()))
        self.audit("approval_requested", {"id": identity, "session": session_id, "tool": name})
        return identity

    def approval(self, identity: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (identity,)).fetchone()
        return dict(row) if row else None

    def decide(self, identity: str, accepted: bool, result: str):
        with self.connection() as db:
            cursor = db.execute("UPDATE approvals SET state=?, result=?, decided_at=? WHERE id=? AND state='pending'",
                                ("approved" if accepted else "rejected", result[:12000], now(), identity))
            if cursor.rowcount != 1:
                raise ValueError("Approval telah diproses")
        self.audit("approval_decided", {"id": identity, "approved": accepted, "outcome": result[:120]})

    def list_memories(self) -> list[dict]:
        # v0.3 only publishes non-revoked, non-expired verified facts.
        return self.verified_memories()

    def search_memories(self, needle: str) -> list[dict]:
        return self.relevant_memories(needle)

    def set_memory(self, key: str, value: str, session_id: str):
        with self.connection() as db:
            db.execute("INSERT INTO memories(key,value,source_session,updated_at) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,source_session=excluded.source_session,updated_at=excluded.updated_at",
                       (key, value, session_id, now()))

    def list_skills(self) -> list[dict]:
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM skills ORDER BY updated_at DESC LIMIT 40")]

    def set_skill(self, slug: str, content: str, session_id: str):
        with self.connection() as db:
            db.execute("INSERT INTO skills(slug,content,source_session,updated_at) VALUES(?,?,?,?) ON CONFLICT(slug) DO UPDATE SET content=excluded.content,source_session=excluded.source_session,updated_at=excluded.updated_at",
                       (slug, content, session_id, now()))

    def log_usage(self, sid: str, model: str, prompt: int, completion: int):
        with self.connection() as db:
            db.execute("INSERT INTO usage(session_id,model,prompt_tokens,completion_tokens,created_at) VALUES(?,?,?,?,?)",
                       (sid, model, prompt, completion, now()))

    def usage_today(self) -> dict:
        day = datetime.now(timezone.utc).date().isoformat()
        with self.connection() as db:
            record = db.execute("SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c, COUNT(*) n FROM usage WHERE created_at>=?", (day,)).fetchone()
        return {"prompt_tokens": record["p"], "completion_tokens": record["c"], "calls": record["n"], "total_tokens": record["p"] + record["c"]}

    def recent_audit(self) -> list[dict]:
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 80")]


    # ------- v0.2 durable task queue (SQLite transactions; single worker process) -------

    def _task_event(self, db: sqlite3.Connection, task_id: str, event: str, details: dict | None = None):
        db.execute("INSERT INTO task_events(task_id,event,details,created_at) VALUES(?,?,?,?)",
                   (task_id, event, as_json(details or {}), now()))

    def enqueue_task(self, session_id: str, message: str, key: str | None = None, kind: str = "chat") -> dict:
        """Atomically deduplicate request and insert user message plus queued task."""
        text = message.strip()
        if not text or len(text) > 10000:
            raise ValueError("Panjang pesan tidak valid")
        if key is not None and (not 8 <= len(key) <= 100 or not all(c.isalnum() or c in "-_." for c in key)):
            raise ValueError("Idempotency key harus 8-100 karakter: huruf, angka, -, _, .")
        if kind not in {"chat", "orchestrate"}:
            raise ValueError("Jenis tugas tidak didukung")
        digest = hashlib.sha256((kind + "\0" + text).encode("utf-8")).hexdigest()
        stamp = now()
        created = False
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise LookupError("Sesi tidak ditemukan")
            if key:
                old = db.execute("SELECT * FROM tasks WHERE session_id=? AND request_key=?", (session_id, key)).fetchone()
                if old:
                    if old["input_hash"] != digest:
                        raise ValueError("Idempotency key sudah digunakan untuk pesan berbeda")
                    return dict(old)
            blocking = db.execute("SELECT id FROM tasks WHERE session_id=? AND status IN ('queued','running','approval_required','interrupted') LIMIT 1", (session_id,)).fetchone()
            if blocking or db.execute("SELECT 1 FROM approvals WHERE session_id=? AND state IN ('pending','executing','unknown') LIMIT 1", (session_id,)).fetchone():
                raise ValueError("Masih ada tugas atau approval aktif pada sesi ini")
            count = db.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0]
            if count >= 50:
                raise ValueError("Antrean penuh (50 tugas). Tunggu atau batalkan tugas lain")
            task_id = str(uuid.uuid4())
            db.execute("INSERT INTO tasks(id,session_id,request_key,input_hash,status,created_at,updated_at,kind) VALUES(?,?,?,?,?,?,?,?)",
                       (task_id, session_id, key, digest, "queued", stamp, stamp, kind))
            db.execute("INSERT INTO messages(session_id,role,content,tool_call_id,tool_calls_json,created_at) VALUES(?,?,?,?,?,?)",
                       (session_id,"user",text,None,None,stamp))
            count = db.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND role='user'", (session_id,)).fetchone()[0]
            db.execute("UPDATE sessions SET updated_at=?, title=CASE WHEN ?=1 THEN ? ELSE title END WHERE id=?",
                       (stamp, count, text[:65], session_id))
            self._task_event(db, task_id, "enqueued")
            created = True
            task = dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        if created:
            self.audit("task_enqueued", {"task": task["id"], "session": session_id})
        return task

    def task(self, task_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def tasks(self, session_id: str | None = None, limit: int = 50) -> list[dict]:
        with self.connection() as db:
            if session_id:
                rows = db.execute("SELECT * FROM tasks WHERE session_id=? ORDER BY rowid DESC LIMIT ?",
                                  (session_id, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM tasks ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def active_task(self, session_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE session_id=? AND status IN ('queued','running','approval_required','interrupted') ORDER BY created_at DESC LIMIT 1",
                             (session_id,)).fetchone()
        return dict(row) if row else None

    def claim_next_task(self) -> dict | None:
        """One worker claims the next task transactionally; never auto-retries uncertain work."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            stamp = now()
            db.execute("UPDATE tasks SET status='running', attempts=attempts+1, started_at=COALESCE(started_at,?),updated_at=? WHERE id=? AND status='queued'",
                       (stamp,stamp,row["id"]))
            self._task_event(db,row["id"],"started",{"attempt":row["attempts"]+1})
            return dict(db.execute("SELECT * FROM tasks WHERE id=?",(row["id"],)).fetchone())

    def cancel_requested(self, task_id: str) -> bool:
        with self.connection() as db:
            row=db.execute("SELECT cancel_requested FROM tasks WHERE id=?",(task_id,)).fetchone()
        return bool(row and row[0])

    def finish_task(self, task_id: str, status: str, elapsed_ms: int, error: str | None = None) -> dict:
        if status not in {"completed","approval_required","failed","canceled","step_limit","budget_exceeded"}:
            raise ValueError("Status selesai tidak didukung")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
            if not row or row["status"] != "running":
                raise ValueError("Tugas tidak sedang berjalan")
            if row["cancel_requested"]:
                status="canceled"
                # A cancellation racing with tool proposals must also revoke
                # pending approvals: a canceled task may never execute later.
                approvals=db.execute("SELECT * FROM approvals WHERE session_id=? AND state='pending'",(row["session_id"],)).fetchall()
                for a in approvals:
                    db.execute("UPDATE approvals SET state='rejected',result='Tugas dibatalkan oleh administrator.',decided_at=? WHERE id=?",(now(),a["id"]))
                    db.execute("INSERT INTO messages(session_id,role,content,tool_call_id,created_at) VALUES(?,?,?,?,?)",
                               (row["session_id"],"tool","Tugas dibatalkan oleh administrator.",a["tool_call_id"],now()))
            stamp=now()
            db.execute("UPDATE tasks SET status=?,last_error=?,elapsed_ms=?,updated_at=?,finished_at=? WHERE id=?",
                       (status, (error or "")[:250] or None, max(0,int(elapsed_ms)), stamp,
                        None if status=="approval_required" else stamp, task_id))
            self._task_event(db,task_id,status,{"elapsed_ms":max(0,int(elapsed_ms))})
            result = dict(db.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone())
        self.audit("task_finished",{"task":task_id,"status":status})
        return result

    def recover_orphan_tasks(self) -> int:
        """Mark previously running jobs interrupted; never silently replay model/tool calls."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            ids = [r[0] for r in db.execute("SELECT id FROM tasks WHERE status='running'").fetchall()]
            for task_id in ids:
                db.execute("UPDATE tasks SET status='interrupted',last_error='Proses terhenti; periksa jejak tugas lalu lanjutkan secara manual.',updated_at=? WHERE id=?",(now(),task_id))
                self._task_event(db,task_id,"interrupted")
            approvals = [r[0] for r in db.execute("SELECT id FROM approvals WHERE state='executing'").fetchall()]
            for aid in approvals:
                db.execute("UPDATE approvals SET state='unknown',result='Hasil tindakan tidak diketahui setelah restart. Periksa workspace sebelum menandai selesai.' WHERE id=?",(aid,))
        for task_id in ids:
            self.audit("task_interrupted",{"task":task_id})
        for aid in approvals:
            self.audit("approval_uncertain",{"approval":aid})
        return len(ids)

    def resume_task(self, task_id: str, max_attempts: int = 3) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
            if not row:
                raise LookupError("Tugas tidak ditemukan")
            if row["status"] not in {"failed","interrupted","approval_required"}:
                raise ValueError("Status tugas tidak dapat dilanjutkan")
            if row["status"] in {"failed","interrupted"} and row["attempts"] >= max_attempts:
                raise ValueError("Batas percobaan tercapai")
            if db.execute("SELECT 1 FROM approvals WHERE session_id=? AND state IN ('pending','executing','unknown') LIMIT 1",(row["session_id"],)).fetchone():
                raise ValueError("Selesaikan semua approval sebelum melanjutkan")
            db.execute("UPDATE tasks SET status='queued',cancel_requested=0,last_error=NULL,updated_at=?,finished_at=NULL WHERE id=?",(now(),task_id))
            self._task_event(db,task_id,"resumed")
            result=dict(db.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone())
        self.audit("task_resumed",{"task":task_id})
        return result

    def resume_after_approval(self, session_id: str) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT * FROM tasks WHERE session_id=? AND status='approval_required' ORDER BY created_at DESC LIMIT 1", (session_id,)).fetchone()
            if not row:
                return False
            if db.execute("SELECT 1 FROM approvals WHERE session_id=? AND state IN ('pending','executing','unknown') LIMIT 1",(session_id,)).fetchone():
                return False
            db.execute("UPDATE tasks SET status='queued',updated_at=? WHERE id=?",(now(),row["id"]))
            self._task_event(db,row["id"],"approval_resolved")
        self.audit("task_resumed_after_approval",{"task":row["id"]})
        return True

    def cancel_task(self, task_id: str) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
            if not row:
                raise LookupError("Tugas tidak ditemukan")
            if row["status"] not in {"queued","running","approval_required","interrupted","failed"}:
                raise ValueError("Tugas tidak dapat dibatalkan")
            if row["status"] == "running":
                db.execute("UPDATE tasks SET cancel_requested=1,updated_at=? WHERE id=?",(now(),task_id))
                self._task_event(db,task_id,"cancel_requested")
            else:
                stamp=now()
                db.execute("UPDATE tasks SET status='canceled',cancel_requested=1,updated_at=?,finished_at=? WHERE id=?",(stamp,stamp,task_id))
                approvals=db.execute("SELECT * FROM approvals WHERE session_id=? AND state='pending'",(row["session_id"],)).fetchall()
                for a in approvals:
                    db.execute("UPDATE approvals SET state='rejected',result='Tugas dibatalkan oleh administrator.',decided_at=? WHERE id=?",(stamp,a["id"]))
                    db.execute("INSERT INTO messages(session_id,role,content,tool_call_id,created_at) VALUES(?,?,?,?,?)",
                               (row["session_id"],"tool","Tugas dibatalkan oleh administrator.",a["tool_call_id"],stamp))
                self._task_event(db,task_id,"canceled")
            result=dict(db.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone())
        self.audit("task_cancel",{"task":task_id,"state":result["status"]})
        return result

    def claim_approval(self, approval_id: str) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor=db.execute("UPDATE approvals SET state='executing' WHERE id=? AND state='pending'",(approval_id,))
            return cursor.rowcount == 1

    def finalize_approval(self, approval_id: str, state: str, result: str):
        if state not in {"approved","rejected","failed"}:
            raise ValueError("State approval tidak valid")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor=db.execute("UPDATE approvals SET state=?,result=?,decided_at=? WHERE id=? AND state='executing'",
                              (state,result[:12000],now(),approval_id))
            if cursor.rowcount != 1:
                raise ValueError("Approval tidak sedang diproses")
        self.audit("approval_decided",{"id":approval_id,"state":state})

    def acknowledge_unknown(self, approval_id: str):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT * FROM approvals WHERE id=? AND state='unknown'",(approval_id,)).fetchone()
            if not row:
                raise ValueError("Approval bukan dalam kondisi tidak pasti")
            db.execute("UPDATE approvals SET state='reviewed',result='Hasil tindakan tidak pasti; administrator menyatakan telah memeriksa hasil secara manual.',decided_at=? WHERE id=?",(now(),approval_id))
            db.execute("INSERT INTO messages(session_id,role,content,tool_call_id,created_at) VALUES(?,?,?,?,?)",
                       (row["session_id"],"tool","HASIL TIDAK PASTI: administrator telah meninjau hasil tindakan di luar sistem. Jangan menganggap perubahan berhasil tanpa verifikasi.",row["tool_call_id"],now()))
        self.audit("approval_uncertain_acknowledged",{"approval":approval_id})
        return row["session_id"]

    def metrics(self) -> dict:
        with self.connection() as db:
            counts={r["status"]:r["n"] for r in db.execute("SELECT status,COUNT(*) n FROM tasks GROUP BY status")}
            avg=db.execute("SELECT ROUND(AVG(elapsed_ms),1) FROM tasks WHERE elapsed_ms IS NOT NULL AND status='completed'").fetchone()[0]
            events=[dict(r) for r in db.execute("SELECT task_id,event,details,created_at FROM task_events ORDER BY id DESC LIMIT 30")]
            failure=db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('failed','interrupted')").fetchone()[0]
        return {"counts":counts,"average_completed_ms":avg or 0,"failures":failure,
                "usage_today":self.usage_today(),"recent_events":events,"audit_verified":self.audit_verified()}

    # ------- v0.3 human-verified, provenance-aware, expiring memory -------

    def save_verified_memory(self, key: str, value: str, session_id: str,
                             project: str = "global", evidence: str = "Disetujui administrator",
                             confidence: float = 0.8, expires_at: str | None = None) -> dict:
        identity = str(uuid.uuid4())
        stamp = now()
        if expires_at is not None:
            expiry=datetime.fromisoformat(expires_at)
            if expiry.tzinfo is None or expiry.utcoffset().total_seconds()!=0:
                raise ValueError("Expiry wajib ISO-8601 UTC")
            expires_at=expiry.astimezone(timezone.utc).isoformat(timespec="seconds")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id,created_at FROM verified_memories WHERE project=? AND key=?", (project,key)).fetchone()
            if existing:
                identity = existing["id"]
                created = existing["created_at"]
            else:
                created = stamp
            db.execute("""INSERT INTO verified_memories(id,project,key,value,evidence,confidence,status,source_session,expires_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project,key) DO UPDATE SET
                value=excluded.value,evidence=excluded.evidence,confidence=excluded.confidence,
                status='verified',source_session=excluded.source_session,expires_at=excluded.expires_at,
                updated_at=excluded.updated_at""",
                (identity,project,key,value,evidence,confidence,"verified",session_id,expires_at,created,stamp))
            row = dict(db.execute("SELECT * FROM verified_memories WHERE project=? AND key=?",(project,key)).fetchone())
        self.audit("memory_verified", {"id": identity, "project": project, "source_session": session_id})
        return row

    def revoke_verified_memory(self, identity: str) -> bool:
        with self.connection() as db:
            cursor = db.execute("UPDATE verified_memories SET status='revoked',updated_at=? WHERE id=? AND status='verified'", (now(),identity))
        if cursor.rowcount:
            self.audit("memory_revoked", {"id": identity})
        return cursor.rowcount == 1

    def verified_memories(self, project: str | None = None, include_revoked: bool = False, limit: int = 100) -> list[dict]:
        query = "SELECT * FROM verified_memories WHERE 1=1"
        args = []
        if not include_revoked:
            query += " AND status='verified' AND (expires_at IS NULL OR expires_at>?)"
            args.append(now())
        if project is not None:
            query += " AND project IN ('global', ?)"
            args.append(project)
        query += " ORDER BY updated_at DESC LIMIT ?"
        args.append(min(max(1,int(limit)),200))
        with self.connection() as db:
            return [dict(row) for row in db.execute(query, args)]

    def relevant_memories(self, query: str, project: str | None = None, limit: int = 8) -> list[dict]:
        import re
        tokens = {word for word in re.findall(r"[\w-]{3,}", query.lower()) if len(word)>2}
        records = self.verified_memories(project,limit=150)
        if not tokens:
            return records[:limit]
        ranked = []
        for row in records:
            hay = (row['key']+' '+row['value']+' '+row['project']).lower()
            score = sum(2 if token in row['key'].lower() else 1 for token in tokens if token in hay)
            if score:
                ranked.append((score, row))
        ranked.sort(key=lambda x: (-x[0], -x[1]['confidence'], x[1]['updated_at']))
        return [r for _,r in ranked[:limit]]

    # ------- v0.3 persisted individual multi-agent stages -------

    def orchestration_steps(self, task_id: str) -> list[dict]:
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM orchestration_steps WHERE task_id=? ORDER BY rowid",(task_id,))]

    def save_orchestration_step(self, task_id: str, role: str, result: str, duration_ms: int):
        with self.connection() as db:
            db.execute("INSERT INTO orchestration_steps(task_id,role,result,duration_ms,created_at) VALUES(?,?,?,?,?)",
                       (task_id,role,result[:10000],max(0,duration_ms),now()))
        self.audit("orchestration_step", {"task": task_id,"role":role,"duration_ms":duration_ms})
