# Hermas Agentic AI v0.5.0

**Hermas Agentic AI** adalah prototipe *self-hosted* AI Agent independen berbasis Python, FastAPI, SQLite, Tailwind CSS lokal, dan ikon SVG Feather-style. **Bukan fork Nous Research Hermes Agent**, belum menyamai seluruh fiturnya, dan belum dinyatakan aman untuk mengelola VPS produksi tanpa pengawasan. Proyek ini dapat dijalankan offline dalam mode demo.

## Ringkasan fitur menurut versi

| Versi | Fitur utama | Batasan |
|---|---|---|
| 0.1 | Dashboard, chat OpenAI-compatible/demo, file tools, persetujuan, memory dan skills | Satu admin |
| 0.2 | Task queue persisten, resume manual, audit hash chain, pengukuran token/waktu | Satu proses/worker |
| 0.3 | Planner/Coder/Reviewer, Verified Memory, Git status/diff/branch/commit dengan approval | Agent menghasilkan usulan, bukan langsung memodifikasi hasil multi-agent |
| 0.4 | Workflow, cron UTC, Telegram opsional, registry manifest MCP, browser preview opsional | MCP metadata-only, browser read-only |
| **0.5** | **VPS Commander: inventaris VPS, SSH read-only opt-in, audit metadata WordPress, sinyal kesehatan, histori, draf backup dan deployment** | **Backup database, restore, deploy, dan perbaikan otomatis BELUM dieksekusi** |

## Apa yang diimplementasikan di v0.5

- **Inventaris multi-VPS:** nama host, IP/domain, port, user SSH non-root, status aktif/nonaktif/arsip. Tidak menyimpan SSH password maupun private key dalam SQLite.
- **Read-only SSH Health:** ketika diaktifkan operator, memeriksa jumlah CPU, load, uptime, RAM/swap, disk root dan `/var/www`, serta status service Nginx, Apache, MariaDB/MySQL, PHP-FPM terpilih. Tidak mengubah konfigurasi server.
- **Health signals:** menandai RAM tersedia di bawah 10%, disk tersedia di bawah 10%, dan load 1 menit yang tinggi terhadap jumlah CPU. Ini heuristik untuk ditinjau, bukan alarm yang telah divalidasi pada VPS produksi.
- **WordPress filesystem audit:** mengecek struktur instalasi, metadata izin `wp-config.php`, jumlah/nama plugin dan tema, dan keberadaan ekstensi PHP pada `wp-content/uploads`. Membatasi jumlah dan durasi scanning untuk mengurangi beban server. Tidak membaca isi `wp-config.php`, database, atau kode sumber; tidak membuktikan situs bebas malware.
- **Histori check:** menyimpan hasil JSON ringkas di SQLite dan audit event yang terkait. stderr SSH tidak disimpan atau dikirim ke frontend.
- **Backup & deployment planning:** membuat checklist persiapan backup, restore test, staging, approval dan rollback. Semuanya **`draft / manual`**, tidak menjalankan `mysqldump`, `rm`, deploy, update, restart, atau perintah shell bebas.
- **Dashboard:** menu VPS Commander (ikon server) untuk mengelola inventory, pemeriksaan read-only, dan rencana.

### Keamanan desain SSH

1. `HERMAS_VPS_SSH_ENABLED=false` merupakan default. Selain itu, setiap host di inventaris baru selalu **nonaktif**.
2. Saat diaktifkan, koneksi memakai OpenSSH dengan `BatchMode=yes`, `StrictHostKeyChecking=yes`, file `known_hosts` yang diverifikasi operator, SSH private key lokal, forwarding dan PTY dimatikan.
3. SSH root **ditolak** di UI/API. Gunakan user Linux non-root, kunci khusus, dan hak akses paling sedikit.
4. Server hanya menjalankan skrip Python statis yang dikirim melalui SSH untuk dua mode: `health` dan `wordpress`. Tidak ada endpoint shell bebas dan tidak ada SSH tool yang dapat dipanggil langsung oleh model LLM.
5. Target WordPress dibatasi ke direktori di bawah `/var/www/` atau `/srv/www/`. Symlink dan keluaran terlalu besar dibatasi/ditolak; eksekusi diberi timeout.
6. Tidak ada auto-approve, remote write, WP-CLI execution, atau network deploy.
7. SSH private key disimpan **di luar repositori**, path-nya di `.env`. Jangan pernah memasukkan private key/password ke chat.

**Penting:** Saat scanning WordPress, remote user harus dapat membaca metadata direktorinya. Jangan memberikan `sudo`, akses root, atau akses langsung database hanya untuk membuat scanner bekerja. SSH/WordPress nyata belum diuji end-to-end pada VPS pengguna.

## Instalasi cepat

Persyaratan: Python 3.11+, pip, Git opsional untuk Coding Studio, OpenSSH client untuk fitur VPS opsional; Windows 11/Linux/macOS.

**Windows:** ekstrak ZIP ke folder baru, masuk `hermas-agentic-ai`, jalankan `start.bat`.

**Ubuntu:**

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git unzip openssh-client
unzip Hermas-Agentic-AI-v0.5.0.zip
cd hermas-agentic-ai
chmod +x start.sh
./start.sh
```

Saat pertama dijalankan `.env` dibuat otomatis dengan password administrator dan secret acak; baca password di terminal. Buka **http://127.0.0.1:8765**. Mode awal `demo`, tidak menggunakan API provider sebenarnya.

Jangan mengekspos dashboard langsung ke Internet. Jika aplikasi berjalan di VPS, gunakan SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 user@alamat-server
```

