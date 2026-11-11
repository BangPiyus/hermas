"""Hermas v0.5: opt-in, read-only SSH health checks and WordPress metadata audits.

No remote mutations, credentials in SQLite, arbitrary commands, agent-exposed SSH,
or automated deployments. Host keys MUST be pinned by the operator.
"""
from __future__ import annotations

import base64
import os
import threading
import ipaddress
import json
import re
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .store import Store, as_json, now


class VPSException(ValueError):
    pass


HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
MAX_RESULT_BYTES = 100_000


def validate_host(host: str) -> str:
    host = host.strip().lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
            raise VPSException('Alamat loopback/link-local/multicast tidak diizinkan sebagai target SSH')
        return str(address)
    if not HOSTNAME_RE.fullmatch(host) or any(part.endswith('-') or len(part) > 63 for part in host.split('.')):
        raise VPSException('Hostname harus DNS valid atau alamat IP; URL dan opsi SSH tidak diizinkan')
    return host


def validate_root(root: str) -> str:
    """Only conventional webroots, no . or .. segments, no shell interpretation."""
    if not isinstance(root, str) or len(root) > 220 or '\x00' in root or '\n' in root:
        raise VPSException('Path WordPress tidak valid')
    p = Path(root)
    if not p.is_absolute() or '..' in p.parts or '.' in p.parts or not (root.startswith('/var/www/') or root.startswith('/srv/www/')):
        raise VPSException('Hanya direktori absolut di bawah /var/www/ atau /srv/www/ yang diizinkan')
    if root.endswith('/') or root.startswith('//'):
        raise VPSException('Path webroot harus eksplisit tanpa slash penutup')
    return root


# Executed on the remote host by a fixed, vetted Python program. Only JSON metadata
# is returned; never read secrets or database contents and never modify remote files.
REMOTE_PROBE = r'''
import json, os, pathlib, stat, subprocess, sys, time
mode=sys.argv[1]
root=sys.argv[2] if len(sys.argv)>2 else ''

def service(name):
    try:
        p=subprocess.run(['systemctl','is-active',name],capture_output=True,text=True,timeout=2)
        return p.stdout.strip()[:40] if p.returncode==0 else 'inactive_or_unknown'
    except (OSError, subprocess.TimeoutExpired): return 'unknown'

def health():
    mem={}
    try:
        with open('/proc/meminfo',encoding='ascii') as f:
            for line in f:
                k,_,v=line.partition(':')
                if k in ('MemTotal','MemAvailable','SwapTotal','SwapFree'):
                    mem[k]=int(v.strip().split()[0])*1024
    except (OSError,ValueError,IndexError): pass
    disks={}
    for directory in ('/','/var/www'):
        try:
            d=os.statvfs(directory)
            disks[directory]={'total':d.f_blocks*d.f_frsize,'free':d.f_bavail*d.f_frsize}
        except OSError: pass
    try: load=list(os.getloadavg())
    except OSError: load=[]
    try:
        with open('/proc/uptime',encoding='ascii') as f: uptime=int(float(f.read().split()[0]))
    except (OSError,ValueError,IndexError): uptime=None
    return {'kind':'health','cpu_count':os.cpu_count(),'load':load,
        'uptime_seconds':uptime,'memory_bytes':mem,'disk_bytes':disks,
        'services':{x:service(x) for x in ('nginx','apache2','mariadb','mysql','php8.3-fpm','php8.2-fpm','php8.1-fpm')}}

def audit():
    base=pathlib.Path(root)
    if not (root.startswith('/var/www/') or root.startswith('/srv/www/')) or '..' in base.parts:
        raise ValueError('outside_allowed_roots')
    # Do not follow root symlinks into unexpected directories.
    if base.is_symlink() or not base.is_dir(): raise ValueError('not_a_directory_or_symlink')
    resolved=base.resolve()
    if not (str(resolved).startswith('/var/www/') or str(resolved).startswith('/srv/www/')): raise ValueError('escaped_webroot')
    required=['wp-admin','wp-includes','wp-content']
    exists={name:(base/name).is_dir() for name in required}
    if not all(exists.values()): raise ValueError('not_wordpress_root')
    if any((base/name).is_symlink() or not str((base/name).resolve()).startswith(str(resolved)+'/') for name in required):
        raise ValueError('wordpress_structure_symlink_not_allowed')
    config=base/'wp-config.php'
    result={'kind':'wordpress','root':root,'structure':exists,'signals':[],
            'uploads_scanned':0,'uploads_truncated':False,'php_upload_paths':[],
            'plugins_count':0,'themes_count':0,'plugin_names':[],'theme_names':[]}
    if config.is_file() and not config.is_symlink():
        permissions=stat.S_IMODE(config.stat().st_mode)
        result['wp_config_permissions']=oct(permissions)
        if permissions & stat.S_IWOTH: result['signals'].append('wp-config.php writable oleh semua pengguna')
    else: result['signals'].append('wp-config.php tidak ditemukan atau berupa symlink')
    for dir_name,key,key_list in (('plugins','plugins_count','plugin_names'),('themes','themes_count','theme_names')):
        folder=base/'wp-content'/dir_name
        if folder.is_dir() and not folder.is_symlink():
            try:
                names=sorted(p.name for p in folder.iterdir() if p.is_dir() and not p.is_symlink())
                result[key]=len(names);result[key_list]=names[:30]
            except OSError: pass
    uploads=base/'wp-content'/'uploads'
    start=time.monotonic()
    if uploads.is_dir() and not uploads.is_symlink():
        for parent,dirs,files in os.walk(uploads,followlinks=False):
            dirs[:]=[d for d in dirs if not pathlib.Path(parent,d).is_symlink()]
            for f in files:
                result['uploads_scanned']+=1
                path=pathlib.Path(parent,f)
                if path.is_symlink():continue
                if f.lower().endswith(('.php','.phtml','.php5','.phar')) and len(result['php_upload_paths'])<50:
                    result['php_upload_paths'].append(str(path.relative_to(base)))
                if result['uploads_scanned']>=15000 or time.monotonic()-start>8:
                    result['uploads_truncated']=True;break
            if result['uploads_truncated']:break
    if result['php_upload_paths']:
        result['signals'].append('File PHP ditemukan di wp-content/uploads; perlu pemeriksaan manual')
    if result['uploads_truncated']:
        result['signals'].append('Audit uploads dibatasi; hasil tidak menyeluruh')
    return result

try:
    if mode=='health': print(json.dumps(health(),separators=(',',':')))
    elif mode=='wordpress': print(json.dumps(audit(),separators=(',',':')))
    else: print(json.dumps({'error':'unknown_mode'}))
except Exception as exc:
    print(json.dumps({'error':str(exc)[:100]}))
'''


