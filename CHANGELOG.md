# Changelog

## v0.6.0 — Production Hardening & Intelligent Operations (2026-11-09)

- SQLite migration `user_version=6`, preserving v0.1–v0.5 data.
- Secondary local accounts (viewer/operator) with scrypt salted password hashes, revocable signed sessions, explicit least-privilege read/acknowledge routes, admin-only legacy AI/code APIs.
- Read-only VPS monitoring scheduler: per-host interval 5–1440 minutes, disabled by default, explicit opt-in and SSH config verification, skip missed slots, status interruption recovery.
- Deduplicated operations alert lifecycle (open/acknowledged/resolved), persistent delivery state, redacted Telegram notifications opt-in; ambiguous sends marked unknown rather than silently retried.
- Local source-workspace snapshots with symlink/path/size checks, SHA256 manifest, ZIP integrity verification, signed restore preview (10 minutes), stale-state rejection, typed RESTORE confirmation, per-file atomic replace with best-effort rollback.
- Dashboard panels for operations/alerts, accounts and workspace snapshot controls; local precompiled Tailwind CSS + SVG icons.
- Security unit tests, backwards compatibility and package integrity verification.
- Limits: NOT a remote backup/restore/deployment solution, NOT general security scanner, NOT production-ready multi-tenant infrastructure. Scheduled scans need staging SSH tests before production.


## v0.5.0 — VPS & WordPress Intelligence (2026-11-09)

- VPS inventory with SSH user/host/port, default-disabled hosts, archival without deleting history.
- Opt-in read-only OpenSSH with pinned known_hosts, dedicated private key on host, no root SSH, no arbitrary remote shell/agent tool.
- Fixed Python remote health probes (CPU, memory, disk, uptime, systemd service states), bounded output and concurrent scans; simple health review thresholds.
- WordPress metadata audit of structure, wp-config permissions, plugin/theme names and executable PHP extensions in uploads (heuristics only, no contents/database reads).
- Persistent scan history, append-only audit events, and safe archive behavior.
- Draft-only backup/deployment checklists with no remote execution.
- VPS Commander dashboard, authenticated endpoints, SQLite schema migration 4 → 5.
- Offline security and API tests; real SSH, Telegram, Docker and model E2E not yet tested.

## v0.4.0 — Automation & Integration (2026-11-09)

- UTC cron scheduler, missed-run coalescing, automatic skips for busy sessions, default disabled.
- Persistent serial workflow execution (chat/orchestrate), stage checkpoints, cancel and pause.
- Dashboard Visual Workflow Builder, Scheduler and integrations panel, Browser Preview panel.
- Telegram gateway optional with allowlisted chat IDs and opt-in workflow runs.
- Declarative MCP/plugin manifest registry; no plugin execution.
- Opt-in read-only Playwright page preview with public hostname allowlist and JS disabled.
- SQLite migrations from 3 to 4, new API endpoints, audit events, regression and security tests.

# Changelog

## 0.3.0 — Intelligence & Coding

- Three independent model role calls for Planner/Coder/Reviewer with SQLite step checkpoints and manual task retry. No automatic code changes from multi-agent mode.
- Verified Memory v3: project namespace, evidence, confidence, expiry, revocation, human approval, legacy migration, and keyword recall. Revoked/expired data excluded from old memory search as well.
- Local Git Coding Studio: safe status/diff inspection, approval-gated `hermas/*` branch creation and scoped commits with SHA state validation; refusal for unsafe Git attributes/configuration. No remote Git or shell access.
- Dashboard toggle for multi-agent, stages viewer, Git status panel and memory metadata.
- Database schema user_version 3; upgrade from v0.2 without deleting existing tasks, approvals, or sessions.
- New offline tests for security, Git, checkpointing, cancellations, memory validation and migration; 49 passing total.
- Remaining limitations: single admin and one worker, no E2E live provider/browser/Docker runs, no vector memory, no automatic patch application from multi-agent output.


## [0.2.0] - 2026-11-09

### Added
- Persistent SQLite task queue, task state/event tables and idempotency keys.
- Async worker, atomic claiming, manual resume, cooperative cancellation and global queue cap.
- Startup interrupted state instead of blind automatic replays.
- Atomic approval claim; unknown result state after mid-execution crash; manual acknowledgement.
- Single-instance cross-process file lock around the app lifespan.
- Risk labels, task explorer, metrics dashboard, polling and authenticated operational API.
- Per-model-call duration/usage audit and aggregate task metrics.
- Source-only Docker test snapshot excluding known secret paths and symlinks.
- v0.2 tests and upgrade instructions from 0.1.

### Hardened
- Complete diff required before approval (overly large diffs are rejected, not truncated).
- Symlink checks for paths, including links within the workspace.
- Canceled tasks revoke pending approval requests, including cancellation races.
- Maximum original file size for automatic write proposals.

### Limits
- Single-process queue; no multi-node scheduler or distributed lease service.
- Crash recovery requires manual review; no exactly-once guarantees for provider calls.
- Docker host privileges and incomplete secret detection remain relevant risks.
- No general host-shell execution or direct production SSH tasks.

## [0.1.0] - 2026-11-07

- Original independent coding-agent MVP with FastAPI auth, SQLite sessions/memory/skills,
  bounded LLM tool-calling, approval-gated writes/tests, local Tailwind + Feather UI and hash-chain audit.
