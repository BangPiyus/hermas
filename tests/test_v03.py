"""Hermas v0.3 tests: multi-agent, verified memory, safe Git workflow."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from hermas.agent import Agent, ModelError
from hermas.config import Settings
from hermas.git_studio import diff as git_diff, status as git_status
from hermas.jobs import TaskWorker
from hermas.store import Store
from hermas.tools import ToolError, execute_approved, proposal, read_tool
from hermas.web import create_app


class TestV03(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "app.php").write_text("<?php echo 'original';\n", encoding="utf8")
        self.cfg = Settings(
            app_dir=Path(__file__).resolve().parents[1], data_dir=self.root / "data", workspace=workspace,
            admin_password="secure-password-value", session_secret="x" * 40,
            model_key="", model_url="https://api.openai.com/v1", model_name="demo",
            model_provider="demo", allowed_hosts=("testserver", "localhost", "127.0.0.1"),
            secure_cookie=False, docker_tests=False, max_steps=4,
            max_output_tokens=300, daily_token_budget=20000,
        )
        self.db = Store(self.cfg.data_dir)
        self.client = TestClient(create_app(self.cfg,self.db))
        self.csrf = self.client.post("/api/login",json={"password":self.cfg.admin_password}).json()["csrf"]
        self.sid = self.client.post("/api/sessions",json={},headers=self.auth()).json()["id"]

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def auth(self):
        return {"X-Hermas-CSRF":self.csrf}

    def git(self,*args):
        return subprocess.run(["git","-C",str(self.cfg.workspace),*args],check=True,
                              capture_output=True,text=True).stdout.strip()

    def init_git(self):
        self.git("init","-q")
        self.git("add","app.php")
        self.git("-c","user.name=Example","-c","user.email=example@localhost","commit","-q","-m","Initial commit")

    def test_schema_and_old_approved_memory_migration(self):
        self.db.set_memory("feature","service pelanggan",self.sid)
        newer=Store(self.cfg.data_dir)
        with newer.connection() as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0],6)
            self.assertIn('kind',[r[1] for r in con.execute('PRAGMA table_info(tasks)')])
        self.assertEqual(newer.verified_memories()[0]['key'],'feature')

    def test_verified_memory_recall_project_and_evidence(self):
        row=self.db.save_verified_memory("project name","billing SaaS",self.sid,
                                          "billing","Confirmed in user design notes",.95)
        self.assertEqual(self.db.relevant_memories("billing",project="billing")[0]["id"],row['id'])
        self.assertFalse(self.db.relevant_memories("billing",project="other"))
        self.assertEqual(row["status"],"verified")
        self.assertEqual(row["confidence"],.95)

    def test_expired_memory_is_hidden(self):
        expired=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        self.db.save_verified_memory("old","value",self.sid,expires_at=expired)
        self.assertFalse(self.db.verified_memories())
        self.assertEqual(len(self.db.verified_memories(include_revoked=True)),1)

    def test_revoked_memory_is_never_returned_by_legacy_search(self):
        row=self.db.save_verified_memory("billing app","sales app",self.sid)
        self.assertTrue(self.db.search_memories("billing"))
        self.db.revoke_verified_memory(row['id'])
        self.assertEqual(self.db.search_memories("billing"),[])
        self.assertEqual(self.db.list_memories(),[])

    def test_project_memory_not_auto_injected(self):
        self.db.save_verified_memory("confidential client","client alpha",self.sid,
                                      project="client-alpha", evidence="Confirmed user request",confidence=.8)
        system=Agent(self.cfg,self.db)._system("client alpha")
        self.assertNotIn("client alpha",system)
        self.assertTrue(self.db.relevant_memories("client",project="client-alpha"))

    def test_secret_memory_rejected_before_approval(self):
        with self.assertRaises(ToolError):
            proposal(self.cfg,"save_verified_memory",{
                'key':'key','value':'api_key=abcdef123','project':'global',
                'evidence':'From user supplied message','confidence':.9})

    def test_verified_memory_requires_approval_then_revoke(self):
        a={'key':'preferensi','value':'PHP native','project':'global',
           'evidence':'Pernyataan eksplisit pengguna','confidence':.95}
        preview, expected=proposal(self.cfg,'save_verified_memory',a)
        self.assertIn('confidence',preview.lower())
        self.assertEqual(self.db.verified_memories(),[])
        aid=self.db.create_approval(self.sid,'toolverify','save_verified_memory',a,preview,expected)
        result=execute_approved(self.cfg,self.db,self.db.approval(aid))
        self.assertIn('tersimpan',result)
        identity=self.db.verified_memories()[0]['id']
        b={'id':identity}
        _,expected=proposal(self.cfg,'revoke_verified_memory',b)
        bid=self.db.create_approval(self.sid,'toolrevoke','revoke_verified_memory',b,'review',expected)
        execute_approved(self.cfg,self.db,self.db.approval(bid))
        self.assertEqual(self.db.verified_memories(),[])
        self.assertEqual(self.db.verified_memories(include_revoked=True)[0]['status'],'revoked')

    def test_memory_source_context_is_data_not_instructions(self):
        self.db.save_verified_memory("framework","Laravel",self.sid,
                                     evidence="User stated in design",confidence=.9)
        prompt=Agent(self.cfg,self.db)._system()
        self.assertIn('confidence=0.9',prompt)
        self.assertIn('evidence=',prompt)
        self.assertIn('DATA',prompt)

    def test_git_requires_workspace_root_repo(self):
        with self.assertRaises(ToolError):
            git_status(self.cfg)
        self.init_git()
        nested=self.cfg.workspace/'sub'
        nested.mkdir()
        with self.assertRaises(ToolError):
            git_status(replace(self.cfg,workspace=nested))

    def test_git_branch_approval_and_diff(self):
        self.init_git()
        preview, expected=proposal(self.cfg,'git_create_branch',{'branch':'hermas/improve-php'})
        self.assertIn('hermas/improve-php',preview)
        aid=self.db.create_approval(self.sid,'branch-tool','git_create_branch',
                                    {'branch':'hermas/improve-php'},preview,expected)
        self.assertNotEqual(self.git('branch','--show-current'),'hermas/improve-php')
        execute_approved(self.cfg,self.db,self.db.approval(aid))
        self.assertEqual(self.git('branch','--show-current'),'hermas/improve-php')
        (self.cfg.workspace/'app.php').write_text('<?php echo "updated";\n')
        self.assertIn('updated',git_diff(self.cfg,'app.php'))
        self.assertIn('app.php',git_status(self.cfg))

    def test_git_branch_rejected_for_dirty_repo_and_stale_status(self):
        self.init_git()
        _,digest=proposal(self.cfg,'git_create_branch',{'branch':'hermas/new'})
        (self.cfg.workspace/'app.php').write_text('modified')
        with self.assertRaises(ToolError):
            proposal(self.cfg,'git_create_branch',{'branch':'hermas/new'})
        aid=self.db.create_approval(self.sid,'branch-stale','git_create_branch',
                                    {'branch':'hermas/new'},'make branch',digest)
        with self.assertRaises(ToolError):
            execute_approved(self.cfg,self.db,self.db.approval(aid))

    def test_git_commit_approved_and_stale_rejected(self):
        self.init_git()
        self.git('switch','-q','-c','hermas/new-code')
        changed=self.cfg.workspace/'app.php'
        changed.write_text('<?php echo "v1";\n',encoding='utf8')
        args={'paths':['app.php'],'message':'Implement updated PHP output'}
        preview,stamp=proposal(self.cfg,'git_commit',args)
        self.assertIn('v1',preview)
        aid=self.db.create_approval(self.sid,'commit-tool','git_commit',args,preview,stamp)
        changed.write_text('<?php echo "another change";\n')
        with self.assertRaises(ToolError):
            execute_approved(self.cfg,self.db,self.db.approval(aid))
        _,stamp=proposal(self.cfg,'git_commit',args)
        bid=self.db.create_approval(self.sid,'commit-accepted','git_commit',args,'preview',stamp)
        result=execute_approved(self.cfg,self.db,self.db.approval(bid))
        self.assertIn('Commit berhasil',result)
        self.assertEqual(self.git('status','--porcelain'),'')

    def test_git_commit_blocks_untrusted_attributes(self):
        self.init_git()
        self.git('switch','-q','-c','hermas/test')
        (self.cfg.workspace/'.gitattributes').write_text('*.php filter=evil\n')
        (self.cfg.workspace/'app.php').write_text('<?php echo "evil";')
        with self.assertRaises(ToolError):
            proposal(self.cfg,'git_commit',{'paths':['app.php'],'message':'Unsafe commit test'})

    def test_multiagent_demo_worker_three_roles_persisted(self):
        task=self.client.post(f'/api/sessions/{self.sid}/tasks',
                              json={'message':'Improve PHP validation','kind':'orchestrate'},
                              headers=self.auth()).json()
        self.assertEqual(task['kind'],'orchestrate')
        result=asyncio.run(TaskWorker(self.db,Agent(self.cfg,self.db)).process_next())
        self.assertEqual(result['status'],'completed')
        steps=self.client.get(f'/api/tasks/{task["id"]}/stages').json()
        self.assertEqual([r['role'] for r in steps],['planner','coder','reviewer'])
        self.assertTrue(all('SIMULASI' in r['result'] for r in steps))
        self.assertIn('belum mengubah file',self.db.messages(self.sid)[-1]['content'])
        self.assertIn('original',(self.cfg.workspace/'app.php').read_text())
        self.assertTrue(self.db.audit_verified())

    def test_multiagent_request_idempotency_and_kind_conflict(self):
        first=self.db.enqueue_task(self.sid,'Review architecture','request-v03-0001',kind='orchestrate')
        same=self.db.enqueue_task(self.sid,'Review architecture','request-v03-0001',kind='orchestrate')
        self.assertEqual(first['id'],same['id'])
        with self.assertRaises(ValueError):
            self.db.enqueue_task(self.sid,'Review architecture','request-v03-0001',kind='chat')

    def test_multiagent_resume_only_missing_checkpoint(self):
        class SometimesFails:
            def __init__(self): self.n=0
            async def complete(self,messages,tools):
                self.n+=1
                if self.n==3: raise ModelError('temporary outage')
                return {'content':f'answer-{self.n}','usage':{}}
        cfg=replace(self.cfg,model_provider='openai')
        fake=SometimesFails()
        task=self.db.enqueue_task(self.sid,'Check inputs',kind='orchestrate')
        worker=TaskWorker(self.db,Agent(cfg,self.db,fake))
        self.assertEqual(asyncio.run(worker.process_next())['status'],'failed')
        self.assertEqual(len(self.db.orchestration_steps(task['id'])),2)
        self.db.resume_task(task['id'])
        self.assertEqual(asyncio.run(worker.process_next())['status'],'completed')
        self.assertEqual(fake.n,4)
        self.assertEqual(len(self.db.orchestration_steps(task['id'])),3)

    def test_multiagent_cancel_before_execution(self):
        task=self.db.enqueue_task(self.sid,'Handle security',kind='orchestrate')
        self.db.cancel_task(task['id'])
        self.assertIsNone(asyncio.run(TaskWorker(self.db,Agent(self.cfg,self.db)).process_next()))
        self.assertEqual(self.db.orchestration_steps(task['id']),[])

    def test_api_guards_stages_and_verification(self):
        r=self.client.get('/api/tasks/no-task/stages')
        self.assertEqual(r.status_code,404)
        self.assertEqual(self.client.post(f'/api/sessions/{self.sid}/tasks',
            json={'message':'task','kind':'danger'},headers=self.auth()).status_code,422)
        self.assertEqual(self.client.get('/api/me').json()['version'],'0.6.0')
        self.assertFalse(self.client.get('/api/memory').json())


if __name__ == '__main__':
    unittest.main()
