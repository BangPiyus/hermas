"""Single-user web interface with signed httpOnly sessions and CSRF enforcement."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
import secrets
import time
from collections import defaultdict
from pathlib import Path
from typing import Literal

from fastapi import Body, Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import Agent
from .automation import AutomationService, AutomationError
from .integrations import IntegrationRegistry, IntegrationError
from .browser import BrowserError, preview as browser_preview
from .telegram_gateway import TelegramGateway
from .config import Settings
from .vps import VPSService, VPSException
from .access import AccessService
from .operations import OperationsService, OperationsError
from .snapshots import WorkspaceSnapshots, SnapshotError
from .jobs import TaskWorker
from .singleton import single_instance
from .store import Store
from .tools import ToolError, execute_approved, read_tool


class Credentials(BaseModel):
    password: str = Field(max_length=256)
    username: str = Field(default='admin',max_length=32)


class AccountCreate(BaseModel):
    username: str = Field(min_length=3,max_length=32)
    password: str = Field(min_length=14,max_length=256)
    role: Literal['viewer','operator']


class AccountChange(BaseModel):
    enabled: bool | None = None
    role: Literal['viewer','operator'] | None = None
    password: str | None = Field(default=None,min_length=14,max_length=256)


class MonitorCreate(BaseModel):
    host_id: str
    kind: Literal['health','wordpress']
    interval_minutes: int = Field(default=30,ge=5,le=1440)
    root: str = Field(default='',max_length=220)


class RestoreConfirm(BaseModel):
    token: str = Field(min_length=16,max_length=30000)
    confirmation: str = Field(max_length=20)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)


class DecisionRequest(BaseModel):
    approved: bool


class WorkflowStep(BaseModel):
    kind: Literal['chat','orchestrate']
    message: str = Field(min_length=1, max_length=4000)


class WorkflowCreate(BaseModel):
    session_id: str
    name: str = Field(min_length=1, max_length=80)
    steps: list[WorkflowStep] = Field(min_length=1, max_length=8)
    enabled: bool = True


class ScheduleCreate(BaseModel):
    session_id: str
    name: str = Field(min_length=1, max_length=80)
    cron: str = Field(min_length=5, max_length=100)
    target: Literal['chat','orchestrate','workflow']
    message: str = Field(default='', max_length=4000)
    workflow_id: str | None = None
    enabled: bool = False


class ToggleRequest(BaseModel):
    enabled: bool


class IntegrationCreate(BaseModel):
    manifest: dict


class BrowserPreviewRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class VPSHostCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    hostname: str = Field(min_length=1, max_length=253)
    ssh_user: str = Field(min_length=1, max_length=32)
    port: int = Field(default=22, ge=1, le=65535)


class VPSCheckRequest(BaseModel):
    kind: Literal['health','wordpress']
    root: str = Field(default='', max_length=220)


class VPSPlanCreate(BaseModel):
    kind: Literal['backup','deployment']
    root: str = Field(default='', max_length=220)
    notes: str = Field(default='', max_length=1200)


class TaskRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100)
    kind: Literal["chat", "orchestrate"] = "chat"


def approval_risk(item: dict) -> dict:
    name = item["tool_name"]
    if name == "write_file":
        arguments = json.loads(item["arguments_json"])
        extension = str(arguments.get("path", "")).lower().rsplit(".", 1)[-1]
        level = "high" if extension in {"sql", "php", "py", "js", "ts", "yml", "yaml"} else "medium"
        return {"level": level, "reason": "Perubahan kode/file: periksa diff lengkap dan backup."}
    if name in {"git_create_branch", "git_commit"}:
        return {"level": "high", "reason": "Perubahan Git di host lokal; review status dan diff terlebih dahulu."}
    if name == "revoke_verified_memory":
        return {"level": "medium", "reason": "Memori akan berhenti disajikan ke AI."}
    if name == "save_verified_memory":
        return {"level": "medium", "reason": "Periksa bukti, confidence dan expiry sebelum menyimpan memori."}
    if name == "run_tests":
        return {"level": "high", "reason": "Eksekusi kode dalam container; audit izin Docker host."}
    if name == "save_skill":
        return {"level": "medium", "reason": "Skill dapat memengaruhi perilaku AI berikutnya."}
    return {"level": "medium", "reason": "Memori jangka panjang perlu verifikasi sumber dan akurasi."}


def create_app(cfg: Settings | None = None, db: Store | None = None, agent: Agent | None = None) -> FastAPI:
    cfg = cfg or Settings.from_env()
    cfg.workspace.mkdir(mode=0o750, parents=True, exist_ok=True)
    db = db or Store(cfg.data_dir)
    agent = agent or Agent(cfg, db)
    signer = URLSafeTimedSerializer(cfg.session_secret, salt="hermas-web-session-v1")
    worker = TaskWorker(db, agent)
    automations = worker.automations
    registry = IntegrationRegistry(db)
    vps = VPSService(db, enabled=cfg.vps_ssh_enabled,
                     ssh_key=cfg.vps_ssh_key_path, known_hosts=cfg.vps_known_hosts_path)
    access = AccessService(db)
    ops = OperationsService(db,vps,enabled=cfg.vps_monitor_enabled,notify_enabled=cfg.alert_telegram_enabled)
    snapshots = WorkspaceSnapshots(db,cfg.workspace,cfg.session_secret)
    telegram = (TelegramGateway(db, automations, cfg.telegram_token, cfg.telegram_chat_ids, cfg.telegram_allow_run)
                if cfg.telegram_enabled else None)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Single-process deployment only. Uncertain work requires human recovery.
        with single_instance(cfg.data_dir):
            db.recover_orphan_tasks()
            runner = asyncio.create_task(worker.run_forever(), name="hermas-task-worker")
            operations_runner = asyncio.create_task(ops.run_forever(cfg.telegram_token,cfg.telegram_chat_ids,cfg.alert_telegram_enabled), name='hermas-operations')
            telegram_runner = asyncio.create_task(telegram.run_forever(), name='hermas-telegram') if telegram else None
            try:
                yield
            finally:
                worker.shutdown()
                if telegram_runner:
                    telegram.stop.set()
                    telegram_runner.cancel()
                    with suppress(asyncio.CancelledError):
                        await telegram_runner
                operations_runner.cancel()
                with suppress(asyncio.CancelledError):
                    await operations_runner
                runner.cancel()
                with suppress(asyncio.CancelledError):
                    await runner

    app = FastAPI(title="Hermas Agentic AI", version="0.6.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.task_worker = worker
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(cfg.allowed_hosts))
    app.mount("/static", StaticFiles(directory=str(cfg.app_dir / "static")), name="static")
    locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
    attempts: dict[str, list[float]] = defaultdict(list)

    @app.middleware("http")
    async def security_headers(req: Request, call_next):
        # Same-origin CSRF header required on every authenticated mutating endpoint.
        if req.url.path.startswith("/api/") and req.method not in {"GET", "HEAD", "OPTIONS"}:
            # Compare Host / Origin, fail closed for a browser declaring an origin.
            origin = req.headers.get("origin")
            expected = f"{req.url.scheme}://{req.headers.get('host', '')}"
            if origin and origin != expected:
                return Response("Origin tidak diizinkan", status_code=403)
        resp = await call_next(req)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # All scripts and Tailwind CSS are vendored locally; no third-party runtime scripts.
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
            "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return resp

    def require_member(session: str | None = Cookie(default=None, alias="hermas_session")) -> dict:
        if not session:
            raise HTTPException(401, "Silakan login")
        try:
            data = signer.loads(session, max_age=86400)
        except (BadSignature, SignatureExpired):
            raise HTTPException(401, "Sesi tidak valid")
        if not data.get('csrf') or not isinstance(data.get('sub'),str):
            raise HTTPException(401, "Sesi tidak valid")
        if data['sub']=='admin':
            return {'sub':'admin','username':'admin','role':'admin','csrf':data['csrf']}
        if not data['sub'].startswith('usr:'):
            raise HTTPException(401,"Sesi tidak valid")
        account=access.get(data['sub'][4:])
        if not account or not account['enabled'] or account['session_version']!=data.get('epoch'):
            raise HTTPException(401,"Sesi telah dicabut")
        return {'sub':data['sub'],'username':account['username'],'role':account['role'],'csrf':data['csrf']}

    def require_user(user: dict = Depends(require_member)) -> dict:
        # Legacy data (chat, memory, code, audit, keys) remains admin-only.
        if user['role']!='admin':
            raise HTTPException(403,'Fitur ini hanya tersedia bagi administrator')
        return user

    def mutating(user: dict = Depends(require_user), csrf: str | None = Header(None, alias="X-Hermas-CSRF")) -> dict:
        if not csrf or not secrets.compare_digest(csrf, user["csrf"]):
            raise HTTPException(403, "CSRF token tidak valid")
        return user

    def operator_mutating(user: dict = Depends(require_member), csrf: str | None = Header(None, alias="X-Hermas-CSRF")) -> dict:
        if user['role'] not in {'operator','admin'}:
            raise HTTPException(403,'Diperlukan izin operator')
        if not csrf or not secrets.compare_digest(csrf, user['csrf']):
            raise HTTPException(403, "CSRF token tidak valid")
        return user

    def member_mutating(user: dict = Depends(require_member), csrf: str | None = Header(None, alias="X-Hermas-CSRF")) -> dict:
        if not csrf or not secrets.compare_digest(csrf, user['csrf']):
            raise HTTPException(403,"CSRF token tidak valid")
        return user

    @app.get("/")
    def index():
        return FileResponse(str(cfg.app_dir / "static" / "index.html"))

    @app.post("/api/login")
    def login(request: Request, response: Response, form: Credentials = Body(...)):
        ip = request.client.host if request.client else "unknown"
        recent = [t for t in attempts[ip] if t > time.monotonic() - 900]
        attempts[ip] = recent
        if len(recent) >= 5:
            raise HTTPException(429, "Terlalu banyak percobaan login. Coba setelah 15 menit.")
        username=form.username.strip().lower()
        account=None
        if username=='admin':
            ok=secrets.compare_digest(form.password,cfg.admin_password)
        else:
            account=access.authenticate(username,form.password)
            ok=account is not None
        if not ok:
            attempts[ip].append(time.monotonic())
            db.audit("login_failed", {"source": "web"})
            raise HTTPException(401, "Username atau password salah")
        attempts[ip] = []
        csrf = secrets.token_urlsafe(24)
        payload={"sub": "admin", "csrf": csrf} if username=='admin' else {"sub": "usr:"+account['id'], "csrf":csrf,"epoch":account['session_version']}
        cookie = signer.dumps(payload)
        response.set_cookie("hermas_session", cookie, httponly=True, secure=cfg.secure_cookie,
                            samesite="strict", max_age=86400, path="/")
        db.audit("login_success", {"source": "web"})
        return {"ok": True, "csrf": csrf}

    @app.get("/api/me")
    def me(user: dict = Depends(require_member)):
        return {"csrf": user["csrf"], "role":user['role'], "username":user['username'], "model": cfg.model_name,
                "mode": cfg.model_provider, "sandbox_enabled": cfg.docker_tests,
                "usage": db.usage_today(), "daily_token_budget": cfg.daily_token_budget,
                "audit_verified": db.audit_verified(), "version": "0.6.0", "queue": True, "multi_agent": True, "git_studio": True, "verified_memory": True,
                "automation": True, "plugin_registry": True, "telegram_enabled": bool(telegram), "browser_enabled": cfg.browser_enabled,
                "vps_commander": True, "vps_ssh_enabled": cfg.vps_ssh_enabled,
                "vps_monitor_enabled":cfg.vps_monitor_enabled,"alert_telegram_enabled":cfg.alert_telegram_enabled}

    @app.post("/api/logout")
    def logout(response: Response, _=Depends(member_mutating)):
        response.delete_cookie("hermas_session", path="/")
        return {"ok": True}

    @app.get("/api/sessions")
    def sessions(_=Depends(require_user)):
        return db.sessions()

    @app.post("/api/sessions")
    def new_session(_=Depends(mutating)):
        return db.create_session()

    def session_data(sid: str):
        if not db.exists(sid):
            raise HTTPException(404, "Sesi tidak ditemukan")
        approvals = db.pending(sid)
        # Avoid sending raw full arguments to browser; diff / preview is enough.
        for item in approvals:
            item["risk"] = approval_risk(item)
            item.pop("arguments_json", None)
        return {"messages": db.messages(sid), "pending": approvals, "tasks": db.tasks(sid, 10)}

    @app.get("/api/sessions/{sid}")
    def get_session(sid: str, _=Depends(require_user)):
        return session_data(sid)

    @app.post("/api/sessions/{sid}/chat")
    async def chat(sid: str, body: ChatRequest = Body(...), _=Depends(mutating)):
        async with locks[sid]:
            if not db.exists(sid):
                raise HTTPException(404, "Sesi tidak ditemukan")
            if db.pending(sid) or db.active_task(sid):
                raise HTTPException(409, "Selesaikan tugas aktif dan approval sebelum mengirim pesan")
            db.add_message(sid, "user", body.message.strip())
            status = await agent.run(sid)
            return {"status": status, **session_data(sid)}

    @app.post("/api/sessions/{sid}/tasks", status_code=202)
    async def queue_task(sid: str, body: TaskRequest = Body(...), _=Depends(mutating)):
        async with locks[sid]:
            try:
                task = db.enqueue_task(sid, body.message, body.idempotency_key, kind=body.kind)
            except LookupError:
                raise HTTPException(404, "Sesi tidak ditemukan")
            except ValueError as exc:
                raise HTTPException(409, str(exc))
            worker.notify()
            return task

    @app.get("/api/tasks")
    def list_tasks(session_id: str | None = None, _=Depends(require_user)):
        return db.tasks(session_id)

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, _=Depends(require_user)):
        task = db.task(task_id)
        if task is None:
            raise HTTPException(404, "Tugas tidak ditemukan")
        return task

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, _=Depends(mutating)):
        task = db.task(task_id)
        if not task:
            raise HTTPException(404, "Tugas tidak ditemukan")
        async with locks[task["session_id"]]:
            try:
                return db.cancel_task(task_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc))

    @app.post("/api/tasks/{task_id}/resume")
    async def resume_task(task_id: str, _=Depends(mutating)):
        task = db.task(task_id)
        if not task:
            raise HTTPException(404, "Tugas tidak ditemukan")
        async with locks[task["session_id"]]:
            try:
                result = db.resume_task(task_id, max_attempts=3)
            except ValueError as exc:
                raise HTTPException(409, str(exc))
            worker.notify()
            return result

    @app.get("/api/tasks/{task_id}/stages")
    def get_stages(task_id: str, _=Depends(require_user)):
        task = db.task(task_id)
        if not task:
            raise HTTPException(404, "Tugas tidak ditemukan")
        if task.get("kind") != "orchestrate":
            raise HTTPException(400, "Tugas ini bukan multi-agent")
        return db.orchestration_steps(task_id)

    @app.get("/api/git/status")
    def git_status_api(_=Depends(require_user)):
        try:
            return json.loads(read_tool(cfg,db,"git_status",{}))
        except ToolError as exc:
            raise HTTPException(400,str(exc))

    @app.get("/api/git/diff")
    def git_diff_api(path: str, _=Depends(require_user)):
        try:
            return {"path":path,"diff":read_tool(cfg,db,"git_diff",{"path":path})}
        except ToolError as exc:
            raise HTTPException(400,str(exc))

    @app.get('/api/workflows')
    def list_workflows(_=Depends(require_user)):
        return automations.workflows()

    @app.post('/api/workflows', status_code=201)
    def create_workflow(body: WorkflowCreate, _=Depends(mutating)):
        if not db.exists(body.session_id):
            raise HTTPException(404,'Sesi tidak ditemukan')
        try:
            return automations.create_workflow(body.session_id, body.name,
                    [step.model_dump() for step in body.steps], body.enabled)
        except AutomationError as exc:
            raise HTTPException(400,str(exc))

    @app.post('/api/workflows/{wid}/enabled')
    def workflow_enabled(wid: str, body: ToggleRequest, _=Depends(mutating)):
        try:
            return automations.set_workflow_enabled(wid, body.enabled)
        except LookupError as exc:
            raise HTTPException(404,str(exc))

    @app.post('/api/workflows/{wid}/run', status_code=202)
    def run_workflow(wid: str, _=Depends(mutating)):
        try:
            result = automations.start_workflow(wid)
        except AutomationError as exc:
            raise HTTPException(409,str(exc))
        worker.notify()
        return result

    @app.get('/api/workflow-runs')
    def workflow_runs(_=Depends(require_user)):
        return automations.workflow_runs()

    @app.post('/api/workflow-runs/{rid}/cancel')
    def cancel_workflow(rid: str, _=Depends(mutating)):
        try:
            automations.cancel_run(rid)
        except LookupError as exc:
            raise HTTPException(404,str(exc))
        except AutomationError as exc:
            raise HTTPException(409,str(exc))
        return {'ok': True}

    @app.get('/api/schedules')
    def list_schedules(_=Depends(require_user)):
        return automations.schedules()

    @app.post('/api/schedules', status_code=201)
    def create_schedule(body: ScheduleCreate, _=Depends(mutating)):
        if not db.exists(body.session_id):
            raise HTTPException(404,'Sesi tidak ditemukan')
        try:
            return automations.create_schedule(body.session_id, body.name, body.cron,
                                                body.target, body.message, body.workflow_id, body.enabled)
        except AutomationError as exc:
            raise HTTPException(400,str(exc))

    @app.post('/api/schedules/{sid}/enabled')
    def enable_schedule(sid: str, body: ToggleRequest, _=Depends(mutating)):
        try:
            return automations.set_schedule_enabled(sid, body.enabled)
        except LookupError as exc:
            raise HTTPException(404,str(exc))

    @app.get('/api/integrations')
    def integrations(_=Depends(require_user)):
        return registry.list()

    @app.post('/api/integrations', status_code=201)
    def create_integration(body: IntegrationCreate, _=Depends(mutating)):
        try:
            return registry.register(body.manifest)
        except IntegrationError as exc:
            raise HTTPException(400,str(exc))
        except __import__('sqlite3').IntegrityError:
            raise HTTPException(409,'Nama integrasi sudah terdaftar')

    @app.post('/api/integrations/{iid}/enabled')
    def toggle_integration(iid: str, body: ToggleRequest, _=Depends(mutating)):
        try:
            registry.toggle(iid, body.enabled)
        except LookupError as exc:
            raise HTTPException(404,str(exc))
        return {'ok': True, 'execution_supported': False}

    @app.post('/api/browser/preview')
    async def browser_api(body: BrowserPreviewRequest, _=Depends(mutating)):
        if not cfg.browser_enabled:
            raise HTTPException(403,'Browser automation dinonaktifkan; aktifkan allowlist terlebih dahulu')
        try:
            return await browser_preview(body.url, cfg.browser_hosts)
        except BrowserError as exc:
            raise HTTPException(400,str(exc))
        except Exception as exc:
            db.audit('browser_failure', {'type': type(exc).__name__})
            raise HTTPException(502,'Browser gagal melakukan preview')

    # VPS Commander: inventory and planning are offline by default. SSH is explicitly
    # opted in by the operator and executes ONLY constant read-only probe scripts.
    @app.get('/api/vps/hosts')
    def vps_hosts(_=Depends(require_member)):
        return vps.hosts()

    @app.post('/api/vps/hosts',status_code=201)
    def vps_add_host(body: VPSHostCreate, _=Depends(mutating)):
        try:
            return vps.add_host(**body.model_dump(),enabled=False)
        except VPSException as exc:
            raise HTTPException(400,str(exc))

    @app.post('/api/vps/hosts/{host_id}/enabled')
    def vps_toggle_host(host_id: str, body: ToggleRequest, _=Depends(mutating)):
        try:
            return vps.set_enabled(host_id,body.enabled)
        except LookupError as exc:raise HTTPException(404,str(exc))
        except VPSException as exc:raise HTTPException(409,str(exc))

    @app.post('/api/vps/hosts/{host_id}/retire')
    def vps_retire_host(host_id: str, _=Depends(mutating)):
        try:
            vps.delete_host(host_id)
            return {'ok':True}
        except LookupError as exc:raise HTTPException(404,str(exc))

    @app.get('/api/vps/hosts/{host_id}/checks')
    def vps_history(host_id: str, _=Depends(require_member)):
        try:return vps.checks(host_id)
        except LookupError as exc:raise HTTPException(404,str(exc))

    @app.post('/api/vps/hosts/{host_id}/checks')
    def vps_run_check(host_id: str, body: VPSCheckRequest, _=Depends(operator_mutating)):
        try:
            return vps.check(host_id,body.kind,body.root)
        except LookupError as exc:raise HTTPException(404,str(exc))
        except VPSException as exc:raise HTTPException(400,str(exc))

    @app.get('/api/vps/hosts/{host_id}/plans')
    def vps_plans(host_id: str, _=Depends(require_user)):
        try:return vps.plans(host_id)
        except LookupError as exc:raise HTTPException(404,str(exc))

    @app.post('/api/vps/hosts/{host_id}/plans',status_code=201)
    def vps_create_plan(host_id: str, body: VPSPlanCreate, _=Depends(mutating)):
        try:return vps.create_plan(host_id,**body.model_dump())
        except LookupError as exc:raise HTTPException(404,str(exc))
        except VPSException as exc:raise HTTPException(400,str(exc))

    # --- v0.6: secondary accounts with narrowly scoped roles ---
    @app.get('/api/accounts')
    def accounts(_=Depends(require_user)):
        return access.accounts()

    @app.post('/api/accounts',status_code=201)
    def create_account(body: AccountCreate, _=Depends(mutating)):
        try:return access.create(**body.model_dump())
        except ValueError as exc:raise HTTPException(400,str(exc))

    @app.post('/api/accounts/{account_id}')
    def update_account(account_id: str, body: AccountChange, _=Depends(mutating)):
        try:return access.change(account_id,**body.model_dump(exclude_unset=True))
        except LookupError as exc:raise HTTPException(404,str(exc))
        except ValueError as exc:raise HTTPException(400,str(exc))

    # --- v0.6: monitoring + alarm lifecycle, read-only status for viewers ---
    @app.get('/api/operations/summary')
    def operations_summary(_=Depends(require_member)):
        return ops.summary()

    @app.get('/api/operations/monitors')
    def operations_monitors(_=Depends(require_member)):
        return ops.monitors()

    @app.post('/api/operations/monitors',status_code=201)
    def create_monitor(body: MonitorCreate, _=Depends(mutating)):
        try:return ops.create_monitor(**body.model_dump())
        except LookupError as exc:raise HTTPException(404,str(exc))
        except (OperationsError,VPSException) as exc:raise HTTPException(400,str(exc))

    @app.post('/api/operations/monitors/{monitor_id}/enabled')
    def enable_monitor(monitor_id: str,body: ToggleRequest,_=Depends(mutating)):
        try:return ops.set_enabled(monitor_id,body.enabled)
        except LookupError as exc:raise HTTPException(404,str(exc))
        except OperationsError as exc:raise HTTPException(409,str(exc))

    @app.get('/api/operations/alerts')
    def operations_alerts(_=Depends(require_member)):
        return ops.alerts()

    @app.post('/api/operations/alerts/{alert_id}/acknowledge')
    def acknowledge_alert(alert_id: str,_=Depends(operator_mutating)):
        try:return ops.acknowledge(alert_id)
        except LookupError as exc:raise HTTPException(404,str(exc))
        except OperationsError as exc:raise HTTPException(409,str(exc))

    @app.get('/api/operations/deliveries')
    def operations_deliveries(_=Depends(require_user)):
        return ops.deliveries()

    # --- v0.6: local workspace snapshots, never VPS/database restore ---
    @app.get('/api/snapshots')
    def list_snapshots(_=Depends(require_user)):
        return snapshots.list()

    @app.post('/api/snapshots',status_code=201)
    def create_snapshot(_=Depends(mutating)):
        try:return snapshots.create()
        except SnapshotError as exc:raise HTTPException(400,str(exc))

    @app.get('/api/snapshots/{identity}/preview')
    def preview_snapshot(identity: str,_=Depends(require_user)):
        try:return snapshots.preview(identity)
        except LookupError as exc:raise HTTPException(404,str(exc))
        except SnapshotError as exc:raise HTTPException(409,str(exc))

    @app.post('/api/snapshots/{identity}/restore')
    def restore_snapshot(identity: str,body: RestoreConfirm,_=Depends(mutating)):
        try:return snapshots.restore(identity,body.token,body.confirmation)
        except LookupError as exc:raise HTTPException(404,str(exc))
        except SnapshotError as exc:raise HTTPException(409,str(exc))

    @app.get("/api/metrics")
    def metrics(_=Depends(require_user)):
        return db.metrics()

    @app.get("/healthz")
    def healthz():
        # Do not expose model keys, workspace paths or detailed service topology.
        return {"status": "ok", "version": "0.6.0"}

    @app.post("/api/approvals/{approval_id}/decision")
    async def decision(approval_id: str, body: DecisionRequest = Body(...), _=Depends(mutating)):
        pending = db.approval(approval_id)
        if pending is None:
            raise HTTPException(404, "Approval tidak ditemukan")
        sid = pending["session_id"]
        async with locks[sid]:
            pending = db.approval(approval_id)
            if pending is None or not db.claim_approval(approval_id):
                raise HTTPException(409, "Approval sudah ditangani atau status tidak pasti")
            outcome = "rejected"
            if body.approved:
                try:
                    result = execute_approved(cfg, db, pending)
                    outcome = "approved"
                except (ToolError, OSError, ValueError) as exc:
                    result = f"TOOL ERROR: {exc}"
                    outcome = "failed"
                except Exception as exc:
                    # The approval is NOT retried automatically after an unknown error.
                    result = f"TOOL ERROR: operasi gagal ({type(exc).__name__}). Periksa hasil manual."
                    outcome = "failed"
            else:
                result = "DITOLAK oleh pengguna; tidak ada tindakan dilakukan."
            db.finalize_approval(approval_id, outcome, result)
            db.add_message(sid, "tool", result[:12000], tool_call_id=pending["tool_call_id"])
            if db.pending(sid):
                status = "approval_required"
            elif db.resume_after_approval(sid):
                worker.notify()
                status = "queued"
            else:
                status = await agent.run(sid)
            return {"status": status, **session_data(sid)}

    @app.post("/api/approvals/{approval_id}/acknowledge")
    async def acknowledge_unknown(approval_id: str, _=Depends(mutating)):
        item = db.approval(approval_id)
        if not item:
            raise HTTPException(404, "Approval tidak ditemukan")
        async with locks[item["session_id"]]:
            try:
                sid = db.acknowledge_unknown(approval_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc))
            if db.resume_after_approval(sid):
                worker.notify()
            return session_data(sid)

    @app.get("/api/memory")
    def memory(_=Depends(require_user)):
        return db.verified_memories(include_revoked=True)

    @app.get("/api/skills")
    def skills(_=Depends(require_user)):
        return db.list_skills()

    @app.get("/api/audit")
    def audit(_=Depends(require_user)):
        return {"verified": db.audit_verified(), "events": db.recent_audit()}

    return app
