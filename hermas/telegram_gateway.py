"""Disabled-by-default Telegram polling with strict chat ID allowlisting.

Never sends workspace contents or credentials, and does not authorize tool
approvals. Commands only: /status, /tasks, /workflows, /run (opt-in).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
import httpx

from .automation import AutomationService, AutomationError
from .store import Store, now

LOG = logging.getLogger(__name__)


class TelegramGateway:
    def __init__(self, db: Store, service: AutomationService, token: str, chat_ids: tuple[int, ...], allow_run: bool = False):
        if not token or not chat_ids:
            raise ValueError('Token dan Telegram chat allowlist wajib')
        self.db, self.service, self.token, self.chat_ids, self.allow_run = db, service, token, chat_ids, allow_run
        self.stop = asyncio.Event()
        self._unauthorized_logged = False

    def get_offset(self) -> int:
        with self.db.connection() as db:
            row = db.execute("SELECT value FROM gateway_state WHERE key='telegram.offset'").fetchone()
            return int(row['value']) if row else 0

    def set_offset(self, offset: int):
        with self.db.connection() as db:
            db.execute("INSERT INTO gateway_state(key,value,updated_at) VALUES('telegram.offset',?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(str(offset), now()))

    def handle_message(self, chat_id: int, text: str) -> str | None:
        if chat_id not in self.chat_ids:
            if not self._unauthorized_logged:
                self.db.audit('telegram_unauthorized', {'channel':'telegram'})
                self._unauthorized_logged = True
            return None
        command = text.strip().split(maxsplit=1)
        if not command:
            return None
        verb = command[0].split('@')[0]
        if verb in ('/start','/help'):
            return 'Hermas v0.6: /status, /tasks, /workflows' + (', /run <workflow_id>' if self.allow_run else '')
        if verb == '/status':
            return 'Hermas aktif. Task hari ini: ' + str(len(self.db.tasks(limit=50))) + '. Perubahan file perlu approval di dashboard.'
        if verb == '/tasks':
            tasks = self.db.tasks(limit=5)
            return '\n'.join(f"{task['id'][:8]} {task['status']}" for task in tasks) or 'Belum ada tugas.'
        if verb == '/workflows':
            flows = self.service.workflows()[:10]
            return '\n'.join(f"{w['id']} — {w['name']}" for w in flows) or 'Belum ada workflow.'
        if verb == '/run' and self.allow_run:
            if len(command) != 2:
                return 'Gunakan /run <workflow_id>'
            try:
                result = self.service.start_workflow(command[1].strip())
            except AutomationError as exc:
                return 'Tidak dapat menjalankan workflow: ' + str(exc)
            self.db.audit('telegram_workflow_started',{'run':result['id']})
            return 'Workflow dijadwalkan. Run ID: ' + result['id']
        return 'Perintah tidak dikenal. Ketik /help.'

    async def send(self, client: httpx.AsyncClient, chat_id: int, message: str):
        response = await client.post('https://api.telegram.org/bot' + self.token + '/sendMessage',
                                     json={'chat_id':chat_id,'text':message[:3000]}, timeout=10)
        response.raise_for_status()

    async def poll_once(self, client: httpx.AsyncClient):
        response = await client.get('https://api.telegram.org/bot' + self.token + '/getUpdates',
                                     params={'offset':self.get_offset(),'timeout':15,'allowed_updates':'["message"]'}, timeout=20)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        if not data.get('ok') or not isinstance(data.get('result'), list):
            raise ValueError('Telegram respons tidak sesuai')
        for item in data['result']:
            uid = item.get('update_id')
            if type(uid) is not int:
                continue
            # Persist offset before action: do not duplicate workflow on network retries.
            self.set_offset(uid + 1)
            message = item.get('message', {})
            chat_id = message.get('chat', {}).get('id')
            text = message.get('text', '')
            if type(chat_id) is not int or not isinstance(text, str):
                continue
            answer = self.handle_message(chat_id, text)
            if answer is not None:
                await self.send(client, chat_id, answer)

    async def run_forever(self):
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            while not self.stop.is_set():
                try:
                    await self.poll_once(client)
                except (httpx.HTTPError, ValueError) as exc:
                    LOG.warning('Telegram gateway gagal: %s',type(exc).__name__)
                    await asyncio.sleep(5)
