"""Persistent, UTC cron scheduling and serial workflow execution.

Security: limited to pre-approved agent task types; no shell/HTTP side effects.
All scheduled enqueue decisions and next-tick updates are transactional. Missed
occurrences are coalesced, never replayed on startup.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import Store, as_json, now


class AutomationError(ValueError):
    pass


def parse_cron(expression: str) -> tuple[set[int], ...]:
    fields = expression.strip().split()
    if len(fields) != 5:
        raise AutomationError('Cron harus lima bagian: menit jam tanggal bulan hari-dalam-pekan (UTC)')
    limits = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    parsed = []
    for part, (low, high) in zip(fields, limits):
        values: set[int] = set()
        for item in part.split(','):
            match = re.fullmatch(r'(\*|\d+(?:-\d+)?)(?:/(\d+))?', item)
            if not match:
                raise AutomationError('Format cron tidak valid')
            base, stride = match.groups()
            step = int(stride) if stride else 1
            if not 1 <= step <= 100:
                raise AutomationError('Langkah cron tidak valid')
            if base == '*':
                a, b = low, high
            elif '-' in base:
                a, b = map(int, base.split('-'))
            else:
                a = int(base)
                b = high if stride else a
            if not low <= a <= b <= high:
                raise AutomationError('Angka cron di luar rentang')
            values.update(range(a, b + 1, step))
        if not values:
            raise AutomationError('Ekspresi cron kosong')
        parsed.append({0 if v == 7 and len(parsed) == 4 else v for v in values})
    return tuple(parsed)


def cron_match(expression: str, dt: datetime, parsed: tuple[set[int], ...] | None = None) -> bool:
    minute, hour, day, month, weekday = parsed or parse_cron(expression)
    # Cron Sunday=0, Python Monday=0.
    day_match = dt.day in day
    weekday_match = ((dt.weekday() + 1) % 7) in weekday
    pieces = expression.split()
    # Vixie semantics: when DOM and DOW are both restricted, either matches.
    if pieces[2] != '*' and pieces[4] != '*':
        date_ok = day_match or weekday_match
    else:
        date_ok = day_match and weekday_match
    return dt.minute in minute and dt.hour in hour and dt.month in month and date_ok


def cron_next(expression: str, after: datetime) -> str:
    parsed = parse_cron(expression)
    candidate = after.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    # A leap-year horizon permits annual events without unbounded computation.
    for _ in range(366 * 24 * 60 + 1):
        if cron_match(expression, candidate, parsed):
            return candidate.isoformat(timespec='seconds')
        candidate += timedelta(minutes=1)
    raise AutomationError('Jadwal tidak memiliki waktu valid dalam 366 hari')


def validate_steps(steps: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        raise AutomationError('Workflow memerlukan 1-8 langkah')
    out = []
    for entry in steps:
        if not isinstance(entry, dict) or set(entry) != {'kind', 'message'}:
            raise AutomationError('Setiap langkah hanya menerima kind dan message')
        kind, message = entry['kind'], entry['message']
        if kind not in {'chat', 'orchestrate'} or not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000:
            raise AutomationError('Langkah workflow tidak valid')
        out.append({'kind': kind, 'message': message.strip()})
    return out


class AutomationService:
    def __init__(self, db: Store):
        self.db = db

    @staticmethod
    def _enqueue(db, sid: str, kind: str, message: str, key: str) -> str:
        if db.execute("SELECT 1 FROM tasks WHERE session_id=? AND status IN ('queued','running','approval_required','interrupted')", (sid,)).fetchone():
            raise AutomationError('Sesi masih mempunyai tugas aktif')
        if db.execute("SELECT 1 FROM approvals WHERE session_id=? AND state IN ('pending','executing','unknown')", (sid,)).fetchone():
            raise AutomationError('Sesi masih mempunyai approval aktif')
        if db.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0] >= 50:
            raise AutomationError('Antrean 50 tugas penuh')
        task_id, stamp = str(uuid.uuid4()), now()
        digest = hashlib.sha256((kind + '\0' + message).encode()).hexdigest()
        db.execute('INSERT INTO tasks(id,session_id,request_key,input_hash,status,created_at,updated_at,kind) VALUES(?,?,?,?,?,?,?,?)',
                   (task_id, sid, key, digest, 'queued', stamp, stamp, kind))
        db.execute('INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)', (sid, 'user', message, stamp))
        db.execute('UPDATE sessions SET updated_at=? WHERE id=?', (stamp, sid))
        db.execute('INSERT INTO task_events(task_id,event,details,created_at) VALUES(?,?,?,?)',
                   (task_id, 'automation_enqueued', '{}', stamp))
        return task_id

    def workflows(self):
        with self.db.connection() as db:
            return [dict(r) | {'steps': json.loads(r['steps_json'])} for r in db.execute('SELECT * FROM workflows ORDER BY created_at DESC')]

    def workflow(self, identity: str):
        with self.db.connection() as db:
            row = db.execute('SELECT * FROM workflows WHERE id=?', (identity,)).fetchone()
            return dict(row) | {'steps': json.loads(row['steps_json'])} if row else None

    def create_workflow(self, sid: str, name: str, steps: list[dict], enabled: bool = True):
        clean = validate_steps(steps)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise AutomationError('Nama workflow 1-80 karakter')
        ident, stamp = str(uuid.uuid4()), now()
        with self.db.connection() as db:
            db.execute('INSERT INTO workflows(id,session_id,name,steps_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                       (ident, sid, name.strip(), as_json(clean), int(enabled), stamp, stamp))
        self.db.audit('workflow_created', {'workflow': ident, 'steps': len(clean)})
        return self.workflow(ident)

    def set_workflow_enabled(self, identity: str, enabled: bool):
        with self.db.connection() as db:
            cur = db.execute('UPDATE workflows SET enabled=?,updated_at=? WHERE id=?', (int(enabled), now(), identity))
            if not cur.rowcount:
                raise LookupError('Workflow tidak ditemukan')
        self.db.audit('workflow_enabled', {'workflow': identity, 'enabled': enabled})
        return self.workflow(identity)

    def workflow_runs(self, workflow_id: str | None = None):
        with self.db.connection() as db:
            if workflow_id:
                rows = db.execute('SELECT * FROM workflow_runs WHERE workflow_id=? ORDER BY created_at DESC LIMIT 50', (workflow_id,))
            else:
                rows = db.execute('SELECT * FROM workflow_runs ORDER BY created_at DESC LIMIT 50')
            return [dict(r) for r in rows]

    def start_workflow(self, workflow_id: str, schedule_key: str | None = None):
        with self.db.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            flow = db.execute('SELECT * FROM workflows WHERE id=? AND enabled=1', (workflow_id,)).fetchone()
            if flow is None:
                raise AutomationError('Workflow tidak ditemukan atau dinonaktifkan')
            steps = json.loads(flow['steps_json'])
            if db.execute("SELECT 1 FROM workflow_runs WHERE workflow_id=? AND status IN ('running','waiting','paused')", (workflow_id,)).fetchone():
                raise AutomationError('Workflow memiliki run yang belum selesai')
            run_id = str(uuid.uuid4())
            key = schedule_key or ('flow.' + run_id)
            task_id = self._enqueue(db, flow['session_id'], steps[0]['kind'], steps[0]['message'], key)
            stamp = now()
            db.execute('INSERT INTO workflow_runs(id,workflow_id,session_id,status,step_index,last_task_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                       (run_id, workflow_id, flow['session_id'], 'running', 0, task_id, stamp, stamp))
        self.db.audit('workflow_started', {'run': run_id, 'workflow': workflow_id})
        return {'id': run_id, 'workflow_id': workflow_id, 'status': 'running', 'last_task_id': task_id}

    def reconcile_runs(self) -> int:
        """Progress each workflow only after verified task completion; no replay on uncertainty."""
        changed = []
        with self.db.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            runs = db.execute("SELECT * FROM workflow_runs WHERE status IN ('running','waiting','paused') ORDER BY created_at").fetchall()
            for run in runs:
                task = db.execute('SELECT * FROM tasks WHERE id=?', (run['last_task_id'],)).fetchone()
                if task is None:
                    continue
                status = task['status']
                if status in {'queued', 'running'}:
                    if run['status'] != 'running':
                        db.execute("UPDATE workflow_runs SET status='running',updated_at=? WHERE id=?", (now(), run['id']))
                    continue
                if status == 'approval_required':
                    db.execute("UPDATE workflow_runs SET status='waiting',updated_at=? WHERE id=?", (now(), run['id']))
                elif status == 'completed':
                    flow = db.execute('SELECT steps_json FROM workflows WHERE id=?', (run['workflow_id'],)).fetchone()
                    steps = json.loads(flow['steps_json'])
                    next_step = run['step_index'] + 1
                    if next_step >= len(steps):
                        db.execute("UPDATE workflow_runs SET status='completed',updated_at=?,finished_at=? WHERE id=?", (now(), now(), run['id']))
                        changed.append((run['id'], 'completed'))
                    else:
                        try:
                            new_task = self._enqueue(db, run['session_id'], steps[next_step]['kind'], steps[next_step]['message'],
                                                     f"flow.{run['id']}.{next_step}")
                        except AutomationError:
                            db.execute("UPDATE workflow_runs SET status='waiting',updated_at=? WHERE id=?", (now(), run['id']))
                        else:
                            db.execute("UPDATE workflow_runs SET status='running',step_index=?,last_task_id=?,updated_at=? WHERE id=?",
                                       (next_step, new_task, now(), run['id']))
                            changed.append((run['id'], 'advanced'))
                elif status in {'failed', 'interrupted', 'canceled', 'step_limit', 'budget_exceeded'}:
                    db.execute("UPDATE workflow_runs SET status='paused',updated_at=? WHERE id=?", (now(), run['id']))
                    changed.append((run['id'], 'paused'))
        for ident, status in changed:
            self.db.audit('workflow_transition', {'run': ident, 'status': status})
        return len(changed)

    def cancel_run(self, run_id: str):
        with self.db.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute('SELECT * FROM workflow_runs WHERE id=?', (run_id,)).fetchone()
            if not run:
                raise LookupError('Run tidak ditemukan')
            if run['status'] in {'completed', 'canceled'}:
                raise AutomationError('Run sudah selesai')
            db.execute("UPDATE workflow_runs SET status='canceled',updated_at=?,finished_at=? WHERE id=?", (now(), now(), run_id))
            task = db.execute('SELECT status FROM tasks WHERE id=?', (run['last_task_id'],)).fetchone()
            if task and task['status'] in {'queued','running','approval_required'}:
                if task['status'] != 'running':
                    db.execute("UPDATE tasks SET cancel_requested=1,status='canceled',updated_at=?,finished_at=? WHERE id=?",
                               (now(), now(), run['last_task_id']))
                else:
                    db.execute('UPDATE tasks SET cancel_requested=1,updated_at=? WHERE id=?', (now(),run['last_task_id']))
                for row in db.execute("SELECT * FROM approvals WHERE session_id=? AND state='pending'", (run['session_id'],)).fetchall():
                    db.execute("UPDATE approvals SET state='rejected',result='Workflow dibatalkan.',decided_at=? WHERE id=?", (now(), row['id']))
                    db.execute('INSERT INTO messages(session_id,role,content,tool_call_id,created_at) VALUES(?,?,?,?,?)',
                               (run['session_id'], 'tool', 'Workflow dibatalkan.',row['tool_call_id'],now()))
        self.db.audit('workflow_canceled', {'run': run_id})

    def schedules(self):
        with self.db.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM schedules ORDER BY created_at DESC')]

    def create_schedule(self, sid: str, name: str, cron: str, target: str, message: str = '', workflow_id: str | None = None, enabled: bool = False):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise AutomationError('Nama jadwal tidak valid')
        if target not in {'chat', 'orchestrate', 'workflow'}:
            raise AutomationError('Jenis target tidak valid')
        if target == 'workflow':
            if not workflow_id:
                raise AutomationError('Workflow wajib dipilih')
            flow = self.workflow(workflow_id)
            if not flow or flow['session_id'] != sid:
                raise AutomationError('Workflow tidak termasuk sesi ini')
        elif not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000:
            raise AutomationError('Pesan jadwal tidak valid')
        next_at = cron_next(cron, datetime.now(timezone.utc))
        ident, stamp = str(uuid.uuid4()), now()
        with self.db.connection() as db:
            db.execute('INSERT INTO schedules(id,session_id,name,cron,target,message,workflow_id,enabled,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, sid, name.strip(), cron.strip(), target, message.strip(), workflow_id, int(enabled), next_at, stamp, stamp))
        self.db.audit('schedule_created', {'schedule': ident, 'enabled': enabled})
        return self.schedule(ident)

    def schedule(self, identity: str):
        with self.db.connection() as db:
            row = db.execute('SELECT * FROM schedules WHERE id=?', (identity,)).fetchone()
            return dict(row) if row else None

    def set_schedule_enabled(self, identity: str, enabled: bool):
        with self.db.connection() as db:
            cur = db.execute('UPDATE schedules SET enabled=?,updated_at=? WHERE id=?', (int(enabled), now(), identity))
            if not cur.rowcount:
                raise LookupError('Jadwal tidak ditemukan')
        self.db.audit('schedule_enabled', {'schedule': identity, 'enabled': enabled})
        return self.schedule(identity)

    def tick(self, at: datetime | None = None) -> list[dict]:
        """Due schedules: at-most-once attempt per due slot, no catch-up floods."""
        at = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        stamp = at.isoformat(timespec='seconds')
        results = []
        with self.db.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            due = db.execute('SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at LIMIT 20', (stamp,)).fetchall()
            for schedule in due:
                original_due = schedule['next_run_at']
                # Advance from current UTC time, coalescing all missed windows.
                next_at = cron_next(schedule['cron'], at)
                task_id, error = None, None
                try:
                    if schedule['target'] == 'workflow':
                        # Workflow starts are handled outside the transaction below.
                        error = 'workflow_pending'
                    else:
                        task_id = self._enqueue(db, schedule['session_id'], schedule['target'], schedule['message'],
                                                'cron.' + schedule['id'] + '.' + original_due)
                except AutomationError as exc:
                    error = str(exc)
                db.execute('UPDATE schedules SET next_run_at=?,last_run_at=?,last_task_id=?,last_error=?,updated_at=? WHERE id=?',
                           (next_at, original_due, task_id, error, stamp, schedule['id']))
                results.append({'schedule': schedule['id'], 'task': task_id, 'error': error, 'target': schedule['target'],
                                'workflow_id': schedule['workflow_id'], 'due': original_due})
        # Keep workflow scheduling safe: failures are visible, and no automatic retries.
        for result in results:
            if result['error'] == 'workflow_pending':
                try:
                    run = self.start_workflow(result['workflow_id'], schedule_key='cron.' + result['schedule'] + '.' + result['due'])
                except AutomationError as exc:
                    result['error'] = str(exc)
                else:
                    result['task'] = run['last_task_id']
                    result['error'] = None
                with self.db.connection() as db:
                    db.execute('UPDATE schedules SET last_task_id=?,last_error=? WHERE id=?',
                               (result['task'], result['error'], result['schedule']))
            self.db.audit('schedule_triggered', {'schedule': result['schedule'], 'task': result['task'],
                                                 'status': 'skipped' if result['error'] else 'enqueued'})
        return results
