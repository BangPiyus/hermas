"""v0.4 offline security, migration, workflow, scheduler, registry and API tests."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from hermas.agent import Agent
from hermas.automation import AutomationService, AutomationError, cron_match, cron_next, parse_cron
from hermas.browser import validate_url, BrowserError
from hermas.config import Settings
from hermas.integrations import IntegrationRegistry, IntegrationError
from hermas.jobs import TaskWorker
from hermas.store import Store
from hermas.telegram_gateway import TelegramGateway
from hermas.web import create_app


class TestV04(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        space = root/'workspace'
        space.mkdir()
        (space/'file.php').write_text('<?php echo "ok";')
        self.cfg = Settings(app_dir=Path(__file__).resolve().parents[1],
                            workspace=space, data_dir=root/'data', admin_password='long-enough-password',
                            session_secret='a'*50,model_key='',model_url='https://api.openai.com/v1',
                            model_name='demo',model_provider='demo',allowed_hosts=('testserver','localhost'),
                            secure_cookie=False,docker_tests=False,max_steps=5,max_output_tokens=300,
                            daily_token_budget=20000)
        self.db = Store(self.cfg.data_dir)
        self.automation = AutomationService(self.db)
        self.worker = TaskWorker(self.db,Agent(self.cfg,self.db))
        self.client=TestClient(create_app(self.cfg,self.db))
        self.csrf=self.client.post('/api/login',json={'password':self.cfg.admin_password}).json()['csrf']
        self.sid=self.client.post('/api/sessions',json={},headers=self.auth).json()['id']

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    @property
    def auth(self):
        return {'X-Hermas-CSRF':self.csrf}

    def test_migrate_version_four_preserves_sessions(self):
        self.assertTrue(self.db.exists(self.sid))
        again=Store(self.cfg.data_dir)
        self.assertTrue(again.exists(self.sid))
        with again.connection() as con:
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0],6)
        self.assertTrue(again.audit_verified())

    def test_cron_utc_and_day_rule(self):
        monday=datetime(2026,11,9,10,0,tzinfo=timezone.utc)
        self.assertTrue(cron_match('0 10 * * 1',monday))
        self.assertFalse(cron_match('0 10 * * 0',monday))
        self.assertEqual(cron_next('*/15 * * * *',monday),'2026-11-09T10:15:00+00:00')
        self.assertTrue(cron_match('0 10 9 * 0',monday))  # DOM/DOW OR
        self.assertEqual(len(parse_cron('0-10/5 10 * * *')[0]),3)
        for bad in ('0 0 * *','x 0 * * *','61 * * * *','*/0 * * * *'):
            with self.assertRaises(AutomationError):
                parse_cron(bad)

    def test_workflow_steps_advance_and_finish(self):
        flow=self.automation.create_workflow(self.sid,'Review PHP',[{'kind':'chat','message':'/list'},
                              {'kind':'chat','message':'/read file.php'}])
        run=self.automation.start_workflow(flow['id'])
        self.assertEqual(run['status'],'running')
        self.assertEqual(asyncio.run(self.worker.process_next())['status'],'completed')
        runs=self.automation.workflow_runs(flow['id'])
        self.assertEqual(runs[0]['step_index'],1)
        self.assertEqual(asyncio.run(self.worker.process_next())['status'],'completed')
        self.assertEqual(self.automation.workflow_runs(flow['id'])[0]['status'],'completed')
        self.assertTrue(self.db.audit_verified())

    def test_workflow_approval_and_cancellation(self):
        flow=self.automation.create_workflow(self.sid,'Edit',[{'kind':'chat','message':'/write file.php :: <?php echo "changed";'},
                              {'kind':'chat','message':'/list'}])
        run=self.automation.start_workflow(flow['id'])
        self.assertEqual(asyncio.run(self.worker.process_next())['status'],'approval_required')
        self.assertEqual(self.automation.workflow_runs(flow['id'])[0]['status'],'waiting')
        pending=self.db.pending(self.sid)
        self.assertEqual(len(pending),1)
        self.automation.cancel_run(run['id'])
        self.assertEqual(self.automation.workflow_runs(flow['id'])[0]['status'],'canceled')
        self.assertEqual(self.db.approval(pending[0]['id'])['state'],'rejected')
        self.assertEqual(self.db.task(run['last_task_id'])['status'],'canceled')
        self.assertIn('ok',(self.cfg.workspace/'file.php').read_text())

    def test_workflow_resumes_after_approved_file_edit(self):
        flow=self.automation.create_workflow(self.sid,'Approval',[{'kind':'chat','message':'/write file.php :: <?php echo "new";'},
                                  {'kind':'chat','message':'/list'}])
        run=self.automation.start_workflow(flow['id'])
        self.assertEqual(asyncio.run(self.worker.process_next())['status'],'approval_required')
        aid=self.db.pending(self.sid)[0]['id']
        response=self.client.post('/api/approvals/'+aid+'/decision',json={'approved':True},headers=self.auth)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['status'],'queued')
        self.assertEqual(asyncio.run(self.worker.process_next())['status'],'completed')
        self.assertEqual(self.automation.workflow_runs(flow['id'])[0]['step_index'],1)
        self.assertEqual(asyncio.run(self.worker.process_next())['status'],'completed')
        self.assertEqual(self.automation.workflow_runs(flow['id'])[0]['status'],'completed')
        self.assertIn('new',(self.cfg.workspace/'file.php').read_text())

    def test_workflow_manual_resume_after_interruption(self):
        flow=self.automation.create_workflow(self.sid,'Resume',[{'kind':'chat','message':'/list'}])
        run=self.automation.start_workflow(flow['id'])
        self.db.claim_next_task()
        self.db.recover_orphan_tasks()
        self.automation.reconcile_runs()
        self.assertEqual(self.automation.workflow_runs(flow['id'])[0]['status'],'paused')
        self.db.resume_task(run['last_task_id'])
        self.automation.reconcile_runs()
        self.assertEqual(self.automation.workflow_runs(flow['id'])[0]['status'],'running')
        asyncio.run(self.worker.process_next())
        self.assertEqual(self.automation.workflow_runs(flow['id'])[0]['status'],'completed')

    def test_workflow_validation_and_active_guard(self):
        with self.assertRaises(AutomationError):
            self.automation.create_workflow(self.sid,'Empty',[])
        with self.assertRaises(AutomationError):
            self.automation.create_workflow(self.sid,'Bad',[{'kind':'shell','message':'rm -rf /'}])
        flow=self.automation.create_workflow(self.sid,'Busy',[{'kind':'chat','message':'/list'}])
        self.automation.start_workflow(flow['id'])
        with self.assertRaises(AutomationError):
            self.automation.start_workflow(flow['id'])
        self.assertEqual(len(self.automation.workflow_runs(flow['id'])),1)

    def test_schedule_coalesces_missed_windows_and_no_duplicate(self):
        sch=self.automation.create_schedule(self.sid,'Frequent','* * * * *','chat','/list',enabled=True)
        with self.db.connection() as con:
            con.execute('UPDATE schedules SET next_run_at=? WHERE id=?',('2026-10-01T00:00:00+00:00',sch['id']))
        now_time=datetime(2026,11,9,11,0,tzinfo=timezone.utc)
        results=self.automation.tick(now_time)
        self.assertEqual(len(results),1)
        self.assertIsNotNone(results[0]['task'])
        self.assertEqual(self.automation.tick(now_time),[])
        self.assertEqual(len(self.db.tasks()),1)
        self.assertEqual(self.automation.schedule(sch['id'])['next_run_at'],'2026-11-09T11:01:00+00:00')
        self.assertEqual(self.automation.schedule(sch['id'])['last_run_at'],'2026-10-01T00:00:00+00:00')

    def test_schedule_skips_busy_session(self):
        sch=self.automation.create_schedule(self.sid,'Daily','* * * * *','chat','/list',enabled=True)
        self.db.enqueue_task(self.sid,'/list')
        with self.db.connection() as con:
            con.execute('UPDATE schedules SET next_run_at=? WHERE id=?',('2026-01-01T00:00:00+00:00',sch['id']))
        result=self.automation.tick()[0]
        self.assertIsNone(result['task'])
        self.assertIn('aktif',result['error'])
        self.assertEqual(len(self.db.tasks()),1)

    def test_schedule_disabled_by_default_and_workflow_target(self):
        f=self.automation.create_workflow(self.sid,'Simple',[{'kind':'chat','message':'/list'}])
        sch=self.automation.create_schedule(self.sid,'Flow','* * * * *','workflow',workflow_id=f['id'])
        self.assertEqual(sch['enabled'],0)
        self.assertEqual(self.automation.tick(datetime(2027,1,1,tzinfo=timezone.utc)),[])
        self.automation.set_schedule_enabled(sch['id'],True)
        result=self.automation.tick(datetime(2027,1,1,tzinfo=timezone.utc))[0]
        self.assertIsNotNone(result['task'])
        self.assertEqual(len(self.automation.workflow_runs(f['id'])),1)

    def test_manifest_registration_never_executes(self):
        registry=IntegrationRegistry(self.db)
        manifest={'name':'sample_mcp','version':'1.0.0','description':'Metadata only',
                  'transport':'mcp-stdio','tools':[{'name':'read_items','description':'Read items','read_only':True}]}
        item=registry.register(manifest)
        self.assertEqual(item['enabled'],0)
        registry.toggle(item['id'],True)
        self.assertEqual(registry.list()[0]['enabled'],1)
        for invalid in ({**manifest,'command':'danger'}, {**manifest,'name':'../../foo'},
                        {**manifest,'tools':[{'name':'read_items','description':'x','read_only':'true'}]}):
            with self.assertRaises(IntegrationError):
                registry.register(invalid)

    def test_browser_url_allowlist_and_private_ips(self):
        with self.assertRaises(BrowserError):
            validate_url('file:///etc/passwd',('example.org',))
        with self.assertRaises(BrowserError):
            validate_url('https://example.org.evil.com',('example.org',))
        with self.assertRaises(BrowserError):
            validate_url('http://example.org:8080/',('example.org',))
        with patch('hermas.browser.socket.getaddrinfo', return_value=[(2,1,6,'',('127.0.0.1',80))]):
            with self.assertRaises(BrowserError):
                validate_url('https://example.org',('example.org',))
        with patch('hermas.browser.socket.getaddrinfo', return_value=[(2,1,6,'',('93.184.215.14',443))]):
            validate_url('https://example.org/',('example.org',))

    def test_telegram_authorized_commands_and_replay_offset(self):
        telegram=TelegramGateway(self.db,self.automation,'fake-token',(12345,),allow_run=False)
        self.assertIsNone(telegram.handle_message(99999,'/status'))
        self.assertIn('Hermas aktif',telegram.handle_message(12345,'/status'))
        self.assertNotIn('/run',telegram.handle_message(12345,'/help'))
        telegram.set_offset(42)
        self.assertEqual(TelegramGateway(self.db,self.automation,'token',(12345,)).get_offset(),42)
        self.assertTrue(self.db.audit_verified())

    def test_telegram_poll_persists_offset_before_reply(self):
        import httpx
        telegram=TelegramGateway(self.db,self.automation,'fake-token',(123,),allow_run=False)
        requests=[]
        def handler(request):
            requests.append(request.url.path)
            if request.url.path.endswith('/getUpdates'):
                return httpx.Response(200,json={'ok':True,'result':[{'update_id':7,'message':{'chat':{'id':123},'text':'/status'}}]})
            return httpx.Response(200,json={'ok':True,'result':{}})
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await telegram.poll_once(client)
        asyncio.run(run())
        self.assertEqual(telegram.get_offset(),8)
        self.assertEqual(len(requests),2)
        self.assertTrue(requests[0].endswith('/getUpdates'))
        self.assertTrue(requests[1].endswith('/sendMessage'))

    def test_api_permissions_validations_and_schedule_toggle(self):
        data={'session_id':self.sid,'name':'Demo','steps':[{'kind':'chat','message':'/list'}]}
        self.assertEqual(self.client.post('/api/workflows',json=data).status_code,403)
        flow=self.client.post('/api/workflows',json=data,headers=self.auth)
        self.assertEqual(flow.status_code,201)
        schedule=self.client.post('/api/schedules',json={'session_id':self.sid,'name':'Hourly',
                                 'cron':'0 * * * *','target':'workflow','workflow_id':flow.json()['id']},headers=self.auth)
        self.assertEqual(schedule.status_code,201)
        self.assertFalse(schedule.json()['enabled'])
        self.assertEqual(self.client.post('/api/schedules/'+schedule.json()['id']+'/enabled',
                            json={'enabled':True},headers=self.auth).status_code,200)
        self.assertEqual(self.client.get('/api/schedules').status_code,200)
        run=self.client.post('/api/workflows/'+flow.json()['id']+'/run',headers=self.auth,json={})
        self.assertEqual(run.status_code,202)
        self.assertEqual(self.client.post('/api/browser/preview',json={'url':'https://example.org'},headers=self.auth).status_code,403)
        self.assertEqual(self.client.get('/api/me').json()['version'],'0.6.0')
        self.assertEqual(self.client.get('/api/health').status_code,404)


if __name__=='__main__':
    unittest.main()
