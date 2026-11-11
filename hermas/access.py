"""v0.6 secondary account management with fail-closed, scoped RBAC.

The original bootstrap admin remains in .env; secondary accounts cannot obtain
legacy API access. Viewers can see operations summaries, operators can also
acknowledge alarms and run explicitly enabled read-only VPS probes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid

from .store import Store, now

USER_RE = re.compile(r"[a-z][a-z0-9_.-]{2,31}\Z")
ROLES = frozenset({'viewer', 'operator'})


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=16384, r=8, p=1, dklen=32)


def _encode(password: str) -> str:
    salt = secrets.token_bytes(16)
    return 'scrypt$16384$' + base64.b64encode(salt).decode() + '$' + base64.b64encode(_derive(password,salt)).decode()


def _verify(password: str, encoded: str) -> bool:
    try:
        prefix, rounds, salt, digest = encoded.split('$')
        if prefix != 'scrypt' or rounds != '16384':
            return False
        return hmac.compare_digest(_derive(password,base64.b64decode(salt)),base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


class AccessService:
    def __init__(self, db: Store):
        self.db = db

    @staticmethod
    def _public(row) -> dict:
        return {key: row[key] for key in ('id','username','role','enabled','session_version','created_at','updated_at')}

    def accounts(self):
        with self.db.connection() as con:
            rows=con.execute('SELECT * FROM auth_users ORDER BY username').fetchall()
        return [self._public(row) for row in rows]

    def get(self, identity: str):
        with self.db.connection() as con:
            row=con.execute('SELECT * FROM auth_users WHERE id=?',(identity,)).fetchone()
        return self._public(row) if row else None

    def create(self, username: str, password: str, role: str):
        username=username.strip().lower()
        if not USER_RE.fullmatch(username) or username == 'admin':
            raise ValueError('Username harus 3–32 karakter huruf kecil/angka/._-; admin dicadangkan')
        if role not in ROLES:
            raise ValueError('Peran hanya viewer/operator; admin utama berasal dari .env')
        if not isinstance(password,str) or len(password)<14 or len(password)>256:
            raise ValueError('Password minimal 14 karakter dan maksimal 256')
        identity=str(uuid.uuid4()); stamp=now()
        try:
            with self.db.connection() as con:
                con.execute('INSERT INTO auth_users(id,username,password_hash,role,enabled,session_version,created_at,updated_at) VALUES(?,?,?,?,1,1,?,?)',
                            (identity,username,_encode(password),role,stamp,stamp))
        except __import__('sqlite3').IntegrityError as exc:
            raise ValueError('Username sudah dipakai') from exc
        self.db.audit('account_created',{'account_id':identity,'role':role})
        return self.get(identity)

    def authenticate(self, username: str, password: str):
        if not isinstance(username,str) or not isinstance(password,str):
            return None
        with self.db.connection() as con:
            row=con.execute('SELECT * FROM auth_users WHERE username=? AND enabled=1',(username.strip().lower(),)).fetchone()
        # Always run a KDF to avoid simple username enumeration timing.
        expected=row['password_hash'] if row else _encode('dummy-password-constant')
        if not _verify(password,expected) or row is None:
            return None
        return self._public(row)

    def change(self, identity: str, *, enabled: bool | None = None, role: str | None = None, password: str | None = None):
        if role is not None and role not in ROLES:
            raise ValueError('Peran hanya viewer/operator')
        if password is not None and not 14<=len(password)<=256:
            raise ValueError('Password harus 14–256 karakter')
        with self.db.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute('SELECT * FROM auth_users WHERE id=?',(identity,)).fetchone()
            if row is None:raise LookupError('Akun tidak ditemukan')
            con.execute('UPDATE auth_users SET enabled=?,role=?,password_hash=?,session_version=session_version+1,updated_at=? WHERE id=?',
                        (int(enabled) if enabled is not None else row['enabled'],role or row['role'],_encode(password) if password is not None else row['password_hash'],now(),identity))
        self.db.audit('account_changed',{'account_id':identity,'enabled':enabled,'role':role,'password_rotated':password is not None})
        return self.get(identity)
