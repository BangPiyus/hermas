"""Opt-in read-only VPS scheduler, deduplicated alerts, and safe Telegram delivery.

No remote shell writes, no autonomous repair, no raw stderr in alerts.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

import httpx

from .store import Store, now
from .vps import VPSService, VPSException, validate_root


class OperationsError(ValueError):
    pass


class OperationsService:
    def __init__(self, db: Store, vps: VPSService, enabled: bool = False, notify_enabled: bool = False):
        self.db, self.vps, self.enabled, self.notify_enabled = db, vps, enabled, notify_enabled

    def monitors(self):
        with self.db.connection() as con:
            return [dict(row) for row in con.execute('SELECT * FROM vps_monitors ORDER BY created_at DESC')]

    def create_monitor(self, host_id: str, kind: str, interval_minutes: int, root: str = ''):
        host=self.vps.host(host_id)
        if host['retired']:raise OperationsError('VPS diarsipkan')
        if kind not in {'health','wordpress'}:raise OperationsError('Jenis monitor tidak valid')
        if type(interval_minutes) is not int or not 5<=interval_minutes<=1440:
            raise OperationsError('Interval harus 5–1440 menit')
        if kind=='wordpress':validate_root(root)
        else:
            if root:raise OperationsError('Health check tidak menerima root')
        identity=str(uuid.uuid4()); stamp=now()
        with self.db.connection() as con:
            con.execute('INSERT INTO vps_monitors(id,host_id,kind,root,interval_minutes,enabled,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,0,?,?,?)',
                        (identity,host_id,kind,root,interval_minutes,stamp,stamp,stamp))
        self.db.audit('monitor_created',{'monitor_id':identity,'host_id':host_id,'kind':kind})
        return self.monitor(identity)

    def monitor(self, identity: str):
        with self.db.connection() as con:
            row=con.execute('SELECT * FROM vps_monitors WHERE id=?',(identity,)).fetchone()
        if not row:raise LookupError('Monitor tidak ditemukan')
        return dict(row)

    def set_enabled(self, identity: str, enabled: bool):
        monitor=self.monitor(identity)
        if enabled:
            host=self.vps.host(monitor['host_id'])
            if not self.enabled or not self.vps.enabled:
                raise OperationsError('Monitoring remote harus diaktifkan eksplisit di .env')
            if not host['enabled'] or host['retired']:
                raise OperationsError('VPS belum diaktifkan')
            try:self.vps._ssh_ready()
            except VPSException as exc:raise OperationsError(str(exc)) from exc
        with self.db.connection() as con:
            con.execute('UPDATE vps_monitors SET enabled=?,next_run_at=?,updated_at=? WHERE id=?',
                        (int(enabled),now(),now(),identity))
        self.db.audit('monitor_enabled',{'monitor_id':identity,'enabled':bool(enabled)})
        return self.monitor(identity)

    def claim_due(self, at: datetime | None = None):
        if not self.enabled or not self.vps.enabled:
            return None
        at=(at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        stamp=at.isoformat(timespec='seconds')
        with self.db.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute('''SELECT m.* FROM vps_monitors m JOIN vps_hosts h ON h.id=m.host_id
                WHERE m.enabled=1 AND h.enabled=1 AND h.retired=0 AND m.next_run_at<=?
                ORDER BY m.next_run_at,m.id LIMIT 1''',(stamp,)).fetchone()
            if not row:return None
            record=dict(row)
            following=(at+timedelta(minutes=record['interval_minutes'])).isoformat(timespec='seconds')
            con.execute('UPDATE vps_monitors SET next_run_at=?,last_run_at=?,last_status=?,updated_at=? WHERE id=?',
                        (following,stamp,'running',stamp,record['id']))
        self.db.audit('monitor_claimed',{'monitor_id':record['id']})
        return record

    def finish(self, record: dict, status: str, check_id: str | None = None):
        if status not in {'ok','review','error'}:raise OperationsError('Status tidak valid')
        with self.db.connection() as con:
            con.execute('UPDATE vps_monitors SET last_status=?,last_check_id=?,updated_at=? WHERE id=?',
                        (status,check_id,now(),record['id']))
        self.db.audit('monitor_finished',{'monitor_id':record['id'],'status':status})
        self.report_status(record['host_id'],record['kind'],status,check_id)

    def recover_monitors(self):
        # A restarted process must not claim an incomplete remote check as success.
        with self.db.connection() as con:
            con.execute("UPDATE vps_monitors SET last_status='interrupted',updated_at=? WHERE last_status='running'",(now(),))

    def execute_one(self, at: datetime | None = None):
        record=self.claim_due(at)
        if record is None:return None
        try:
            result=self.vps.check(record['host_id'],record['kind'],record['root'])
            status=result['status']; check_id=result['id']
        except Exception:  # Redacted: no SSH stderr, host secrets or remote trace.
            status='error';check_id=None
        self.finish(record,status,check_id)
        return {'monitor_id':record['id'],'status':status,'check_id':check_id}

    def report_status(self, host_id: str, kind: str, status: str, check_id: str | None = None):
        if kind not in {'health','wordpress'} or status not in {'ok','review','error'}:
            raise OperationsError('Status laporan tidak valid')
        fingerprint=f'{host_id}:{kind}'
        with self.db.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            existing=con.execute('SELECT * FROM op_alerts WHERE fingerprint=?',(fingerprint,)).fetchone()
            stamp=now()
            if status=='ok':
                if existing and existing['status']!='resolved':
                    con.execute("UPDATE op_alerts SET status='resolved',last_seen_at=?,updated_at=? WHERE id=?",(stamp,stamp,existing['id']))
                con.execute("UPDATE alert_deliveries SET status='canceled',updated_at=? WHERE alert_id=? AND status='pending'",(stamp,existing['id'])) if existing else None
                return None
            severity='critical' if status=='error' else 'warning'
            if existing:
                alert_id=existing['id']
                new_status=existing['status'] if existing['status'] in {'open','acknowledged'} else 'open'
                con.execute('UPDATE op_alerts SET status=?,severity=?,last_seen_at=?,count=count+1,updated_at=? WHERE id=?',
                            (new_status,severity,stamp,stamp,alert_id))
                should_queue=existing['status']=='resolved'
            else:
                alert_id=str(uuid.uuid4()); should_queue=True
                con.execute('INSERT INTO op_alerts(id,fingerprint,host_id,kind,severity,status,count,first_seen_at,last_seen_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?,?)',
                            (alert_id,fingerprint,host_id,kind,severity,'open',stamp,stamp,stamp))
            if should_queue and self.notify_enabled:
                con.execute('INSERT INTO alert_deliveries(id,alert_id,status,created_at,updated_at) VALUES(?,?,?,?,?)',
                            (str(uuid.uuid4()),alert_id,'pending',stamp,stamp))
        self.db.audit('operations_alert',{'host_id':host_id,'kind':kind,'severity':severity,'alert_id':alert_id})
        return self.alert(alert_id)

    def alerts(self, limit=100):
        with self.db.connection() as con:
            return [dict(row) for row in con.execute('SELECT * FROM op_alerts ORDER BY updated_at DESC LIMIT ?', (min(200,max(1,int(limit))),))]

    def alert(self, identity):
        with self.db.connection() as con:
            row=con.execute('SELECT * FROM op_alerts WHERE id=?',(identity,)).fetchone()
        if not row:raise LookupError('Alert tidak ditemukan')
        return dict(row)

    def acknowledge(self, identity: str):
        alert=self.alert(identity)
        if alert['status']=='resolved':raise OperationsError('Alert sudah resolved')
        with self.db.connection() as con:
            con.execute("UPDATE op_alerts SET status='acknowledged',updated_at=? WHERE id=?",(now(),identity))
            con.execute("UPDATE alert_deliveries SET status='canceled',updated_at=? WHERE alert_id=? AND status='pending'",(now(),identity))
        self.db.audit('alert_acknowledged',{'alert_id':identity})
        return self.alert(identity)

    def claim_delivery(self):
        with self.db.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute('''SELECT d.*,a.host_id,a.kind,a.severity FROM alert_deliveries d
                JOIN op_alerts a ON a.id=d.alert_id WHERE d.status='pending' AND a.status='open'
                ORDER BY d.created_at LIMIT 1''').fetchone()
            if not row:return None
            con.execute("UPDATE alert_deliveries SET status='sending',updated_at=? WHERE id=?",(now(),row['id']))
            return dict(row)

    def finish_delivery(self, identity: str, delivered: bool):
        # Failure is unknown (Telegram may have accepted request). Never auto-resend.
        with self.db.connection() as con:
            con.execute('UPDATE alert_deliveries SET status=?,updated_at=? WHERE id=? AND status=\'sending\'',
                        ('sent' if delivered else 'unknown',now(),identity))
        self.db.audit('alert_delivery_result',{'delivery_id':identity,'delivered':bool(delivered)})

    def recover_deliveries(self):
        with self.db.connection() as con:
            con.execute("UPDATE alert_deliveries SET status='unknown',updated_at=? WHERE status='sending'",(now(),))

    def deliveries(self):
        with self.db.connection() as con:
            return [dict(row) for row in con.execute('SELECT id,alert_id,status,created_at,updated_at FROM alert_deliveries ORDER BY created_at DESC LIMIT 100')]

    def summary(self):
        with self.db.connection() as con:
            monitors=con.execute('SELECT COUNT(*) FROM vps_monitors WHERE enabled=1').fetchone()[0]
            alerts={row['status']:row['n'] for row in con.execute('SELECT status,COUNT(*) n FROM op_alerts GROUP BY status')}
            last=[dict(r) for r in con.execute('SELECT m.id,m.host_id,m.kind,m.last_run_at,m.last_status,m.next_run_at FROM vps_monitors m ORDER BY m.updated_at DESC LIMIT 8')]
            snapshots=con.execute('SELECT COUNT(*) FROM workspace_snapshots').fetchone()[0]
        return {'active_monitors':monitors,'alerts':alerts,'recent_monitors':last,'snapshots':snapshots,'audit_verified':self.db.audit_verified()}

    async def deliver_one(self, token: str, chat_ids: tuple[int,...]):
        if not self.notify_enabled or not token or not chat_ids:return None
        delivery=self.claim_delivery()
        if delivery is None:return None
        # No hostnames/IPs, paths, file contents, or remote stderr leave the system.
        message=f"Hermas v0.6 · {delivery['severity'].upper()} · pemeriksaan {delivery['kind']} butuh perhatian. Buka dashboard Hermas untuk detail."
        try:
            async with httpx.AsyncClient(trust_env=False,follow_redirects=False) as client:
                for chat in chat_ids:
                    response=await client.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':message},timeout=8)
                    response.raise_for_status()
                    if not response.json().get('ok'):raise ValueError('Telegram API gagal')
        except (httpx.HTTPError, ValueError):
            self.finish_delivery(delivery['id'],False)
            return {'status':'unknown','delivery_id':delivery['id']}
        self.finish_delivery(delivery['id'],True)
        return {'status':'sent','delivery_id':delivery['id']}

    async def run_forever(self, telegram_token: str = '', telegram_chat_ids: tuple[int,...] = (), notify: bool = False):
        self.recover_deliveries()
        self.recover_monitors()
        while True:
            try:
                if self.enabled and self.vps.enabled:
                    await asyncio.to_thread(self.execute_one)
                if notify:
                    await self.deliver_one(telegram_token, telegram_chat_ids)
            except Exception as exc:
                # Preserve service availability, audit only the exception class.
                self.db.audit('operations_worker_error',{'exception_type':type(exc).__name__})
            await asyncio.sleep(30)
