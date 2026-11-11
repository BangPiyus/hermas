"""Durable, fail-closed single-process task dispatcher.

The database stores task states. Work never silently replays after an uncertain
restart, and side effects require their own explicit approval.
"""
from __future__ import annotations

import asyncio
import time
import logging
from contextlib import suppress

from .agent import Agent
from .automation import AutomationService
from .orchestration import Orchestrator
from .store import Store


class TaskWorker:
    def __init__(self, db: Store, agent: Agent):
        self.db = db
        self.agent = agent
        self.orchestrator = Orchestrator(db, agent)
        self.automations = AutomationService(db)
        self.wakeup = asyncio.Event()
        self.stop = asyncio.Event()

    async def process_next(self) -> dict | None:
        task = self.db.claim_next_task()
        if task is None:
            return None
        started = time.perf_counter()
        try:
            if task.get("kind") == "orchestrate":
                status = await self.orchestrator.run(task["id"],task["session_id"],should_stop=lambda: self.db.cancel_requested(task["id"]))
            else:
                status = await self.agent.run(task["session_id"], should_stop=lambda: self.db.cancel_requested(task["id"]))
            if status == "provider_error":
                state = "failed"
                error = "Provider model gagal; periksa log audit dan sesi."
            elif status in {"completed", "approval_required", "step_limit", "budget_exceeded", "canceled"}:
                state = status
                error = None
            else:
                state = "failed"
                error = "Agent mengembalikan status yang tidak dikenal"
        except Exception as exc:
            state = "failed"
            # Never leak raw model errors, credentials, or full messages into task metadata.
            error = f"Eksekusi gagal: {type(exc).__name__}"
            self.db.audit("task_exception", {"task": task["id"], "type": type(exc).__name__})
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        result = self.db.finish_task(task["id"], state, elapsed_ms, error)
        # A completed stage is only advanced after a committed final task status.
        self.automations.reconcile_runs()
        return result

    async def run_forever(self):
        last_tick = 0.0
        while not self.stop.is_set():
            try:
                # Retry only the scheduler decision on each loop, never uncertain agent work.
                self.automations.reconcile_runs()
                if time.monotonic() - last_tick >= 15:
                    self.automations.tick()
                    last_tick = time.monotonic()
                processed = await self.process_next()
            except Exception as exc:
                logging.getLogger(__name__).error("Hermas worker error: %s", type(exc).__name__)
                processed = None
            if processed is None:
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                self.wakeup.clear()

    def notify(self):
        self.wakeup.set()

    def shutdown(self):
        self.stop.set()
        self.wakeup.set()