def build_remote_command(mode: str, root: str = '') -> str:
    if mode not in {'health', 'wordpress'}:
        raise VPSException('Mode probe tidak didukung')
    if mode == 'wordpress':
        validate_root(root)
    # The remote script is constant; untrusted path is base64-encoded and passed as a
    # shell-quoted argument, never interpolated into executable source code.
    source = base64.b64encode(REMOTE_PROBE.encode()).decode('ascii')
    python = f"import base64;exec(compile(base64.b64decode('{source}'),'<hermas-readonly>','exec'))"
    return shlex.join(['python3', '-c', python, mode, root])


def health_signals(report: dict) -> list[str]:
    """Conservative alerts for human review; not incident declarations."""
    alerts=[]
    memory=report.get('memory_bytes',{})
    total=memory.get('MemTotal')
    available=memory.get('MemAvailable')
    if isinstance(total,(int,float)) and isinstance(available,(int,float)) and total>0 and 0<=available<=total:
        if available/total < 0.10:
            alerts.append('RAM tersedia di bawah 10%; periksa proses dan swap')
    disks=report.get('disk_bytes',{})
    if isinstance(disks,dict):
        for mount,detail in disks.items():
            if not isinstance(detail,dict):continue
            disk_total,disk_free=detail.get('total'),detail.get('free')
            if isinstance(disk_total,(int,float)) and isinstance(disk_free,(int,float)) and disk_total>0 and 0<=disk_free<=disk_total:
                if disk_free/disk_total<0.10:
                    alerts.append(f'Ruang kosong filesystem {mount} di bawah 10%')
    load=report.get('load',[])
    cpus=report.get('cpu_count')
    if isinstance(load,list) and load and isinstance(load[0],(int,float)) and isinstance(cpus,int) and cpus>0:
        if load[0]>cpus*1.5:
            alerts.append('Load average 1 menit relatif tinggi dibanding jumlah CPU')
    return alerts


