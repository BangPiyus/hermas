"""Restricted, audited tools. No general host shell access."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .config import Settings
from .store import Store

SENSITIVE = {".git", ".ssh", ".venv", "node_modules", "vendor", "__pycache__", ".hermas", "data", "secrets"}
SENSITIVE_ENDINGS = (".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3")
SENSITIVE_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json", "wp-config.php", "config.php"}
ALLOWED_WRITE_EXT = {".py", ".php", ".js", ".ts", ".html", ".css", ".json", ".md", ".txt", ".yml", ".yaml", ".sql"}

TOOL_DEFINITIONS = [
    {"type": "function", "function": {"name": "list_files", "description": "List a directory inside the configured workspace, read-only.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative directory, default ."}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read source code from a permitted workspace file (never credentials).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_text", "description": "Search a literal string in permitted workspace files.",
        "parameters": {"type": "object", "properties": {"needle": {"type": "string"}, "path": {"type": "string"}}, "required": ["needle"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_memory", "description": "Search approved persistent facts from prior conversations.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "write_file", "description": "PROPOSAL ONLY. Create or replace a source file. Requires human approval. Never edit credentials.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "run_tests", "description": "PROPOSAL ONLY. Run an allowlisted Python unittest discovery or PHP lint in a locked-down Docker container. Requires approval and enabled runner.",
        "parameters": {"type": "object", "properties": {"language": {"type": "string", "enum": ["python", "php"]}, "path": {"type": "string", "description": "PHP file relative path; unused for python"}}, "required": ["language"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "save_memory", "description": "PROPOSAL ONLY. Store a concise durable fact for future sessions; requires human approval.",
        "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "git_status", "description": "Read-only Git status for workspace root repo.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "git_diff", "description": "Read-only unified diff of a permitted source file in workspace Git repository.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "git_create_branch", "description": "PROPOSAL ONLY. Create new hermas/* Git branch after human approval; requires clean repo.",
        "parameters": {"type": "object", "properties": {"branch": {"type": "string"}}, "required": ["branch"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "git_commit", "description": "PROPOSAL ONLY. Commit explicitly named source files in hermas/* branch after human approval and full diff review.",
        "parameters": {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}, "message": {"type": "string"}}, "required": ["paths", "message"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "recall_verified_memory", "description": "Read-only recall of previously verified, non-expired memory facts with provenance.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "project": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "save_verified_memory", "description": "PROPOSAL ONLY. Save a fact with project, supporting evidence, confidence and optional expiry; requires human approval.",
        "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}, "project": {"type": "string"}, "evidence": {"type": "string"}, "confidence": {"type": "number"}, "expires_at": {"type": "string"}}, "required": ["key", "value", "project", "evidence", "confidence"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "revoke_verified_memory", "description": "PROPOSAL ONLY. Revoke a verified memory by ID; requires human approval.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "save_skill", "description": "PROPOSAL ONLY. Store a reviewed Markdown procedure, not executable code. Requires human approval.",
        "parameters": {"type": "object", "properties": {"slug": {"type": "string"}, "content": {"type": "string"}}, "required": ["slug", "content"], "additionalProperties": False}}},
]

WRITE_TOOLS = {"write_file", "run_tests", "save_memory", "save_skill", "save_verified_memory", "revoke_verified_memory", "git_create_branch", "git_commit"}


class ToolError(Exception):
    pass


def _path(cfg: Settings, name: str, is_file: bool = False, writing: bool = False) -> Path:
    if not isinstance(name, str) or not name or len(name) > 200:
        raise ToolError("Path tidak valid")
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts:
        raise ToolError("Path harus relatif dan tidak boleh traversal")
    for segment in rel.parts:
        lower = segment.lower()
        if lower in SENSITIVE or lower in SENSITIVE_NAMES or lower.startswith(".env") or lower.endswith(SENSITIVE_ENDINGS):
            raise ToolError("Akses file rahasia atau sistem ditolak")
    root = cfg.workspace.resolve()
    # Reject symlinks even when they point *inside* the workspace. An attacker
    # should not be able to quietly redirect a reviewed write to another file.
    raw = root
    for component in rel.parts:
        raw = raw / component
        if raw.is_symlink():
            raise ToolError("Symlink dalam path tidak diizinkan")
    candidate = raw.resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError("Path keluar dari workspace")
    if is_file and candidate.exists() and (not candidate.is_file() or candidate.is_symlink()):
        raise ToolError("Bukan file reguler")
    if writing:
        if candidate.suffix.lower() not in ALLOWED_WRITE_EXT:
            raise ToolError("Ekstensi tidak diizinkan untuk ditulis")
        if not candidate.parent.is_dir() or not candidate.parent.resolve().is_relative_to(root):
            raise ToolError("Parent directory belum ada atau tidak aman")
    return candidate


def digest_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_verified_memory(args: dict) -> None:
    from datetime import datetime, timezone
    key, value = args.get("key"), args.get("value")
    project, evidence = args.get("project"), args.get("evidence")
    score, expires = args.get("confidence"), args.get("expires_at")
    if not isinstance(key,str) or not 1 <= len(key) <= 80 or not isinstance(value,str) or not 1 <= len(value) <= 500:
        raise ToolError("Key/value memory tidak valid")
    if not isinstance(project,str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,60}", project):
        raise ToolError("Project memory tidak valid")
    if not isinstance(evidence,str) or not 8 <= len(evidence) <= 300:
        raise ToolError("Sumber/evidence wajib 8-300 karakter")
    if isinstance(score,bool) or not isinstance(score,(int,float)) or not 0 <= score <= 1:
        raise ToolError("Confidence wajib antara 0 dan 1")
    combined = " ".join((key,value,evidence)).lower()
    if re.search(r"(-----begin .*private key-----|sk-[a-z0-9_-]{12,}|(password|api[_ -]?key|secret|token)\s*[:=]\s*\S+)", combined):
        raise ToolError("Jangan menyimpan password, API key atau rahasia sebagai memori")
    if expires is not None:
        if not isinstance(expires,str) or len(expires) > 35:
            raise ToolError("Format expiry tidak valid")
        try:
            dt=datetime.fromisoformat(expires)
        except ValueError as exc:
            raise ToolError("Expiry harus ISO-8601 dengan zona waktu UTC") from exc
        if dt.tzinfo is None or dt.utcoffset().total_seconds()!=0 or dt <= datetime.now(timezone.utc):
            raise ToolError("Expiry harus timestamp UTC di masa depan")


def _human_preview(cfg: Settings, name: str, args: dict) -> tuple[str, str | None]:
    if name == "write_file":
        dest = _path(cfg, args.get("path", ""), is_file=True, writing=True)
        content = args.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > 30000:
            raise ToolError("Isi file harus UTF-8 dan maksimal 30 KB")
        if dest.exists() and dest.stat().st_size > 250000:
            raise ToolError("File lama melebihi 250 KB; edit otomatis ditolak")
        old = dest.read_text("utf-8", errors="replace") if dest.exists() else ""
        diff = "".join(list(difflib.unified_diff(old.splitlines(keepends=True), content.splitlines(keepends=True),
                          fromfile=f"sebelum/{args['path']}", tofile=f"sesudah/{args['path']}")))
        if len(diff.encode("utf-8")) > 64000:
            raise ToolError("Diff melebihi 64 KB; pecah perubahan agar seluruh diff dapat direview")
        return (diff or "File tidak berubah"), digest_file(dest)
    if name == "run_tests":
        if args.get("language") not in {"python", "php"}:
            raise ToolError("Bahasa test tidak didukung")
        if args.get("language") == "php":
            src = _path(cfg, args.get("path", ""), is_file=True)
            if not src.is_file() or src.suffix.lower() != ".php":
                raise ToolError("PHP lint memerlukan file .php yang ada")
        return f"Menjalankan pengujian {args['language']} di Docker sandbox, network off, filesystem read-only.", None
    if name == "save_memory":
        if not isinstance(args.get("key"), str) or not isinstance(args.get("value"), str) or not 1 <= len(args["key"]) <= 80 or not 1 <= len(args["value"]) <= 500:
            raise ToolError("Memory wajib punya key (1-80) dan value (1-500)")
        return f"Simpan fakta (harus diverifikasi pengguna): {args['key']}\n{args['value']}", None
    if name in {"git_create_branch", "git_commit"}:
        from .git_studio import proposal as git_proposal
        return git_proposal(cfg, name, args)
    if name == "save_verified_memory":
        _validate_verified_memory(args)
        return (f"Verified memory / {args['project']} / {args['key']}\n"
                f"Fakta: {args['value']}\nBukti: {args['evidence']}\n"
                f"Confidence: {args['confidence']}\nExpiry: {args.get('expires_at') or 'tidak ada'}", None)
    if name == "revoke_verified_memory":
        identity = args.get("id", "")
        if not isinstance(identity, str) or len(identity) > 100:
            raise ToolError("ID memory tidak valid")
        return f"Nonaktifkan memory terverifikasi: {identity}", None
    if name == "save_skill":
        if not isinstance(args.get("slug"), str) or not re.fullmatch(r"[a-z0-9-]{1,40}", args["slug"]):
            raise ToolError("Slug skill tidak valid")
        if not isinstance(args.get("content"), str) or not 1 <= len(args["content"]) <= 4000:
            raise ToolError("Isi skill harus 1-4000 karakter")
        return f"Skill: {args['slug']}\n\n{args['content'][:8000]}", None
    raise ToolError("Tool tidak dikenal")


def proposal(cfg: Settings, name: str, args: dict) -> tuple[str, str | None]:
    return _human_preview(cfg, name, args)


def read_tool(cfg: Settings, db: Store, name: str, args: dict) -> str:
    if name == "list_files":
        p = _path(cfg, args.get("path") or ".")
        if not p.is_dir():
            raise ToolError("Folder tidak ditemukan")
        items = []
        for item in sorted(p.iterdir(), key=lambda x: x.name.casefold()):
            if item.is_symlink():
                continue
            try:
                _path(cfg, str(item.relative_to(cfg.workspace)))
            except ToolError:
                continue
            items.append({"name": item.name, "type": "dir" if item.is_dir() else "file"})
            if len(items) >= 100:
                break
        return json.dumps(items, ensure_ascii=False)
    if name == "read_file":
        p = _path(cfg, args.get("path", ""), is_file=True)
        if not p.is_file() or p.stat().st_size > 250000:
            raise ToolError("File tidak ditemukan atau melebihi 250 KB")
        data = p.read_bytes()[:16000]
        if b"\x00" in data:
            raise ToolError("File biner tidak didukung")
        return data.decode("utf-8", errors="replace") + ("\n[TRUNCATED]" if p.stat().st_size > 16000 else "")
    if name == "search_text":
        needle = args.get("needle", "")
        if not isinstance(needle, str) or not 1 <= len(needle) <= 120:
            raise ToolError("Pencarian tidak valid")
        base = _path(cfg, args.get("path") or ".")
        roots = [base] if base.is_file() else base.rglob("*")
        matches = []
        files_checked = 0
        for file in roots:
            if len(matches) >= 40 or files_checked >= 300:
                break
            try:
                relative = str(file.relative_to(cfg.workspace))
                _path(cfg, relative, is_file=True)
                if not file.is_file() or file.is_symlink() or file.stat().st_size > 200000:
                    continue
                files_checked += 1
                lines = file.read_text("utf-8", errors="replace").splitlines()
                for idx, line in enumerate(lines, 1):
                    if needle.casefold() in line.casefold():
                        matches.append({"path": relative, "line": idx, "text": line[:240]})
                        if len(matches) >= 40:
                            break
            except (OSError, UnicodeError, ToolError, ValueError):
                continue
        return json.dumps(matches, ensure_ascii=False)
    if name == "git_status":
        from .git_studio import status
        return status(cfg)
    if name == "git_diff":
        from .git_studio import diff
        return diff(cfg, args.get("path", ""))
    if name == "recall_verified_memory":
        query = args.get("query", "")
        project = args.get("project")
        if not isinstance(query, str) or not 1 <= len(query) <= 120:
            raise ToolError("Query tidak valid")
        if project is not None and (not isinstance(project, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,60}",project)):
            raise ToolError("Project memory tidak valid")
        return json.dumps(db.relevant_memories(query, project), ensure_ascii=False)
    if name == "search_memory":
        query = args.get("query", "")
        if not isinstance(query, str) or not 1 <= len(query) <= 120:
            raise ToolError("Kata kunci memory tidak valid")
        return json.dumps(db.search_memories(query), ensure_ascii=False)
    raise ToolError("Tool tidak dikenal atau memerlukan approval")


def _make_safe_snapshot(cfg: Settings, output: Path) -> int:
    """Build a bounded source-only sandbox snapshot; do not mount full workspace.

    Excludes known secret names, symlinks, non-source and oversized files.
    This is defense in depth, not a complete secret scanner.
    """
    count = 0
    size = 0
    root = cfg.workspace.resolve()
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        current = Path(dirpath)
        safe_dirs = []
        for dirname in dirs:
            relative = (current / dirname).relative_to(root)
            try:
                _path(cfg, str(relative))
                if not (current / dirname).is_symlink():
                    safe_dirs.append(dirname)
            except ToolError:
                continue
        dirs[:] = safe_dirs
        for filename in files:
            source = current / filename
            relative = source.relative_to(root)
            try:
                checked = _path(cfg, str(relative), is_file=True)
            except ToolError:
                continue
            if checked.is_symlink() or not checked.is_file() or checked.suffix.lower() not in ALLOWED_WRITE_EXT:
                continue
            length = checked.stat().st_size
            if length > 250000:
                continue
            count += 1
            size += length
            if count > 500 or size > 5_000_000:
                raise ToolError("Snapshot sandbox terlalu besar (maksimum 500 file / 5 MB)")
            dest = output / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(checked, dest)
    return count


def _run_docker(cfg: Settings, lang: str, relfile: str | None) -> str:
    if not cfg.docker_tests:
        raise ToolError("Runner Docker nonaktif. Set HERMAS_ENABLE_DOCKER_TESTS=true pada host yang diisolasi.")
    with tempfile.TemporaryDirectory(prefix="hermas-sandbox-") as directory:
        snapshot = Path(directory)
        count = _make_safe_snapshot(cfg, snapshot)
        # Sandbox runs as UID 65534; give it read-only access to staged source.
        snapshot.chmod(0o755)
        for staged in snapshot.rglob("*"):
            staged.chmod(0o755 if staged.is_dir() else 0o644)
        if lang == "python" and not list(snapshot.rglob("test_*.py")):
            raise ToolError("Tidak ada test_*.py yang aman di workspace")
        if lang == "php" and not (snapshot / relfile).is_file():
            raise ToolError("File PHP tidak ada dalam snapshot sandbox")
        base = ["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only", "--pids-limit=64",
                "--memory=256m", "--cpus=1", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--user=65534:65534", "--tmpfs", "/tmp:rw,nosuid,noexec,size=32m",
                "--mount", f"type=bind,src={snapshot},dst=/workspace,readonly", "--workdir", "/workspace",
                "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1"]
        if lang == "python":
            cmd = base + ["python:3.12-alpine", "python", "-B", "-m", "unittest", "discover", "-s", "/workspace", "-p", "test_*.py"]
        else:
            cmd = base + ["php:8.3-cli-alpine", "php", "-l", "/workspace/" + relfile]
        try:
            output = subprocess.run(cmd, capture_output=True, text=True, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolError(f"Runner Docker gagal: {type(exc).__name__}") from exc
        return json.dumps({"exit_code": output.returncode, "stdout": output.stdout[:5000],
                           "stderr": output.stderr[:3000], "snapshot_files": count}, ensure_ascii=False)


def execute_approved(cfg: Settings, db: Store, pending: dict) -> str:
    args = json.loads(pending["arguments_json"])
    name = pending["tool_name"]
    # Revalidate the exact proposal at execution time, not just at request time.
    proposal(cfg, name, args)
    if name == "write_file":
        dest = _path(cfg, args["path"], is_file=True, writing=True)
        if digest_file(dest) != pending["expected_sha256"]:
            raise ToolError("File telah berubah sejak proposal dibuat. Buat proposal baru.")
        backup_name = None
        if dest.exists():
            backup_folder = cfg.data_dir / "backups"
            backup_folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup_name = pending["id"] + ".bak"
            backup = backup_folder / backup_name
            backup.write_bytes(dest.read_bytes())
            backup.chmod(0o600)
        fd, temp_name = tempfile.mkstemp(prefix=".hermas-", dir=str(dest.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(args["content"])
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp_name, 0o644)
            os.replace(temp_name, dest)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return f"File '{args['path']}' berhasil ditulis. Backup: {backup_name or 'file baru'}."
    if name == "run_tests":
        rel = args.get("path") if args["language"] == "php" else None
        return _run_docker(cfg, args["language"], rel)
    if name in {"git_create_branch", "git_commit"}:
        from .git_studio import execute as git_execute
        return git_execute(cfg, name, args, pending.get("expected_sha256"))
    if name == "save_verified_memory":
        _validate_verified_memory(args)
        row = db.save_verified_memory(args['key'],args['value'],pending['session_id'],
                                      args['project'],args['evidence'],float(args['confidence']),args.get('expires_at'))
        return f"Verified memory tersimpan: {row['id']}"
    if name == "revoke_verified_memory":
        if not db.revoke_verified_memory(args['id']):
            raise ToolError("Memory sudah dicabut atau tidak ditemukan")
        return "Memory dicabut dari konteks AI."
    if name == "save_memory":
        db.set_memory(args["key"], args["value"], pending["session_id"])
        db.save_verified_memory(args["key"], args["value"], pending["session_id"],
                                "global", "Disetujui administrator dari chat", 0.70)
        return "Memory tersimpan setelah persetujuan."
    if name == "save_skill":
        db.set_skill(args["slug"], args["content"], pending["session_id"])
        return "Skill tersimpan setelah persetujuan."
    raise ToolError("Tool tidak dikenal")
