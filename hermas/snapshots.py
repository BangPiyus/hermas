"""Local workspace-only snapshots; signed restore review, checksum and symlink guards.

NOT VPS snapshots, database backup, or an atomic multi-file transaction.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from .store import Store, now, as_json

MAX_FILES=200
MAX_ITEM=4*1024*1024
MAX_TOTAL=32*1024*1024
ALLOWED_EXT=frozenset({'.php','.py','.js','.ts','.css','.html','.md','.txt','.json','.yaml','.yml','.toml','.sh'})
SECRET_RE=re.compile(r'(^\.|(^|[-_.])(secret|token|credential|private|password|key|id_rsa|wp-config)([-_.]|$))',re.I)


class SnapshotError(ValueError):
    pass


class WorkspaceSnapshots:
    def __init__(self, db: Store, workspace: Path, session_secret: str):
        self.db=db
        self.workspace=workspace.resolve()
        self.folder=db.directory/'snapshots'
        self.folder.mkdir(mode=0o700,parents=True,exist_ok=True)
        try:self.folder.chmod(0o700)
        except OSError:pass
        self.signer=URLSafeTimedSerializer(session_secret,salt='hermas-snapshot-restore-v1')

    @staticmethod
    def allowed(relative: str) -> bool:
        p=Path(relative)
        if not relative or p.is_absolute() or '..' in p.parts or '\\' in relative or '\x00' in relative:
            return False
        if any(SECRET_RE.search(part) or part in {'node_modules','venv','__pycache__','vendor','dist','build','.git'} for part in p.parts):
            return False
        return p.suffix.lower() in ALLOWED_EXT and len(relative)<=240

    def path(self, rel: str) -> Path:
        if not self.allowed(rel):raise SnapshotError('Nama file snapshot tidak diizinkan')
        target=self.workspace/rel
        current=self.workspace
        for part in Path(rel).parts:
            current=current/part
            if current.is_symlink():raise SnapshotError('Symlink pada path snapshot ditolak')
        if target.resolve(strict=False).is_relative_to(self.workspace) is False:
            raise SnapshotError('Path keluar workspace')
        return target

    def current(self, rel: str) -> str | None:
        target=self.path(rel)
        if not target.exists():return None
        if not target.is_file() or target.stat().st_size>MAX_ITEM:
            raise SnapshotError('Berkas tujuan tidak valid/terlalu besar')
        return hashlib.sha256(target.read_bytes()).hexdigest()

    def list(self):
        with self.db.connection() as con:
            rows=con.execute('SELECT id,created_at,files_count,total_bytes,archive_sha256 FROM workspace_snapshots ORDER BY created_at DESC LIMIT 100').fetchall()
        return [dict(row) for row in rows]

    def record(self, identity: str):
        with self.db.connection() as con:
            row=con.execute('SELECT * FROM workspace_snapshots WHERE id=?',(identity,)).fetchone()
        if not row:raise LookupError('Snapshot tidak ditemukan')
        return dict(row)

    def create(self):
        files=[];count=0;total=0
        for root,dirs,names in os.walk(self.workspace,followlinks=False):
            dirs[:]=sorted(d for d in dirs if not (Path(root)/d).is_symlink() and not d.startswith('.') and d not in {'venv','vendor','node_modules','dist','build','__pycache__'})
            for name in sorted(names):
                path=Path(root)/name
                relative=path.relative_to(self.workspace).as_posix()
                if not self.allowed(relative):continue
                if path.is_symlink() or not path.is_file():continue
                st=path.stat()
                if not stat.S_ISREG(st.st_mode):continue
                if st.st_size>MAX_ITEM:raise SnapshotError('File terlalu besar; snapshot dibatalkan')
                count+=1;total+=st.st_size
                if count>MAX_FILES or total>MAX_TOTAL:raise SnapshotError('Batas ukuran/jumlah file snapshot terlampaui')
                data=path.read_bytes()
                if len(data)>MAX_ITEM:raise SnapshotError('File berubah saat snapshot')
                files.append((relative,data,hashlib.sha256(data).hexdigest()))
        if not files:raise SnapshotError('Tidak ada file source yang memenuhi kebijakan')
        ident=str(uuid.uuid4())
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for relative, data, digest in files:
                archive.writestr(relative,data)
        content=buffer.getvalue()
        digest=hashlib.sha256(content).hexdigest()
        filename=self.folder/(ident+'.zip')
        with filename.open('xb') as file:
            file.write(content)
        try:filename.chmod(0o600)
        except OSError:pass
        manifest={relative:checksum for relative,_,checksum in files}
        with self.db.connection() as con:
            con.execute('INSERT INTO workspace_snapshots(id,archive_name,archive_sha256,manifest_json,files_count,total_bytes,created_at) VALUES(?,?,?,?,?,?,?)',
                        (ident,filename.name,digest,as_json(manifest),len(files),total,now()))
        self.db.audit('workspace_snapshot_created',{'snapshot_id':ident,'file_count':len(files)})
        return {'id':ident,'files_count':len(files),'total_bytes':total,'created_at':now()}

    def _verify(self, identity: str):
        record=self.record(identity)
        filename=self.folder/(identity+'.zip')
        if record['archive_name']!=filename.name or filename.is_symlink() or not filename.is_file():
            raise SnapshotError('Path arsip tidak valid')
        if filename.stat().st_size>MAX_TOTAL+1024*1024:
            raise SnapshotError('Arsip terlalu besar')
        data=filename.read_bytes()
        if hashlib.sha256(data).hexdigest()!=record['archive_sha256']:
            raise SnapshotError('Checksum arsip tidak cocok')
        manifest=json.loads(record['manifest_json'])
        if not isinstance(manifest,dict) or not 1<=len(manifest)<=MAX_FILES:
            raise SnapshotError('Manifest rusak')
        content={}
        try:
            with zipfile.ZipFile(io.BytesIO(data),'r') as archive:
                entries=archive.infolist()
                if len(entries)!=len(manifest) or set(a.filename for a in entries)!=set(manifest):
                    raise SnapshotError('Isi arsip tidak cocok dengan manifest')
                total=0
                for item in entries:
                    if not self.allowed(item.filename) or item.is_dir() or item.file_size>MAX_ITEM:
                        raise SnapshotError('Jenis file arsip tidak valid')
                    if (item.external_attr >> 16) & 0o170000 == 0o120000:
                        raise SnapshotError('Symlink dalam arsip ditolak')
                    total+=item.file_size
                    if total>MAX_TOTAL:raise SnapshotError('Arsip melewati batas aman')
                    blob=archive.read(item)
                    if hashlib.sha256(blob).hexdigest()!=manifest[item.filename]:
                        raise SnapshotError('Checksum file arsip tidak cocok')
                    content[item.filename]=blob
        except (zipfile.BadZipFile,RuntimeError) as exc:
            raise SnapshotError('Arsip korup') from exc
        return record,content

    def preview(self, identity: str):
        record,content=self._verify(identity)
        baseline={path:self.current(path) for path in content}
        changes=[{'path':path,'action':'create' if before is None else ('unchanged' if before==hashlib.sha256(content[path]).hexdigest() else 'overwrite')}
                 for path,before in sorted(baseline.items())]
        token=self.signer.dumps({'snapshot':identity,'baseline':baseline,'archive':record['archive_sha256']})
        return {'snapshot_id':identity,'changes':changes,'token':token,'expires_in_seconds':600,
                'warning':'Hanya file di dalam snapshot; tidak menghapus file lain. Restore multi-file tidak atomik.'}

    def restore(self, identity: str, token: str, confirmation: str):
        if confirmation!='RESTORE':raise SnapshotError('Ketik RESTORE untuk mengonfirmasi')
        try:
            data=self.signer.loads(token,max_age=600)
        except (BadSignature,SignatureExpired) as exc:
            raise SnapshotError('Token preview tidak valid/kedaluwarsa') from exc
        if data.get('snapshot')!=identity:raise SnapshotError('Token snapshot tidak cocok')
        record,content=self._verify(identity)
        if data.get('archive')!=record['archive_sha256'] or not isinstance(data.get('baseline'),dict):
            raise SnapshotError('Arsip berubah sejak preview')
        if set(data['baseline'])!=set(content):raise SnapshotError('Isi snapshot berubah')
        # Preflight all conflicts before ANY write.
        for relative in content:
            if self.current(relative)!=data['baseline'][relative]:
                raise SnapshotError('Workspace berubah sejak preview; ulangi preview')
        backups={relative:(self.path(relative).read_bytes() if self.path(relative).exists() else None) for relative in content}
        written=[]
        try:
            for relative,blob in content.items():
                target=self.path(relative)
                target.parent.mkdir(parents=True,exist_ok=True)
                # Recheck symlink after directory creation, including all parents.
                self.path(relative)
                fd,name=tempfile.mkstemp(prefix='.hermas-restore-',dir=target.parent)
                try:
                    with os.fdopen(fd,'wb') as out:
                        out.write(blob);out.flush();os.fsync(out.fileno())
                    os.chmod(name,0o600)
                    os.replace(name,target)
                finally:
                    if os.path.exists(name):os.unlink(name)
                written.append(relative)
        except Exception as exc:
            # Best effort rollback; manual review required if rollback itself fails.
            errors=[]
            for relative in reversed(written):
                target=self.path(relative)
                try:
                    old=backups[relative]
                    if old is None:target.unlink(missing_ok=True)
                    else:
                        fd,name=tempfile.mkstemp(prefix='.hermas-rollback-',dir=target.parent)
                        try:
                            with os.fdopen(fd,'wb') as out:out.write(old);out.flush();os.fsync(out.fileno())
                            os.chmod(name,0o600);os.replace(name,target)
                        finally:
                            if os.path.exists(name):os.unlink(name)
                except OSError:errors.append(relative)
            self.db.audit('workspace_restore_failed',{'snapshot_id':identity,'rollback_failures':len(errors)})
            raise SnapshotError('Restore gagal; periksa workspace manual' if errors else 'Restore gagal; rollback lokal telah dicoba') from exc
        self.db.audit('workspace_restored',{'snapshot_id':identity,'file_count':len(written)})
        return {'restored_files':len(written),'snapshot_id':identity,'files':written}
