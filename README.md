# Hermas Agentic AI v0.6.0

**Self-hosted AI agent MVP** for coding, orchestrated review, automation, and **opt-in read-only** VPS/WordPress operations. Built in Python + FastAPI + SQLite, with local Tailwind CSS and Feather-style SVG icons. This is an independent project, **not** a fork of Nous Research Hermes Agent. It does not yet match Hermes Agent's full capabilities and has **not been certified for unattended production use**.

## Capabilities by version

| Version | Capabilities | Important limits |
|---|---|---|
| v0.1 | Chat/demo, approved file modifications, memory, skills, authenticated dashboard | Bootstrap admin, limited tools |
| v0.2 | Persistent task queue, approval states, audit hash chain, cancellation & metrics | One process; interrupted writes require human review |
| v0.3 | Planner/Coder/Reviewer, verified memory, local Git Coding Studio | Proposals/review only; no autonomous commit from orchestration |
| v0.4 | Workflow builder, UTC cron, Telegram polling, MCP manifests, Playwright page preview | MCP metadata-only; browser read-only |
| v0.5 | VPS inventory, SSH health checks, WordPress metadata audits, manual backup/deploy plans | SSH opt-in and read-only; no remote restore or deploy |
| **v0.6** | **RBAC viewer/operator, scheduled VPS monitoring, deduplicated alert lifecycle, optional Telegram alerts, local source snapshots and reviewed restore** | **Monitoring opt-in; snapshots apply to local workspace only** |

### New in 0.6

**Accounts & access:** main admin credential stays in `.env`. Admin can create `viewer` and `operator` accounts. Secondary accounts use scrypt+salt password hashes and revocable signed session cookies. `viewer` can read the Operations dashboard and VPS inventory; `operator` can additionally acknowledge alerts and request enabled read-only VPS checks. **Only the primary administrator** can access chat, tasks, files, Git, memory, approvals, schedules, integrations, database audit, account management and restore. Existing endpoints retain admin-only access. Roles cannot create another admin account.

**VPS scheduled monitoring:** per-host `health` or `wordpress` monitor with 5–1440-minute interval, default disabled. Requires all of `HERMAS_VPS_SSH_ENABLED=true`, `HERMAS_VPS_MONITOR_ENABLED=true`, checked SSH credentials and a separately enabled host. Checks use the **existing fixed read-only SSH probes**. At most one due monitor is claimed per cycle, missed checks are coalesced, and interrupted scans are explicitly marked rather than reported successful. There is no auto-remediation, remote write, or general SSH shell.

**Alerts:** persistent `open → acknowledged` or `resolved` lifecycle; repeat failures for the same host/type increment a counter instead of generating a flood. The scheduler records errors **without raw SSH stderr**. If `HERMAS_ALERT_TELEGRAM_ENABLED=true` and token/chat allowlist are configured, new alert events may send a sanitized message **without hostname, IP, server path, or file contents**. Telegram failures/timeouts become delivery `unknown`, **not** automatically resent. Alerts recorded before opt-in are not retrospectively sent. Single-host/type deduplication is coarse and cannot distinguish two concurrent issues of the same type.

**Workspace snapshots:** snapshot a **filtered subset of source files** in the Hermas workspace (max 200 files/32 MiB aggregate; max 4 MiB per file). Reject symlinks, hidden paths, known secret-like names, oversized files and path traversal. Store SHA256 manifest and archived bytes outside the workspace in the Hermas data directory. Preview restore shows `create / overwrite / unchanged`, issues a signed token valid 10 minutes, detects changes between preview and execution, requires the exact confirmation `RESTORE`, and performs per-file atomic replacement with best-effort rollback. **A multi-file restore is not atomic**. Any code files containing secrets under non-obvious names can still be backed up; keep `data/` private. **Not** a VPS snapshot, database backup, or remote WordPress restore.

**Observability:** operations summary, recent monitors, alert lifecycle and delivery list, plus existing task metrics, usage and audit hash-chain verification. No alert or monitoring telemetry is sent to external services unless separately enabled.

## Installation

Requirements: Python **3.11+**, pip; optional Git, OpenSSH for VPS, Docker for isolated code tests, Playwright for browser preview. Windows 11/Linux/macOS.

### Windows

Extract `Hermas-Agentic-AI-v0.6.0.zip`, open `hermas-agentic-ai`, run `start.bat`. Copy the initial random administrator password printed in the terminal. Go to **http://127.0.0.1:8765**.

