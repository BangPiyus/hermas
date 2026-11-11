"""Environment configuration, with fail-closed defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_dir: Path
    data_dir: Path
    workspace: Path
    admin_password: str
    session_secret: str
    model_key: str
    model_url: str
    model_name: str
    model_provider: str
    allowed_hosts: tuple[str, ...]
    secure_cookie: bool
    docker_tests: bool
    max_steps: int
    max_output_tokens: int
    daily_token_budget: int
    browser_enabled: bool = False
    browser_hosts: tuple[str, ...] = ()
    telegram_enabled: bool = False
    telegram_token: str = ''
    telegram_chat_ids: tuple[int, ...] = ()
    telegram_allow_run: bool = False
    vps_ssh_enabled: bool = False
    vps_ssh_key_path: str = ""
    vps_known_hosts_path: str = ""
    vps_monitor_enabled: bool = False
    alert_telegram_enabled: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        app_dir = Path(__file__).resolve().parent.parent
        password = os.getenv("HERMAS_ADMIN_PASSWORD", "")
        secret = os.getenv("HERMAS_SESSION_SECRET", "")
        if len(password) < 12 or password.upper().startswith("CHANGE-ME"):
            raise RuntimeError("HERMAS_ADMIN_PASSWORD wajib minimal 12 karakter")
        if len(secret) < 32 or secret.upper().startswith("CHANGE-ME"):
            raise RuntimeError("HERMAS_SESSION_SECRET wajib minimal 32 karakter")
        workspace = Path(os.getenv("HERMAS_WORKSPACE", str(app_dir / "workspace"))).expanduser().resolve()
        data_dir = Path(os.getenv("HERMAS_DATA_DIR", str(app_dir / "data"))).expanduser().resolve()
        if workspace == data_dir or workspace in data_dir.parents or data_dir in workspace.parents:
            raise RuntimeError("HERMAS_WORKSPACE dan HERMAS_DATA_DIR harus terpisah")
        provider = os.getenv("HERMAS_MODEL_PROVIDER", "openai").lower().strip()
        if provider not in {"openai", "demo"}:
            raise RuntimeError("HERMAS_MODEL_PROVIDER harus 'openai' atau 'demo'")
        url = os.getenv("HERMAS_MODEL_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        if provider == "openai" and not url.startswith("https://"):
            if not (url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")):
                raise RuntimeError("Model API harus HTTPS atau localhost")
        if provider == "openai" and not os.getenv("HERMAS_MODEL_API_KEY", ""):
            raise RuntimeError("HERMAS_MODEL_API_KEY harus diisi untuk provider openai")
        hosts = tuple(h.strip() for h in os.getenv("HERMAS_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip())
        if "*" in hosts:
            raise RuntimeError("Host wildcard (*) tidak diizinkan")
        telegram_enabled = os.getenv('HERMAS_TELEGRAM_ENABLED', 'false').lower() == 'true'
        telegram_token = os.getenv('HERMAS_TELEGRAM_TOKEN', '')
        chat_ids_value = os.getenv('HERMAS_TELEGRAM_CHAT_IDS', '')
        try:
            chat_ids = tuple(int(v.strip()) for v in chat_ids_value.split(',') if v.strip())
        except ValueError as exc:
            raise RuntimeError('HERMAS_TELEGRAM_CHAT_IDS harus ID numerik') from exc
        if telegram_enabled and (not telegram_token or not chat_ids):
            raise RuntimeError('Telegram memerlukan token dan chat ID allowlist')
        browser_hosts = tuple(s.strip().lower() for s in os.getenv('HERMAS_BROWSER_HOSTS', '').split(',') if s.strip())
        if any('*' in name or '/' in name or ':' in name for name in browser_hosts):
            raise RuntimeError('HERMAS_BROWSER_HOSTS hanya menerima hostname eksplisit')
        browser_enabled = os.getenv('HERMAS_BROWSER_ENABLED', 'false').lower() == 'true'
        if browser_enabled and not browser_hosts:
            raise RuntimeError('HERMAS_BROWSER_HOSTS diperlukan jika browser aktif')
        if os.getenv('HERMAS_ALERT_TELEGRAM_ENABLED','false').lower() == 'true' and (not telegram_token or not chat_ids):
            raise RuntimeError('Alert Telegram memerlukan token dan chat ID allowlist')
        return cls(
            app_dir=app_dir, data_dir=data_dir, workspace=workspace,
            admin_password=password, session_secret=secret,
            model_key=os.getenv("HERMAS_MODEL_API_KEY", ""), model_url=url,
            model_name=os.getenv("HERMAS_MODEL", "gpt-4.1-mini"),
            model_provider=provider, allowed_hosts=hosts,
            secure_cookie=os.getenv("HERMAS_SECURE_COOKIE", "false").lower() == "true",
            docker_tests=os.getenv("HERMAS_ENABLE_DOCKER_TESTS", "false").lower() == "true",
            max_steps=min(max(int(os.getenv("HERMAS_MAX_STEPS", "6")), 1), 12),
            max_output_tokens=min(max(int(os.getenv("HERMAS_MAX_OUTPUT_TOKENS", "1200")), 100), 4096),
            daily_token_budget=max(1000, int(os.getenv("HERMAS_DAILY_TOKEN_BUDGET", "40000"))),
            browser_enabled=browser_enabled, browser_hosts=browser_hosts,
            telegram_enabled=telegram_enabled, telegram_token=telegram_token,
            telegram_chat_ids=chat_ids,
            telegram_allow_run=os.getenv('HERMAS_TELEGRAM_ALLOW_RUN', 'false').lower() == 'true',
            vps_ssh_enabled=os.getenv('HERMAS_VPS_SSH_ENABLED', 'false').lower() == 'true',
            vps_ssh_key_path=os.getenv('HERMAS_VPS_SSH_KEY_PATH', '').strip(),
            vps_known_hosts_path=os.getenv('HERMAS_VPS_KNOWN_HOSTS_PATH', '').strip(),
            vps_monitor_enabled=os.getenv('HERMAS_VPS_MONITOR_ENABLED','false').lower() == 'true',
            alert_telegram_enabled=os.getenv('HERMAS_ALERT_TELEGRAM_ENABLED','false').lower() == 'true',
        )