## Upgrade dari v0.4 atau versi lebih lama

1. Hentikan seluruh worker Hermas lama. **Backup** `.env`, `data/` (termasuk SQLite, WAL/SHM jika ada), dan `workspace/` ketika aplikasi sudah berhenti.
2. Ekstrak ZIP v0.5 ke direktori **baru**; jangan menimpa source secara acak.
3. Salin `.env`, `data/`, dan `workspace/` lama ke folder baru. Bila `.env` memakai path absolut, koreksi sesuai folder baru.
4. Jalankan aplikasi. SQLite akan menaikkan `PRAGMA user_version` ke **5** sambil mempertahankan sesi, tasks, audit, dan data versi sebelumnya.
5. Semua VPS yang baru didaftarkan tetap nonaktif; SSH monitoring tidak otomatis diaktifkan setelah upgrade.
6. Periksa daftar Tasks, Workflow dan Audit sebelum mengaktifkan scheduler lama. Interruption harus diperiksa manual.

## Mengaktifkan SSH hanya ketika sudah siap

File `.env`:

```dotenv
HERMAS_VPS_SSH_ENABLED=false
HERMAS_VPS_SSH_KEY_PATH=
HERMAS_VPS_KNOWN_HOSTS_PATH=
```

Siapkan pasangan key khusus untuk Hermas dan tambahkan public key ke akun non-root di VPS. Buat file `known_hosts` dengan fingerprint server yang **diverifikasi lewat kanal independen**; hasil `ssh-keyscan` tanpa verifikasi **tidak cukup**. Simpan file pada lokasi yang dibatasi hak aksesnya (Linux private key `chmod 600`; Windows gunakan ACL pribadi dan jangan simpan di folder publik).

Ketika sudah siap untuk percobaan pada VPS **staging**, masukkan path absolut ke dua file tersebut, ubah `HERMAS_VPS_SSH_ENABLED=true`, lalu restart Hermas. Aktifkan host tertentu dari dashboard, baru klik "Periksa VPS" atau "Audit WordPress". Sistem hanya melaksanakan dua skrip baca-saja. Default SSH bisa dibiarkan **false** jika masih melengkapi fitur.

## API v0.5

Semua API berikut memerlukan cookie login administrator. Mutasi memerlukan `X-Hermas-CSRF`; browser dashboard mengelolanya secara otomatis. API v0.1–v0.4 tetap tersedia.

| Metode | Endpoint | Fungsi |
|---|---|---|
| GET | `/api/vps/hosts` | Inventaris VPS |
| POST | `/api/vps/hosts` | Daftarkan VPS baru (nonaktif) |
| POST | `/api/vps/hosts/{id}/enabled` | Toggle host (tidak mengaktifkan SSH global) |
| POST | `/api/vps/hosts/{id}/retire` | Arsip host, histori tetap disimpan |
| GET | `/api/vps/hosts/{id}/checks` | Histori pemeriksaan |
| POST | `/api/vps/hosts/{id}/checks` | Minta pemeriksaan `health`/`wordpress` read-only |
| GET | `/api/vps/hosts/{id}/plans` | Daftar rencana operasional |
| POST | `/api/vps/hosts/{id}/plans` | Buat rencana `backup` atau `deployment` |

Contoh JSON registrasi:

```json
{"name":"VPS WordPress staging","hostname":"vps.example.org","ssh_user":"deploy","port":22}
```

Contoh WordPress audit:

```json
{"kind":"wordpress","root":"/var/www/site.example.org"}
```

Contoh rencana:

```json
{"kind":"backup","root":"/var/www/site.example.org","notes":"Uji restore di staging sebelum rilis"}
```

**Tidak ada API eksekusi plan:** rencana adalah daftar langkah untuk operator, bukan antrean perintah yang dapat dijalankan.

## Keterbatasan dan keamanan umum

- Single admin dan satu proses; belum multi-tenant/RBAC, High Availability atau multi-worker distributed.
- Memory terverifikasi tetap membutuhkan manusia memeriksa faktanya.
- Telegram Gateway optional dengan chat ID allowlist; tidak bisa menyetujui edit file melalui Telegram.
- Plugin/MCP Registry masih **metadata-only**, tidak menjalankan MCP server/client pihak ketiga.
- Browser Preview masih read-only, tidak menjadi jaminan sandbox SSRF lengkap.
- SSH checks tidak melakukan vulnerability scanning komprehensif, pemeriksaan checksum core WordPress, pemeriksaan malware berbasis konten, audit database, atau patch server.
- **Belum ada backup otomatis, database dump, restore, deployment produksi, auto-remediation, scheduled VPS scans, alert Telegram VPS, maupun full browser interaction.**
- Pengujian aktual dengan model AI live, Docker daemon, SSH staging/produksi, browser, dan Telegram publik belum dilakukan. Gunakan terlebih dahulu pada proyek/VM percobaan.

## Verifikasi

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q hermas
node --check static/app.js
```

Lihat **PANDUAN-INDONESIA.md** untuk langkah rinci dan contoh konfigurasi. Lisensi ada di `LICENSE`.