class VPSService:
    def __init__(self, db: Store, *, enabled: bool = False, ssh_key: str = '', known_hosts: str = ''):
        self.db = db
        self.enabled = enabled
        self.ssh_key = ssh_key
        self.known_hosts = known_hosts
        self._scan_limit = threading.BoundedSemaphore(2)

    def hosts(self) -> list[dict]:
        with self.db.connection() as con:
            rows=con.execute('SELECT * FROM vps_hosts ORDER BY created_at DESC').fetchall()
        return [dict(row) for row in rows]

    def host(self, host_id: str) -> dict:
        with self.db.connection() as con:
            row=con.execute('SELECT * FROM vps_hosts WHERE id=?',(host_id,)).fetchone()
        if not row: raise LookupError('Server tidak ditemukan')
        return dict(row)

    def add_host(self, *, name: str, hostname: str, ssh_user: str, port: int = 22, enabled: bool = False) -> dict:
        name=name.strip()
        if not (1<=len(name)<=80) or any(ord(c)<32 for c in name):
            raise VPSException('Nama server tidak valid')
        hostname=validate_host(hostname)
        if not USER_RE.fullmatch(ssh_user) or ssh_user=='root':
            raise VPSException('Gunakan SSH user non-root dengan nama Linux valid')
        if not (1<=port<=65535): raise VPSException('Port SSH tidak valid')
        item={'id':str(uuid.uuid4()),'name':name,'hostname':hostname,'ssh_user':ssh_user,
              'port':port,'enabled':int(enabled),'created_at':now(),'updated_at':now()}
        with self.db.connection() as con:
            con.execute('INSERT INTO vps_hosts(id,name,hostname,ssh_user,port,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',tuple(item.values()))
        self.db.audit('vps_registered',{'host_id':item['id']})
        return item

    def set_enabled(self, host_id: str, enabled: bool) -> dict:
        host=self.host(host_id)
        if host.get('retired'):
            raise VPSException('Server yang diarsipkan tidak dapat diaktifkan kembali')
        with self.db.connection() as con:
            con.execute('UPDATE vps_hosts SET enabled=?,updated_at=? WHERE id=?',(int(enabled),now(),host_id))
        self.db.audit('vps_enabled_changed',{'host_id':host_id,'enabled':bool(enabled)})
        return self.host(host_id)

    def delete_host(self, host_id: str) -> None:
        self.host(host_id)
        with self.db.connection() as con:
            # Never delete checks; preserve audit trails. Instead retire the entry.
            con.execute('UPDATE vps_hosts SET enabled=0,retired=1,updated_at=? WHERE id=?',(now(),host_id))
        self.db.audit('vps_retired',{'host_id':host_id})

    def checks(self, host_id: str, limit: int = 25) -> list[dict]:
        self.host(host_id)
        with self.db.connection() as con:
            rows=con.execute('SELECT id,host_id,kind,observed_at,status,result_json FROM vps_checks WHERE host_id=? ORDER BY observed_at DESC,id DESC LIMIT ?', (host_id,min(max(limit,1),100))).fetchall()
        return [{**dict(r),'result':json.loads(r['result_json'])} for r in rows]

    def _ssh_ready(self) -> None:
        if not self.enabled:
            raise VPSException('SSH monitoring nonaktif; aktifkan HERMAS_VPS_SSH_ENABLED=true secara eksplisit')
        if shutil.which('ssh') is None:
            raise VPSException('OpenSSH client tidak ditemukan')
        known=Path(self.known_hosts)
        key=Path(self.ssh_key)
        if not known.is_file() or not key.is_file():
            raise VPSException('SSH key atau pinned known_hosts tidak tersedia')
        if os.name == 'posix' and (key.stat().st_mode & 0o077) != 0:
            raise VPSException('Private key harus memiliki permission 0600 (tidak dapat diakses pengguna lain)')
        if key.is_symlink() or known.is_symlink():
            raise VPSException('Symlink SSH key/known_hosts tidak diizinkan')

    def build_ssh_argv(self, host: dict, mode: str, root: str = '') -> list[str]:
        self._ssh_ready()
        command=build_remote_command(mode,root)
        return [
            'ssh','-F',('NUL' if os.name == 'nt' else '/dev/null'),'-T','-i',str(Path(self.ssh_key).resolve()),
            '-o','BatchMode=yes','-o','IdentitiesOnly=yes',
            '-o',f'UserKnownHostsFile={Path(self.known_hosts).resolve()}',
            '-o','StrictHostKeyChecking=yes','-o','UpdateHostKeys=no',
            '-o','ClearAllForwardings=yes','-o','ForwardAgent=no','-o','ForwardX11=no','-o','PermitLocalCommand=no',
            '-o','ConnectTimeout=8','-o','ConnectionAttempts=1',
            '-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2',
            '-o','LogLevel=ERROR','-p',str(host['port']),'-l',host['ssh_user'],
            host['hostname'],command
        ]

    def check(self, host_id: str, mode: str, root: str = '') -> dict:
        host=self.host(host_id)
        if host.get('retired') or not host['enabled']:
            raise VPSException('Server nonaktif/diarsipkan; pemeriksaan tidak diizinkan')
        if mode=='wordpress': validate_root(root)
        cmd=self.build_ssh_argv(host,mode,root)
        self.db.audit('vps_check_requested',{'host_id':host_id,'kind':mode})
        if not self._scan_limit.acquire(blocking=False):
            raise VPSException('Batas dua pemindaian bersamaan tercapai')
        try:
            try:
                proc=subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
            except subprocess.TimeoutExpired as exc:
                raise VPSException('SSH timeout; periksa jaringan/izin user') from exc
            except OSError as exc:
                raise VPSException('Tidak dapat memulai koneksi SSH') from exc
        finally:
            self._scan_limit.release()
        if proc.returncode != 0:
            # Deliberately avoid storing raw stderr (may include sensitive host configuration).
            raise VPSException(f'Pemeriksaan SSH gagal (exit code {proc.returncode}); periksa host key/akses')
        if len(proc.stdout)>MAX_RESULT_BYTES:
            raise VPSException('Hasil pemeriksaan terlalu besar; ditolak')
        try:
            report=json.loads(proc.stdout)
        except (ValueError,UnicodeDecodeError) as exc:
            raise VPSException('Respons SSH bukan JSON valid') from exc
        if not isinstance(report,dict) or report.get('kind')!=mode or 'error' in report:
            raise VPSException('Respons pemeriksaan ditolak atau direktori tidak valid')
        if mode=='health' and not isinstance(report.get('memory_bytes'),dict):
            raise VPSException('Skema hasil health check tidak valid')
        if mode=='wordpress' and not isinstance(report.get('signals'),list):
            raise VPSException('Skema hasil WordPress audit tidak valid')
        if mode=='health':
            report['signals']=health_signals(report)
        # Bound the entire report before persisting; never include arbitrary remote stderr.
        result=json.dumps(report,ensure_ascii=False)
        if len(result)>30000:
            raise VPSException('Hasil pemeriksaan melebihi batas')
        item={'id':str(uuid.uuid4()),'host_id':host_id,'kind':mode,'observed_at':now(),
              'status':'review' if report.get('signals') else 'ok','result_json':result}
        with self.db.connection() as con:
            con.execute('INSERT INTO vps_checks(id,host_id,kind,observed_at,status,result_json) VALUES(?,?,?,?,?,?)',tuple(item.values()))
        self.db.audit('vps_check_completed',{'host_id':host_id,'kind':mode,'status':item['status']})
        return {'id':item['id'],'host_id':host_id,'kind':mode,'observed_at':item['observed_at'],'status':item['status'],'result':report}

    def create_plan(self, host_id: str, kind: str, *, root: str = '', notes: str = '') -> dict:
        host=self.host(host_id)
        if host.get('retired'):raise VPSException('Server telah diarsipkan')
        if kind not in {'backup','deployment'}:raise VPSException('Jenis rencana tidak dikenal')
        if root:validate_root(root)
        if len(notes)>1200:raise VPSException('Catatan terlalu panjang')
        steps=([
            {'title':'Verifikasi disk, kapasitas dan izin read-only','action':'manual'},
            {'title':'Ambil snapshot file WordPress ke penyimpanan terenkripsi','action':'manual'},
            {'title':'Ekspor database secara konsisten dengan kredensial lokal khusus backup','action':'manual'},
            {'title':'Simpan manifest checksum dan uji restore di staging','action':'manual'},
            {'title':'Verifikasi retensi, akses, dan jadwal penghapusan backup','action':'manual'},
        ] if kind=='backup' else [
            {'title':'Periksa Git status, dependency, dan konfigurasi aplikasi','action':'manual'},
            {'title':'Konfirmasi backup tervalidasi dan rollback point','action':'manual'},
            {'title':'Deploy dahulu di staging dan jalankan automated test','action':'manual'},
            {'title':'Minta persetujuan operator untuk maintenance window','action':'manual'},
            {'title':'Deploy dengan metode atomic release; smoke test + rollback jika gagal','action':'manual'},
        ])
        plan={'id':str(uuid.uuid4()),'host_id':host_id,'kind':kind,'root':root,'notes':notes.strip(),
              'status':'draft','steps':steps,'created_at':now()}
        with self.db.connection() as con:
            con.execute('INSERT INTO vps_plans(id,host_id,kind,root,notes,status,steps_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                (plan['id'],host_id,kind,root,plan['notes'],'draft',as_json(steps),plan['created_at']))
        self.db.audit('vps_plan_drafted',{'plan_id':plan['id'],'host_id':host_id,'kind':kind})
        return plan

    def plans(self, host_id: str) -> list[dict]:
        self.host(host_id)
        with self.db.connection() as con:
            rows=con.execute('SELECT * FROM vps_plans WHERE host_id=? ORDER BY created_at DESC,id DESC LIMIT 50',(host_id,)).fetchall()
        return [{**dict(r),'steps':json.loads(r['steps_json'])} for r in rows]
