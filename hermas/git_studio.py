"""Constrained local Git studio: no shell, no network, approval for mutations.

This is NOT a general git execution feature. Repositories are treated as
potentially untrusted: hooks, external diff, global git config and SSH prompts
are disabled; commits are refused for repositories with repository attributes
or filter configuration that could launch programs from a host.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from .config import Settings
from .tools import ToolError, _path, ALLOWED_WRITE_EXT, digest_file


BRANCH_RE = re.compile(r"hermas/[a-z0-9][a-z0-9._-]{0,48}$")


def _git(cfg: Settings, *args: str, timeout: int = 12) -> str:
    environment = {**{k:v for k,v in os.environ.items() if not k.startswith("GIT_")},
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GIT_SSH_COMMAND": "false",
                   "GIT_PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0"}
    command = ["git", "-C", str(cfg.workspace.resolve()),
               "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
               "-c", "diff.external=", "-c", "commit.gpgsign=false",
               "-c", "core.pager=cat", *args]
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, timeout=timeout, env=environment, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"Git tidak tersedia atau timeout: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        raise ToolError(f"Operasi Git ditolak (exit {completed.returncode}): {(completed.stderr or completed.stdout)[:260]}")
    return completed.stdout[:30000]


def _root(cfg: Settings) -> None:
    root = _git(cfg, "rev-parse", "--show-toplevel").strip()
    if Path(root).resolve() != cfg.workspace.resolve():
        raise ToolError("Root repo Git harus tepat sama dengan HERMAS_WORKSPACE")
    # Require an ordinary local checkout: reject git worktrees/submodules or
    # redirection to an outside Git directory for host-side Git operations.
    local_git = cfg.workspace.resolve() / ".git"
    if not local_git.is_dir() or local_git.is_symlink() or local_git.resolve().parent != cfg.workspace.resolve():
        raise ToolError("Gunakan checkout Git biasa dengan .git directory di workspace")


def _branch(cfg: Settings) -> str:
    return _git(cfg, "symbolic-ref", "--quiet", "--short", "HEAD").strip()


def status(cfg: Settings) -> str:
    _root(cfg)
    branch = _branch(cfg)
    raw = _git(cfg, "-c", "core.quotePath=false", "status", "--porcelain=v1", "--untracked-files=normal")
    clean = []
    for line in raw.splitlines():
        filename = line[3:]
        if " -> " in filename:
            filename = filename.split(" -> ")[-1]
        try:
            _path(cfg, filename)
        except ToolError:
            continue
        clean.append(line[:2] + " " + filename)
        if len(clean) >= 100:
            break
    return json.dumps({"branch": branch, "changes": clean}, ensure_ascii=False)


def _safe_git_path(path: str) -> None:
    if not isinstance(path,str) or not path or any(c in path for c in "*?[]:!\n\r\0"):
        raise ToolError("Pathspec Git khusus atau karakter kontrol tidak diizinkan")


def diff(cfg: Settings, path: str) -> str:
    _root(cfg)
    _safe_git_path(path)
    dest = _path(cfg, path, is_file=True)
    if not dest.is_file():
        raise ToolError("File tidak ada")
    if dest.suffix.lower() not in ALLOWED_WRITE_EXT or dest.stat().st_size > 30000:
        raise ToolError("File tidak dapat direview melalui Git")
    # Use --no-ext-diff --no-textconv to avoid running diff helpers.
    tracked = _git(cfg, "ls-files", "--", path).strip()
    if tracked:
        result = _git(cfg, "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--", path)
    else:
        text = dest.read_text("utf-8", errors="replace")
        result = "".join(difflib.unified_diff([],text.splitlines(keepends=True),
                                                  fromfile="/dev/null",tofile="b/"+path))
    if len(result) > 14000:
        raise ToolError("Diff terlalu besar; tinjau dalam bagian lebih kecil")
    return result or "Tidak ada perubahan dari HEAD"


def _stamp(cfg: Settings, paths: list[str] | None = None) -> str:
    head = _git(cfg, "rev-parse", "HEAD").strip()
    pieces = [_branch(cfg), head]
    if paths:
        for name in paths:
            dest = _path(cfg,name,is_file=True)
            pieces.extend([name,digest_file(dest) or "missing"])
    else:
        pieces.append(_git(cfg,"status","--porcelain=v1"))
    return hashlib.sha256("\0".join(pieces).encode()).hexdigest()


def _validate_commit_repo(cfg: Settings):
    _root(cfg)
    # Git clean/smudge filters may execute programs when staging content.
    # Refuse commits when attributes or nonstandard local filter definitions exist.
    if any(cfg.workspace.rglob(".gitattributes")) or (cfg.workspace/".git"/"info"/"attributes").is_file():
        raise ToolError("Commit ditolak: repo berisi Git attributes (potensi Git filter)")
    config_text = _git(cfg, "config", "--local", "--list")
    if re.search(r"(?im)^(filter\.|core\.hooksPath|core\.fsmonitor|include\.)",config_text):
        raise ToolError("Commit ditolak: repo berisi filter/hook/include konfigurasi yang tidak dipercaya")


def proposal(cfg: Settings, name: str, args: dict) -> tuple[str,str]:
    _root(cfg)
    if name == "git_create_branch":
        branch = args.get("branch")
        if not isinstance(branch,str) or not BRANCH_RE.fullmatch(branch) or branch.endswith("."):
            raise ToolError("Nama branch wajib hermas/<slug> aman (huruf kecil/angka)")
        raw = _git(cfg,"status","--porcelain=v1")
        if raw.strip():
            raise ToolError("Repo harus bersih sebelum membuat branch")
        if _git(cfg,"branch","--list",branch).strip():
            raise ToolError("Branch sudah ada")
        return f"Buat branch baru: {_branch(cfg)} -> {branch}\nTidak mengubah file.",_stamp(cfg)
    if name == "git_commit":
        _validate_commit_repo(cfg)
        branch = _branch(cfg)
        if not branch.startswith("hermas/"):
            raise ToolError("Commit hanya diizinkan pada branch hermas/*")
        paths = args.get("paths")
        message = args.get("message")
        if not isinstance(paths,list) or not 1 <= len(paths) <= 10 or not all(isinstance(p,str) for p in paths) or len(paths)!=len(set(paths)):
            raise ToolError("paths harus daftar unik berisi 1-10 file")
        if not isinstance(message,str) or not 5 <= len(message) <= 120 or "\n" in message:
            raise ToolError("Pesan commit harus 5-120 karakter pada satu baris")
        previews=[]
        for path in paths:
            _safe_git_path(path)
            dest = _path(cfg,path,is_file=True)
            if not dest.is_file() or dest.suffix.lower() not in ALLOWED_WRITE_EXT or dest.stat().st_size > 25000:
                raise ToolError("Hanya file sumber yang ada dan kecil dapat di-commit")
            file_diff = diff(cfg,path)
            if file_diff == "Tidak ada perubahan dari HEAD":
                raise ToolError("File tanpa perubahan tidak dapat di-commit")
            previews.append(file_diff)
        preview = f"Commit di {branch}: {message}\nFile: {', '.join(paths)}\n\n" + "\n".join(previews)
        if len(preview.encode("utf-8")) > 12000:
            raise ToolError("Diff total melebihi 12 KB; pecah commit")
        return preview,_stamp(cfg,paths)
    raise ToolError("Operasi Git tidak dikenali")


def execute(cfg: Settings, name: str, args: dict, expected: str | None) -> str:
    _, current = proposal(cfg,name,args)
    if current != expected:
        raise ToolError("Git HEAD/branch/file berubah setelah approval diajukan")
    if name == "git_create_branch":
        _git(cfg,"switch","-c",args["branch"])
        return f"Branch {_branch(cfg)} berhasil dibuat."
    if name == "git_commit":
        paths = args["paths"]
        _git(cfg,"add","--",*paths)
        _git(cfg,"-c","user.name=Hermas Agentic AI","-c","user.email=hermas@localhost",
             "commit","--no-verify","--only","-m",args["message"],"--",*paths,timeout=20)
        return f"Commit berhasil: {_git(cfg,'rev-parse','--short','HEAD').strip()}"
    raise ToolError("Operasi Git tidak dikenali")
