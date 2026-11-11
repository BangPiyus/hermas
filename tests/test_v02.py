"""Offline v0.2 reliability + API tests; no external LLM/Docker required."""
import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from hermas.agent import Agent, ModelError
from hermas.config import Settings
from hermas.jobs import TaskWorker
from hermas.singleton import single_instance
from hermas.store import Store
from hermas.tools import ToolError, proposal, _path, _make_safe_snapshot
from hermas.web import create_app


class TestV02(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "example.php").write_text("<?php echo 'one';\n", encoding="utf8")
        self.cfg = Settings(
            app_dir=Path(__file__).resolve().parents[1], data_dir=self.root / "data", workspace=workspace,
            admin_password="secure-password-value", session_secret="x" * 40,
            model_key="", model_url="https://api.openai.com/v1", model_name="demo",
            model_provider="demo", allowed_hosts=("testserver", "localhost", "127.0.0.1"),
            secure_cookie=False, docker_tests=False, max_steps=4,
            max_output_tokens=300, daily_token_budget=20000,
        )
        self.db = Store(self.cfg.data_dir)
        self.app = create_app(self.cfg, self.db)
        self.client = TestClient(self.app)
        login = self.client.post("/api/login", json={"password": self.cfg.admin_password})
        self.csrf = login.json()["csrf"]
        self.sid = self.client.post("/api/sessions", json={}, headers=self.auth()).json()["id"]

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def auth(self):
        return {"X-Hermas-CSRF": self.csrf}

    def queue(self, msg, key=None):
        payload = {"message": msg}
        if key is not None:
            payload["idempotency_key"] = key
        return self.client.post(f"/api/sessions/{self.sid}/tasks", json=payload, headers=self.auth())

    def test_queue_idempotency_and_sqlite_persistence(self):
        first = self.queue("/list", "my-request-0001")
        self.assertEqual(first.status_code, 202)
        retry = self.queue("/list", "my-request-0001")
        self.assertEqual(retry.json()["id"], first.json()["id"])
        self.assertEqual(len([m for m in self.db.messages(self.sid) if m["role"] == "user"]), 1)
        self.assertEqual(self.queue("/read example.php", "my-request-0001").status_code, 409)
        self.assertEqual(self.queue("/read example.php", "my-request-0002").status_code, 409)
        self.assertEqual(Store(self.cfg.data_dir).task(first.json()["id"])["status"], "queued")

    def test_worker_runs_to_completion_and_metrics(self):
        q = self.queue("/list")
        worker = TaskWorker(self.db, Agent(self.cfg, self.db))
        result = asyncio.run(worker.process_next())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["id"], q.json()["id"])
        self.assertEqual(result["attempts"], 1)
        self.assertIsNone(asyncio.run(worker.process_next()))
        self.assertEqual(self.db.metrics()["counts"]["completed"], 1)
        self.assertTrue(self.db.metrics()["audit_verified"])
        self.assertEqual(self.client.get("/api/metrics").status_code, 401 if not self.client.cookies else 200)

    def test_approval_resumes_task_without_immediate_agent_invocation(self):
        task = self.queue("/write example.php :: <?php echo 'two';")
        worker = TaskWorker(self.db, Agent(self.cfg, self.db))
        first = asyncio.run(worker.process_next())
        self.assertEqual(first["status"], "approval_required")
        pending = self.db.pending(self.sid)[0]
        self.assertEqual(self.client.get(f"/api/sessions/{self.sid}").json()["pending"][0]["risk"]["level"], "high")
        self.assertIn("one", (self.cfg.workspace / "example.php").read_text())
        response = self.client.post(f"/api/approvals/{pending['id']}/decision", json={"approved": True}, headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "queued")
        self.assertEqual(self.db.task(task.json()["id"])["status"], "queued")
        self.assertIn("two", (self.cfg.workspace / "example.php").read_text())
        second = asyncio.run(worker.process_next())
        self.assertEqual(second["status"], "completed")
        self.assertEqual(self.client.get("/api/tasks").json()[0]["attempts"], 2)
        self.assertTrue(self.db.audit_verified())

    def test_queued_cancel_and_duplicate_cancel(self):
        task = self.queue("/write example.php :: <?php echo 'bad';").json()
        out = self.client.post(f"/api/tasks/{task['id']}/cancel", json={}, headers=self.auth())
        self.assertEqual(out.json()["status"], "canceled")
        self.assertEqual(self.client.post(f"/api/tasks/{task['id']}/cancel", json={}, headers=self.auth()).status_code, 409)
        self.assertIsNone(self.db.claim_next_task())
        self.assertIn("one", (self.cfg.workspace / "example.php").read_text())

    def test_cancel_after_approval_revokes_pending(self):
        task = self.queue("/write example.php :: altered").json()
        worker = TaskWorker(self.db, Agent(self.cfg, self.db))
        asyncio.run(worker.process_next())
        aid = self.db.pending(self.sid)[0]["id"]
        out = self.client.post(f"/api/tasks/{task['id']}/cancel", json={}, headers=self.auth())
        self.assertEqual(out.json()["status"], "canceled")
        self.assertEqual(len(self.db.pending(self.sid)), 0)
        self.assertEqual(self.db.approval(aid)["state"], "rejected")
        self.assertEqual(self.client.post(f"/api/approvals/{aid}/decision", json={"approved": True}, headers=self.auth()).status_code, 409)

    def test_recover_without_replaying_approval(self):
        task = self.queue("/list").json()
        self.assertEqual(self.db.claim_next_task()["status"], "running")
        aid = self.db.create_approval(self.sid, "ab01", "write_file", {"path": "example.php", "content": "bad"}, "diff", None)
        self.assertTrue(self.db.claim_approval(aid))
        self.assertEqual(self.db.recover_orphan_tasks(), 1)
        self.assertEqual(self.db.task(task["id"])["status"], "interrupted")
        self.assertEqual(self.db.approval(aid)["state"], "unknown")
        self.assertEqual(self.client.post(f"/api/approvals/{aid}/decision", json={"approved": True}, headers=self.auth()).status_code, 409)
        self.assertEqual(self.client.post(f"/api/tasks/{task['id']}/resume", json={}, headers=self.auth()).status_code, 409)
        acknowledgement = self.client.post(f"/api/approvals/{aid}/acknowledge", json={}, headers=self.auth())
        self.assertEqual(acknowledgement.status_code, 200)
        self.assertEqual(self.db.approval(aid)["state"], "reviewed")
        out = self.client.post(f"/api/tasks/{task['id']}/resume", json={}, headers=self.auth())
        self.assertEqual(out.json()["status"], "queued")
        self.assertIn("one", (self.cfg.workspace / "example.php").read_text())

    def test_failed_provider_and_manual_retry(self):
        class UnavailableProvider:
            async def complete(self, messages, tools):
                raise ModelError("provider unavailable")
        task = self.queue("Hello").json()
        worker = TaskWorker(self.db, Agent(self.cfg, self.db, UnavailableProvider()))
        self.assertEqual(asyncio.run(worker.process_next())["status"], "failed")
        resumed = self.client.post(f"/api/tasks/{task['id']}/resume", json={}, headers=self.auth())
        self.assertEqual(resumed.json()["status"], "queued")
        self.assertEqual(asyncio.run(TaskWorker(self.db, Agent(self.cfg, self.db)).process_next())["status"], "completed")

    def test_cancel_running_during_provider_request(self):
        class DelayedProvider:
            def __init__(self):
                self.started=asyncio.Event()
                self.finish=asyncio.Event()
            async def complete(self, messages, tools):
                self.started.set()
                await self.finish.wait()
                return {"content": None, "tool_calls": [{"id":"write_after_cancel","type":"function",
                        "function":{"name":"write_file","arguments":json.dumps({"path":"example.php","content":"altered"})}}], "usage":{}}

        job=self.queue("Do something").json()
        provider=DelayedProvider()
        worker=TaskWorker(self.db, Agent(self.cfg,self.db,provider))
        async def scenario():
            running=asyncio.create_task(worker.process_next())
            await asyncio.wait_for(provider.started.wait(),2)
            self.assertEqual(self.db.cancel_task(job["id"])["status"],"running")
            provider.finish.set()
            return await running
        outcome=asyncio.run(scenario())
        self.assertEqual(outcome["status"],"canceled")
        self.assertEqual(self.db.pending(self.sid),[])
        self.assertIn("one",(self.cfg.workspace/"example.php").read_text())

    def test_old_database_schema_is_migrated_without_losing_sessions(self):
        data=self.root / "old-db"
        data.mkdir()
        path=data / "hermas.sqlite3"
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)")
            conn.execute("INSERT INTO sessions VALUES (?,?,?,?)",("legacy-id","Percakapan lama","2026-01-01","2026-01-01"))
            conn.commit()
        finally:
            conn.close()
        upgraded=Store(data)
        self.assertTrue(upgraded.exists("legacy-id"))
        with upgraded.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],6)
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE name='tasks'").fetchone())

    def test_queue_blocks_synchronous_chat(self):
        self.queue("/list")
        response=self.client.post(f"/api/sessions/{self.sid}/chat",json={"message":"/read example.php"},headers=self.auth())
        self.assertEqual(response.status_code,409)

    def test_double_approval_claim_rejected(self):
        self.queue("/write example.php :: new")
        asyncio.run(TaskWorker(self.db, Agent(self.cfg,self.db)).process_next())
        aid=self.db.pending(self.sid)[0]["id"]
        self.assertTrue(self.db.claim_approval(aid))
        response=self.client.post(f"/api/approvals/{aid}/decision",json={"approved":True},headers=self.auth())
        self.assertEqual(response.status_code,409)
        self.assertIn("one",(self.cfg.workspace/"example.php").read_text())

    def test_task_api_requires_csrf(self):
        response = self.client.post(f"/api/sessions/{self.sid}/tasks", json={"message": "/list"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/healthz").json()["version"], "0.6.0")

    def test_approval_diff_cannot_be_partially_hidden(self):
        text="\n".join(f"line {i}" for i in range(10000))
        with self.assertRaises(ToolError):
            proposal(self.cfg, "write_file", {"path": "example.php", "content": text})

    def test_snapshot_excludes_credentials_and_symlinks(self):
        (self.cfg.workspace / ".env").write_text("API_KEY=SENSITIVE")
        (self.cfg.workspace / "credentials.json").write_text('{"secret":"sensitive"}')
        (self.cfg.workspace / "secret.key").write_text("private")
        (self.cfg.workspace / "linked.php").symlink_to(self.cfg.workspace / "example.php")
        output=self.root / "snapshot"
        output.mkdir()
        count=_make_safe_snapshot(self.cfg, output)
        self.assertEqual(count,1)
        self.assertTrue((output / "example.php").exists())
        self.assertFalse((output / ".env").exists())
        self.assertFalse((output / "credentials.json").exists())
        self.assertFalse((output / "secret.key").exists())
        self.assertFalse((output / "linked.php").exists())

    def test_internal_symlink_rejected(self):
        (self.cfg.workspace / "alias.php").symlink_to(self.cfg.workspace / "example.php")
        with self.assertRaises(ToolError):
            _path(self.cfg, "alias.php", is_file=True, writing=True)

    @unittest.skipIf(os.name == "nt", "Unix lock test")
    def test_process_lock(self):
        with single_instance(self.cfg.data_dir):
            with self.assertRaises(RuntimeError):
                with single_instance(self.cfg.data_dir):
                    pass

    def test_claim_atomic_concurrently(self):
        self.queue("/list")
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed=list(pool.map(lambda _: self.db.claim_next_task(), [1,2]))
        self.assertEqual(sum(item is not None for item in claimed), 1)

    def test_real_lifespan_worker(self):
        # Use an independent data directory to avoid the existing TestClient instance.
        with TestClient(create_app(self.cfg, self.db)) as active:
            credentials=active.post("/api/login", json={"password":self.cfg.admin_password}).json()
            token={"X-Hermas-CSRF":credentials["csrf"]}
            sid=active.post("/api/sessions",json={},headers=token).json()["id"]
            task=active.post(f"/api/sessions/{sid}/tasks", json={"message":"/list"},headers=token).json()
            deadline=time.monotonic()+4
            while time.monotonic()<deadline:
                record=active.get(f"/api/tasks/{task['id']}").json()
                if record["status"]=="completed":
                    break
                time.sleep(.05)
            self.assertEqual(record["status"],"completed")


if __name__ == "__main__":
    unittest.main()