### Linux

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git openssh-client unzip
unzip Hermas-Agentic-AI-v0.6.0.zip
cd hermas-agentic-ai
chmod +x start.sh
./start.sh
```

Local demo runs without any AI API key, SSH key or Telegram bot. The first-run setup generates `.env` with a unique password and secret. To use an actual provider, set `HERMAS_MODEL_PROVIDER=openai`, `HERMAS_MODEL_API_KEY`, and a compatible HTTPS endpoint. Keep the application bound to `127.0.0.1`; use an SSH tunnel for remote administration.

## Upgrade from any v0.1–v0.5 release

1. **Stop the old process/workers** and back up old `.env`, `workspace/`, and all `data/` files **after stopping** (SQLite + WAL/SHM included). Verify the backup.
2. Extract v0.6 into a **new** directory, not over the existing folder.
3. Copy your `.env`, `data/` and `workspace/` into the new folder. Fix absolute paths in `.env` if needed.
4. Start v0.6. SQLite migrates automatically to `PRAGMA user_version=6`; old sessions, chats, tasks, approvals, workflows, memory and VPS inventory are retained.
5. Review scheduled tasks and approvals before enabling remote monitoring. New monitors and alert delivery remain off by default.
6. After confirming successful operation, archive the old installation separately.

Do **not** downgrade and point an older Hermas version at a database migrated to v0.6.

## v0.6 environment options (all default false)

```dotenv
HERMAS_VPS_SSH_ENABLED=false
HERMAS_VPS_SSH_KEY_PATH=
HERMAS_VPS_KNOWN_HOSTS_PATH=
HERMAS_VPS_MONITOR_ENABLED=false
HERMAS_ALERT_TELEGRAM_ENABLED=false
HERMAS_TELEGRAM_ENABLED=false
HERMAS_TELEGRAM_TOKEN=
HERMAS_TELEGRAM_CHAT_IDS=
```

Alert Telegram can be enabled independently of Telegram command polling. The token is kept outside SQLite, in `.env`; never commit it. Both alert delivery and polling, if enabled, are restricted to the configured numeric chat ID allowlist. Verify SSH host keys **out of band**, keep key file permissions restricted (0600 on Unix), use a non-root SSH account and start with staging servers. See **PANDUAN-INDONESIA.md** for detailed steps.

## v0.6 API endpoints

All endpoints below require signed login session cookies; POST requires `X-Hermas-CSRF`. Existing v0.1–v0.5 API paths remain. Access classes: **Admin**, **Operator**, **Viewer**.

| Method | Endpoint | Minimum access | Action |
|---|---|---|---|
| GET | `/api/me` | Viewer | Current role, flags, and CSRF token |
| GET | `/api/accounts` | Admin | Secondary account list (no password hashes) |
| POST | `/api/accounts` | Admin | Create viewer/operator |
| POST | `/api/accounts/{id}` | Admin | Change role, password, enabled status; revoke sessions |
| GET | `/api/operations/summary` | Viewer | Operations overview |
| GET | `/api/operations/monitors` | Viewer | Saved monitors and results |
| POST | `/api/operations/monitors` | Admin | Define disabled monitoring job |
| POST | `/api/operations/monitors/{id}/enabled` | Admin | Enable only when SSH/config/host ready |
| GET | `/api/operations/alerts` | Viewer | Alert history |
| POST | `/api/operations/alerts/{id}/acknowledge` | Operator | Acknowledge an open alert |
| GET | `/api/operations/deliveries` | Admin | Telegram delivery status (including `unknown`) |
| GET | `/api/snapshots` | Admin | List local source snapshots |
| POST | `/api/snapshots` | Admin | Create snapshot |
| GET | `/api/snapshots/{id}/preview` | Admin | Signed preview, current file comparison |
| POST | `/api/snapshots/{id}/restore` | Admin | Requires token + `confirmation:"RESTORE"` |

`POST /api/login` accepts `{"username":"admin","password":"..."}` or a secondary username. The default username for legacy logins is `admin`.

Example create monitor (default disabled):

```json
{"host_id":"<ID from VPS inventory>","kind":"health","interval_minutes":30,"root":""}
```

Example create account:

```json
{"username":"viewer01","password":"use-a-long-unique-password","role":"viewer"}
```

## Security / known limitations

- **Do not deploy as root** or expose port 8765 directly to the public Internet. Use HTTPS reverse proxy and network access control only after separate security review.
- Viewer/operator account handling is **scoped RBAC**, **not a complete multi-tenant isolation model**. Existing chats and code APIs are admin-only.
- Bootstrap administrator remains a single `.env` secret. Login lockout is in-memory/per-process and not durable across restart.
- Scheduled monitoring is read-only but still performs external SSH calls and can increase server load. Initial intervals should be conservative.
- Telegram sends are at-most-one automatic attempt. An ambiguous result may have delivered successfully; it is marked `unknown` for manual review.
- Snapshot does not capture hidden paths, all extension types or databases and uses filename filtering (not comprehensive secret scanning). A restore cannot guarantee an atomic multi-file transaction or protection against a privileged local attacker racing the filesystem.
- No production backup/restore/deploy executor, no real fleet-wide secret management, no HA/multi-process coordination, no enterprise SSO, no comprehensive WordPress malware scan, no live-model/remote-SSH/Telegram E2E validation.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q hermas
node --check static/app.js # optional; Node.js installed
```

All repository tests are offline/mocked. See `CHANGELOG.md` and `PANDUAN-INDONESIA.md`. License in `LICENSE`.

## GitHub and community contributions

For the public repository release, see [GitHub Publishing Guide](docs/GITHUB-PUBLISHING.md), [Contributing](CONTRIBUTING.md), [Security Policy](SECURITY.md), and [Code of Conduct](CODE_OF_CONDUCT.md). The project is distributed under the existing MIT [LICENSE](LICENSE). Contributions use fork + pull request; review and CI checks are expected before merging. Do **not** commit `.env`, databases, tokens, SSH keys, or private source from `workspace/`. This is an experimental MVP, not a production-ready managed VPS service.
