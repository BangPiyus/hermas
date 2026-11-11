"""Hermas security and approval regression tests (offline, no API key required)."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from hermas.agent import Agent
from hermas.config import Settings
from hermas.store import Store
from hermas.tools import ToolError, _path, execute_approved, proposal, read_tool
from hermas.web import create_app


class HermasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "hello.py").write_text("print('old')\n", encoding="utf-8")
        (workspace / ".env").write_text("TOPSECRET=123\n", encoding="utf-8")
        self.cfg = Settings(
            app_dir=Path(__file__).resolve().parents[1],
            data_dir=self.root / "data", workspace=workspace,
            admin_password="long-secure-password", session_secret="s" * 48,
            model_key="", model_url="https://api.openai.com/v1", model_name="demo-model",
            model_provider="demo", allowed_hosts=("testserver", "localhost", "127.0.0.1"),
            secure_cookie=False, docker_tests=False,
            max_steps=4, max_output_tokens=300, daily_token_budget=20000,
        )
        self.db = Store(self.cfg.data_dir)
        self.client = TestClient(create_app(self.cfg, self.db))
        self.login()

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def login(self):
        bad = self.client.post('/api/login', json={'password': 'wrong'})
        self.assertEqual(bad.status_code, 401)
        good = self.client.post('/api/login', json={'password': self.cfg.admin_password})
        self.assertEqual(good.status_code, 200)
        self.csrf = good.json()['csrf']

    def post(self, endpoint, payload):
        return self.client.post(endpoint, json=payload, headers={'X-Hermas-CSRF': self.csrf})

    def session(self):
        return self.post('/api/sessions', {}).json()['id']

    def test_csrf_and_auth(self):
        self.assertEqual(self.client.post('/api/sessions', json={}).status_code, 403)
        self.assertEqual(self.post('/api/sessions', {}).status_code, 200)
        self.assertEqual(self.client.post('/api/sessions', json={}, headers={'X-Hermas-CSRF': 'incorrect'}).status_code, 403)

    def test_safe_read_and_secret_block(self):
        self.assertIn("hello.py", read_tool(self.cfg, self.db, 'list_files', {'path': '.'}))
        self.assertIn('old', read_tool(self.cfg, self.db, 'read_file', {'path': 'hello.py'}))
        with self.assertRaises(ToolError):
            read_tool(self.cfg, self.db, 'read_file', {'path': '.env'})
        with self.assertRaises(ToolError):
            read_tool(self.cfg, self.db, 'read_file', {'path': '../config.py'})

    def test_symlink_escape_is_blocked(self):
        link = self.cfg.workspace / 'outside.py'
        link.symlink_to(self.root / 'outside.py')
        with self.assertRaises(ToolError):
            _path(self.cfg, 'outside.py', is_file=True)

    def test_write_requires_approval_and_creates_backup(self):
        sid = self.session()
        res = self.post(f'/api/sessions/{sid}/chat', {'message': '/write hello.py :: print("new")'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['status'], 'approval_required')
        self.assertIn('old', (self.cfg.workspace / 'hello.py').read_text())
        approval = res.json()['pending'][0]
        self.assertIn('-print', approval['preview'])
        self.assertNotIn('arguments_json', approval)
        res = self.post(f"/api/approvals/{approval['id']}/decision", {'approved': True})
        self.assertEqual(res.status_code, 200)
        self.assertEqual((self.cfg.workspace / 'hello.py').read_text(), 'print("new")')
        self.assertTrue((self.cfg.data_dir / 'backups' / (approval['id'] + '.bak')).exists())
        self.assertEqual(len(res.json()['pending']), 0)
        self.assertTrue(self.db.audit_verified())

    def test_rejection_does_not_modify(self):
        sid = self.session()
        res = self.post(f'/api/sessions/{sid}/chat', {'message': '/write hello.py :: removed'})
        identity = res.json()['pending'][0]['id']
        out = self.post(f'/api/approvals/{identity}/decision', {'approved': False})
        self.assertEqual(out.status_code, 200)
        self.assertIn('old', (self.cfg.workspace / 'hello.py').read_text())

    def test_conflict_prevents_stale_overwrite(self):
        sid = self.session()
        res = self.post(f'/api/sessions/{sid}/chat', {'message': '/write hello.py :: new'})
        identity = res.json()['pending'][0]['id']
        (self.cfg.workspace / 'hello.py').write_text('modified by another process')
        out = self.post(f'/api/approvals/{identity}/decision', {'approved': True})
        self.assertEqual(out.status_code, 200)
        self.assertEqual((self.cfg.workspace / 'hello.py').read_text(), 'modified by another process')
        self.assertTrue(any('TOOL ERROR' in (m['content'] or '') for m in out.json()['messages']))

    def test_memory_requires_approval_and_survives_sessions(self):
        sid = self.session()
        res = self.post(f'/api/sessions/{sid}/chat', {'message': '/remember project: coding PHP native'})
        self.assertFalse(self.db.list_memories())
        pending = res.json()['pending'][0]
        self.post(f"/api/approvals/{pending['id']}/decision", {'approved': True})
        self.assertEqual(self.db.list_memories()[0]['value'], 'coding PHP native')
        new_sid = self.session()
        self.assertTrue(self.db.exists(new_sid))
        self.assertIn('coding PHP native', Agent(self.cfg, self.db)._system())

    def test_tool_disabled_by_default(self):
        sid = self.session()
        res = self.post(f'/api/sessions/{sid}/chat', {'message': '/test'})
        pending = res.json()['pending'][0]
        out = self.post(f"/api/approvals/{pending['id']}/decision", {'approved': True})
        self.assertTrue(any('Runner Docker nonaktif' in (m['content'] or '') for m in out.json()['messages']))

    def test_audit_detects_tamper(self):
        self.session()
        self.assertTrue(self.db.audit_verified())
        with self.db.connection() as conn:
            conn.execute("UPDATE audit SET details='tampered' WHERE id=(SELECT max(id) FROM audit)")
        self.assertFalse(self.db.audit_verified())

    def test_tool_calling_with_fake_llm(self):
        class FakeProvider:
            async def complete(self, messages, tools):
                if messages[-1]["role"] == "tool":
                    return {"content": "Perubahan sudah diproses dan diperiksa.", "tool_calls": [], "usage": {"prompt_tokens": 20, "completion_tokens": 12}}
                return {"content": None, "tool_calls": [
                    {"id": "read_call", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "hello.py"})}},
                    {"id": "write_call", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "hello.py", "content": "print(2)\n"})}},
                ], "usage": {"prompt_tokens": 22, "completion_tokens": 17}}
        sid = self.db.create_session()['id']
        self.db.add_message(sid, 'user', 'Periksa dan perbarui hello.py')
        agent = Agent(self.cfg, self.db, FakeProvider())
        self.assertEqual(asyncio.run(agent.run(sid)), 'approval_required')
        self.assertIn('old', (self.cfg.workspace / 'hello.py').read_text())
        pending = self.db.pending(sid)[0]
        result = execute_approved(self.cfg, self.db, pending)
        self.db.decide(pending['id'], True, result)
        self.db.add_message(sid, 'tool', result, tool_call_id=pending['tool_call_id'])
        self.assertEqual(asyncio.run(agent.run(sid)), 'completed')
        self.assertEqual((self.cfg.workspace / 'hello.py').read_text(), 'print(2)\n')
        self.assertIn('Perubahan sudah', self.db.messages(sid)[-1]['content'])

    def test_secret_protection_in_write(self):
        with self.assertRaises(ToolError):
            proposal(self.cfg, 'write_file', {'path': 'wp-config.php', 'content': 'bad'})
        with self.assertRaises(ToolError):
            proposal(self.cfg, 'write_file', {'path': '.env', 'content': 'secret'})

    def test_origin_block(self):
        res = self.client.post('/api/sessions', json={}, headers={'Origin':'https://example.net','X-Hermas-CSRF':self.csrf})
        self.assertEqual(res.status_code, 403)

    def test_skill_requires_approval(self):
        sid = self.session()
        res = self.post(f'/api/sessions/{sid}/chat', {'message': '/skill php-style :: Avoid get_result; use bind_result'})
        self.assertFalse(self.db.list_skills())
        identity = res.json()['pending'][0]['id']
        self.post(f'/api/approvals/{identity}/decision', {'approved': True})
        self.assertEqual(self.db.list_skills()[0]['slug'], 'php-style')


if __name__ == '__main__':
    unittest.main()
