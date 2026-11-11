"""Declarative plugin/MCP registry. Registration never executes downloaded code.

The v0.4 registry is deliberately metadata-only. Tool execution over MCP is
NOT implemented. Built-in tools continue to use existing human approvals.
"""
from __future__ import annotations

import json
import re
import uuid

from .store import Store, as_json, now


class IntegrationError(ValueError):
    pass


def validate_manifest(data: dict) -> dict:
    if not isinstance(data, dict) or len(as_json(data)) > 8192:
        raise IntegrationError('Manifest harus JSON object maksimal 8 KB')
    if set(data) != {'name', 'version', 'description', 'transport', 'tools'}:
        raise IntegrationError('Field wajib: name, version, description, transport, tools')
    if not isinstance(data['name'], str) or not re.fullmatch(r'[a-z][a-z0-9_-]{2,39}', data['name']):
        raise IntegrationError('Slug integrasi tidak valid')
    if not isinstance(data['version'], str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?', data['version']):
        raise IntegrationError('Versi harus semver')
    if not isinstance(data['description'], str) or not 1 <= len(data['description']) <= 300:
        raise IntegrationError('Deskripsi tidak valid')
    if data['transport'] not in {'mcp-http', 'mcp-stdio', 'webhook', 'builtin'}:
        raise IntegrationError('Transport tidak dikenal')
    tools = data['tools']
    if not isinstance(tools, list) or len(tools) > 30:
        raise IntegrationError('Maksimum 30 tool')
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {'name', 'description', 'read_only'}:
            raise IntegrationError('Tool harus mempunyai name, description, read_only')
        if not isinstance(tool['name'], str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,59}', tool['name']):
            raise IntegrationError('Nama tool tidak valid')
        if not isinstance(tool['description'], str) or not 1 <= len(tool['description']) <= 300:
            raise IntegrationError('Deskripsi tool tidak valid')
        if type(tool['read_only']) is not bool:
            raise IntegrationError('read_only harus boolean')
    if len({tool['name'] for tool in tools}) != len(tools):
        raise IntegrationError('Nama tool duplikat')
    return data


class IntegrationRegistry:
    def __init__(self, store: Store):
        self.db = store

    def list(self) -> list[dict]:
        with self.db.connection() as db:
            rows = db.execute('SELECT * FROM integrations ORDER BY created_at DESC').fetchall()
        return [dict(r) | {'manifest': json.loads(r['manifest_json'])} for r in rows]

    def register(self, manifest: dict):
        valid = validate_manifest(manifest)
        ident, stamp = str(uuid.uuid4()), now()
        with self.db.connection() as db:
            db.execute('INSERT INTO integrations(id,name,manifest_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                       (ident, valid['name'], as_json(valid), 0, stamp, stamp))
        self.db.audit('integration_registered', {'integration': ident, 'name': valid['name']})
        return {'id': ident, 'name': valid['name'], 'manifest': valid, 'enabled': 0}

    def toggle(self, ident: str, enabled: bool):
        with self.db.connection() as db:
            cur = db.execute('UPDATE integrations SET enabled=?,updated_at=? WHERE id=?', (int(enabled),now(),ident))
            if cur.rowcount == 0:
                raise LookupError('Integrasi tidak ditemukan')
        self.db.audit('integration_toggled', {'integration': ident, 'enabled': enabled})
        # Enabling is for registry visibility only; never executes tool code.
