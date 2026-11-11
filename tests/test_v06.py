"""v0.6 production-hardening regression tests (no external API/SSH required)."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from hermas.access import AccessService
from hermas.config import Settings
from hermas.operations import OperationsService, OperationsError
from hermas.snapshots import WorkspaceSnapshots, SnapshotError
from hermas.store import Store
from hermas.vps import VPSService
from hermas.web import create_app


class V06Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        self.workspace=root/'workspace';self.workspace.mkdir()
        self.cfg=Settings(app_dir=Path(__file__).resolve().parents[1], workspace=self.workspace, data_dir=root/'data',
            admin_password='secure-password-long-enough',session_secret='k'*48,model_key='',
            model_url='https://api.openai.com/v1',model_name='demo',model_provider='demo',
            allowed_hosts=('testserver','localhost'),secure_cookie=False,docker_tests=False,
            max_steps=4,max_output_tokens=200,daily_token_budget=20000)
        self.db=Store(self.cfg.data_dir)
        self.vps=VPSService(self.db)
        self.ops=OperationsService(self.db,self.vps)
        self.accounts=AccessService(self.db)
        self.snapshots=WorkspaceSnapshots(self.db,self.workspace,self.cfg.session_secret)
        self.client=TestClient(create_app(self.cfg,self.db))
        response=self.client.post('/api/login',json={'password':self.cfg.admin_password})
        self.assertEqual(response.status_code,200)
        self.csrf={'X-Hermas-CSRF':response.json()['csrf']}

    def tearDown(self):
        self.client.close();self.temp.cleanup()

    def add_host(self, enabled=True):
        return self.vps.add_host(name='Staging 01',hostname='vps.example.org',ssh_user='deploy',enabled=enabled)

    def test_migration_and_legacy_sessions_preserved(self):
        self.assertEqual(self.db.connection().__class__.__name__,'_GeneratorContextManager')
        with self.db.connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],6)
        old=self.db.create_session()
        with self.db.connection() as con:con.execute('PRAGMA user_version=5')
        self.assertTrue(Store(self.cfg.data_dir).exists(old['id']))
        self.assertTrue(Store(self.cfg.data_dir).audit_verified())

    def test_accounts_hash_and_no_password_in_api(self):
        a=self.client.post('/api/accounts',json={'username':'op_user','password':'super strong password for account','role':'operator'},headers=self.csrf)
        self.assertEqual(a.status_code,201)
        self.assertNotIn('password_hash',a.json());self.assertNotIn('password',str(a.json()))
        with self.db.connection() as con:
            self.assertNotIn('super strong password',con.execute('SELECT password_hash FROM auth_users').fetchone()[0])
        self.assertIsNone(self.accounts.authenticate('op_user','invalid'))
        self.assertIsNotNone(self.accounts.authenticate('op_user','super strong password for account'))

    def test_viewer_and_operator_cannot_access_admin_api(self):
        self.accounts.create('viewonly','correct horse battery staple','viewer')
        guest=TestClient(create_app(self.cfg,self.db))
        login=guest.post('/api/login',json={'username':'viewonly','password':'correct horse battery staple'})
        self.assertEqual(login.status_code,200)
        token={'X-Hermas-CSRF':login.json()['csrf']}
        self.assertEqual(guest.get('/api/me').json()['role'],'viewer')
        self.assertEqual(guest.get('/api/operations/summary').status_code,200)
        self.assertEqual(guest.get('/api/sessions').status_code,403)
        self.assertEqual(guest.get('/api/memory').status_code,403)
        self.assertEqual(guest.get('/api/accounts').status_code,403)
        self.assertEqual(guest.post('/api/accounts',json={'username':'badguy','password':'xxxxxxxxxxxxxxxx','role':'operator'},headers=token).status_code,403)
        self.assertEqual(guest.get('/api/vps/hosts').status_code,200)
        self.assertEqual(guest.post('/api/logout',json={},headers=token).status_code,200)
        guest.close()

    def test_operator_can_ack_only_and_no_escalation(self):
        host=self.add_host()
        alert=self.ops.report_status(host['id'],'health','review')
        self.accounts.create('opsuser','correct horse battery staple','operator')
        guest=TestClient(create_app(self.cfg,self.db))
        login=guest.post('/api/login',json={'username':'opsuser','password':'correct horse battery staple'})
        token={'X-Hermas-CSRF':login.json()['csrf']}
        self.assertEqual(guest.post('/api/operations/alerts/'+alert['id']+'/acknowledge',json={},headers=token).status_code,200)
        self.assertEqual(guest.post('/api/snapshots',json={},headers=token).status_code,403)
        self.assertEqual(guest.post('/api/vps/hosts/'+host['id']+'/enabled',json={'enabled':False},headers=token).status_code,403)
        guest.close()

    def test_revoked_session_is_rejected(self):
        account=self.accounts.create('seconduser','correct horse battery staple','viewer')
        guest=TestClient(create_app(self.cfg,self.db))
        guest.post('/api/login',json={'username':'seconduser','password':'correct horse battery staple'})
        self.assertEqual(guest.get('/api/operations/summary').status_code,200)
        self.accounts.change(account['id'],enabled=False)
        self.assertEqual(guest.get('/api/operations/summary').status_code,401)
        guest.close()

    def test_monitor_defaults_to_disabled_and_cannot_enable_without_optin(self):
        host=self.add_host()
        monitor=self.ops.create_monitor(host['id'],'health',15)
        self.assertEqual(monitor['enabled'],0)
        with self.assertRaises(OperationsError):self.ops.set_enabled(monitor['id'],True)
        self.assertIsNone(self.ops.execute_one())
        self.assertEqual(self.ops.monitors()[0]['last_run_at'],None)

    def test_monitor_restricts_root_interval_and_status(self):
        host=self.add_host()
        for interval in [0,4,1441]:
            with self.assertRaises(OperationsError):self.ops.create_monitor(host['id'],'health',interval)
        with self.assertRaises(Exception):self.ops.create_monitor(host['id'],'wordpress',15,'/etc')
        with self.assertRaises(OperationsError):self.ops.create_monitor(host['id'],'health',10,'/var/www/test')

    def test_monitor_one_due_claim_even_with_two_instances(self):
        host=self.add_host()
        ops=OperationsService(self.db,self.vps,enabled=True)
        self.vps.enabled=True
        monitor=ops.create_monitor(host['id'],'health',5)
        with patch.object(self.vps,'_ssh_ready'):
            ops.set_enabled(monitor['id'],True)
        first=ops.claim_due(datetime.now(timezone.utc)+timedelta(minutes=1))
        self.assertIsNotNone(first)
        second=ops.claim_due(datetime.now(timezone.utc)+timedelta(minutes=1))
        self.assertIsNone(second)
        ops.finish(first,'ok')
        self.assertEqual(ops.monitor(monitor['id'])['last_status'],'ok')

    def test_monitor_works_with_mock_probe_and_records_alert(self):
        host=self.add_host();ops=OperationsService(self.db,self.vps,enabled=True);self.vps.enabled=True
        monitor=ops.create_monitor(host['id'],'health',10)
        with patch.object(self.vps,'_ssh_ready'):
            ops.set_enabled(monitor['id'],True)
        with patch.object(self.vps,'check',return_value={'status':'review','id':'check-demo'}):
            result=ops.execute_one(datetime.now(timezone.utc)+timedelta(seconds=30))
        self.assertEqual(result['status'],'review')
        self.assertEqual(len(ops.alerts()),1)
        self.assertEqual(ops.monitor(monitor['id'])['last_check_id'],'check-demo')

    def test_deduplicated_alert_and_resolution(self):
        self.ops.notify_enabled=True
        host=self.add_host();a=self.ops.report_status(host['id'],'health','review')
        same=self.ops.report_status(host['id'],'health','review')
        self.assertEqual(a['id'],same['id']);self.assertEqual(same['count'],2)
        self.assertEqual(len(self.ops.deliveries()),1)
        self.ops.report_status(host['id'],'health','ok')
        self.assertEqual(self.ops.alert(a['id'])['status'],'resolved')
        self.assertEqual(self.ops.deliveries()[0]['status'],'canceled')
        again=self.ops.report_status(host['id'],'health','error')
        self.assertEqual(again['status'],'open')
        self.assertEqual(again['severity'],'critical')
        self.assertEqual(len(self.ops.deliveries()),2)

    def test_alert_notification_is_optin_and_not_retroactive(self):
        host=self.add_host()
        self.ops.report_status(host['id'],'health','error')
        self.assertEqual(self.ops.deliveries(),[])
        self.ops.notify_enabled=True
        # An existing open alert is deduplicated; no surprise backfill on enable.
        self.ops.report_status(host['id'],'health','review')
        self.assertEqual(self.ops.deliveries(),[])

    def test_interrupted_monitors_recovery(self):
        host=self.add_host();self.vps.enabled=True
        ops=OperationsService(self.db,self.vps,enabled=True)
        mon=ops.create_monitor(host['id'],'health',10)
        with patch.object(self.vps,'_ssh_ready'):
            ops.set_enabled(mon['id'],True)
        ops.claim_due(datetime.now(timezone.utc)+timedelta(seconds=10))
        self.assertEqual(ops.monitor(mon['id'])['last_status'],'running')
        ops.recover_monitors()
        self.assertEqual(ops.monitor(mon['id'])['last_status'],'interrupted')

    def test_alert_ack_skips_outgoing(self):
        host=self.add_host();a=self.ops.report_status(host['id'],'wordpress','review')
        self.ops.acknowledge(a['id'])
        self.assertIsNone(self.ops.claim_delivery())
        self.assertEqual(self.ops.alert(a['id'])['status'],'acknowledged')
        self.ops.report_status(host['id'],'wordpress','error')
        self.assertEqual(self.ops.alert(a['id'])['status'],'acknowledged')

    def test_delivery_after_crash_is_unknown_not_retried(self):
        self.ops.notify_enabled=True
        host=self.add_host();self.ops.report_status(host['id'],'health','error')
        claimed=self.ops.claim_delivery()
        self.assertIsNotNone(claimed)
        self.ops.recover_deliveries()
        self.assertIsNone(self.ops.claim_delivery())
        self.assertEqual(self.ops.deliveries()[0]['status'],'unknown')

    def test_local_snapshot_restore_preview(self):
        file=self.workspace/'index.php'
        file.write_text('<?php echo "A";')
        record=self.snapshots.create();file.write_text('<?php echo "B";')
        plan=self.snapshots.preview(record['id'])
        self.assertEqual(plan['changes'][0]['action'],'overwrite')
        with self.assertRaises(SnapshotError):self.snapshots.restore(record['id'],plan['token'],'no')
        result=self.snapshots.restore(record['id'],plan['token'],'RESTORE')
        self.assertEqual(result['restored_files'],1)
        self.assertEqual(file.read_text(),'<?php echo "A";')
        self.assertTrue(self.db.audit_verified())

    def test_restore_stale_token_denied(self):
        file=self.workspace/'abc.py';file.write_text('hello')
        record=self.snapshots.create();plan=self.snapshots.preview(record['id'])
        file.write_text('new content')
        with self.assertRaisesRegex(SnapshotError,'Workspace berubah'):
            self.snapshots.restore(record['id'],plan['token'],'RESTORE')
        self.assertEqual(file.read_text(),'new content')

    def test_snapshot_excludes_secrets_and_symlinks(self):
        (self.workspace/'hello.php').write_text('<?php echo 1;')
        (self.workspace/'.env').write_text('SECRET=123')
        (self.workspace/'wp-config.php').write_text('<?php $password="x";')
        (self.workspace/'sub').mkdir()
        (self.workspace/'sub'/'token.json').write_text('secret')
        (self.workspace/'sub'/'ok.js').write_text('let x=1')
        try:(self.workspace/'link.php').symlink_to(self.workspace/'hello.php')
        except (OSError,NotImplementedError):pass
        snapshot=self.snapshots.create()
        with self.db.connection() as con:
            import json
            manifest=json.loads(con.execute('SELECT manifest_json FROM workspace_snapshots WHERE id=?',(snapshot['id'],)).fetchone()[0])
        self.assertEqual(set(manifest),{'hello.php','sub/ok.js'})

    def test_snapshot_tamper_detected(self):
        (self.workspace/'file.md').write_text('alpha')
        record=self.snapshots.create()
        archive=self.db.directory/'snapshots'/(record['id']+'.zip')
        archive.write_bytes(b'corrupted')
        with self.assertRaisesRegex(SnapshotError,'Checksum'):
            self.snapshots.preview(record['id'])

    def test_snapshot_endpoints_require_admin_csrf(self):
        (self.workspace/'file.php').write_text('<?php echo "ok";')
        self.assertEqual(self.client.post('/api/snapshots',json={}).status_code,403)
        result=self.client.post('/api/snapshots',json={},headers=self.csrf)
        self.assertEqual(result.status_code,201)
        preview=self.client.get('/api/snapshots/'+result.json()['id']+'/preview')
        self.assertEqual(preview.status_code,200)
        restore=self.client.post('/api/snapshots/'+result.json()['id']+'/restore',
          json={'token':preview.json()['token'],'confirmation':'RESTORE'},headers=self.csrf)
        self.assertEqual(restore.status_code,200)

    def test_operations_api_role_and_monitor_readonly(self):
        host=self.add_host()
        self.assertEqual(self.client.post('/api/operations/monitors',json={'host_id':host['id'],'kind':'health','interval_minutes':5}).status_code,403)
        added=self.client.post('/api/operations/monitors',json={'host_id':host['id'],'kind':'health','interval_minutes':5},headers=self.csrf)
        self.assertEqual(added.status_code,201)
        self.assertEqual(added.json()['enabled'],0)
        self.assertEqual(self.client.post('/api/operations/monitors/'+added.json()['id']+'/enabled',json={'enabled':True},headers=self.csrf).status_code,409)
        self.assertEqual(self.client.get('/api/operations/monitors').status_code,200)
        self.assertEqual(self.client.get('/api/operations/summary').json()['active_monitors'],0)
