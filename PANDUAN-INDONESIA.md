# Panduan Hermas Agentic AI v0.6.0

**Status:** pengembangan fitur MVP, belum diuji ke akun Telegram dan VPS sebenarnya. Semua fitur jaringan yang baru **nonaktif secara default**. Kamu bisa memasang Hermas nanti setelah seluruh fitur yang diinginkan selesai.

## 1. Fitur utama v0.6

- **Role & akun:** `admin` (akses penuh), `operator` (lihat monitoring, jalankan health check read-only yang sudah diizinkan, akui alert), `viewer` (monitoring baca-saja). Akun tambahan tidak bisa membuka percakapan, source code, secrets, memori, approvals, atau mengelola akun.
- **Scheduled VPS monitoring:** health / audit WordPress berbasis perintah SSH tetap, interval minimal 5 menit, disable default, koalesensi jadwal terlewat.
- **Alerts:** `open`, `acknowledged`, `resolved`; masalah host/jenis yang sama tidak membuat alert baru berulang. Pengiriman Telegram harus dinyalakan terpisah.
- **Workspace snapshots:** simpan kode lokal Hermas di `data/snapshots`, baca diff rencana restore, token 10 menit, tulis `RESTORE` untuk mengonfirmasi. Tidak menyentuh VPS atau database.
- **Operations dashboard:** klik ikon bel untuk ringkasan kesehatan, monitor, alert, dan status audit; ikon `users` untuk akun; ikon `hard-drive` untuk snapshot.

## 2. Instalasi pertama (tanpa konfigurasi server)

### Windows 11

1. Instal Python 3.11+ dan pastikan dapat dipanggil dari Command Prompt.
2. Ekstrak `Hermas-Agentic-AI-v0.6.0.zip`.
3. Masuk ke folder `hermas-agentic-ai`, lalu jalankan `start.bat`.
4. Salin password admin acak yang muncul pada terminal. Username awal adalah **`admin`**.
5. Buka **http://127.0.0.1:8765**.
6. Coba fitur panel bel, akun, dan snapshot. Jangan mengaktifkan SSH atau Telegram sebelum siap mengujinya di staging.

### Ubuntu / Linux

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git openssh-client unzip
unzip Hermas-Agentic-AI-v0.6.0.zip
cd hermas-agentic-ai
chmod +x start.sh
./start.sh
```

Untuk mengakses VPS dari komputer sendiri, gunakan SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 deploy@IP-VPS
```

Jangan membuka port 8765 ke Internet secara langsung. Jalankan aplikasi sebagai Linux user non-root.

## 3. Upgrade dari v0.5 atau sebelumnya

**Jangan menimpa source lama tanpa backup.**

1. Matikan seluruh proses Hermas lama.
2. Salin `.env`, folder `data/` dan `workspace/` dari folder lama setelah semua proses berhenti. Backup WAL/SHM SQLite jika masih ada.
3. Ekstrak v0.6 ke folder baru dan salin berkas data tersebut ke folder baru.
4. Jika `.env` memakai path absolut ke folder lama, sesuaikan `HERMAS_WORKSPACE` dan `HERMAS_DATA_DIR`.
5. Jalankan v0.6. Database akan dimigrasikan otomatis menjadi **schema 6**, dengan riwayat versi sebelumnya dipertahankan.
6. Cek task yang terinterupsi, approvals, dan workflow lama sebelum menggunakan scheduler.

## 4. Menambah akun Viewer dan Operator

1. Login sebagai `admin`.
2. Buka ikon **users** pada toolbar.
3. Masukkan username (3–32 karakter huruf kecil/angka/._-) dan password **minimal 14 karakter**.
4. Pilih `Viewer` atau `Operator`, lalu buat akun.
5. Dari panel yang sama, akun dapat dinonaktifkan, diubah perannya, atau direset password. Seluruh sesi lama akan tercabut ketika perubahan diterapkan.

Akun tambahan **tidak boleh** menjadi admin. Kredensial admin utama tetap berada di `.env`. Akun viewer/operator hanya melihat informasi operasi yang dibutuhkan dan tidak membuka isi percakapan AI.

## 5. Memasang Scheduled VPS Monitoring (nanti saat pengujian staging)

1. Buat VPS inventory via ikon **server**. Server otomatis dalam keadaan nonaktif.
2. Siapkan user Linux non-root, SSH key khusus dan `known_hosts` yang fingerprint-nya telah diverifikasi lewat panel provider VPS atau kanal independen. Jangan pernah mematikan `StrictHostKeyChecking`.
3. Batasi permission key Linux dengan `chmod 600` dan simpan di luar direktori proyek.
4. Edit `.env`, contoh:

```dotenv
HERMAS_VPS_SSH_ENABLED=true
HERMAS_VPS_SSH_KEY_PATH=/home/hermas/.ssh/hermas_ed25519
HERMAS_VPS_KNOWN_HOSTS_PATH=/home/hermas/.ssh/known_hosts
HERMAS_VPS_MONITOR_ENABLED=true
```

5. Restart Hermas. Aktifkan host yang akan dipantau pada VPS Commander.
6. Buka ikon **bel**, pilih host, tipe `health` atau `wordpress`, interval misalnya `30` menit, lalu **Tambah monitor**. Monitor dibuat **nonaktif**.
7. Klik Aktifkan. Sistem memeriksa kesiapan konfigurasi SSH dan host, kemudian menjadwalkan pemeriksaan pertama. Mode WordPress hanya menerima root absolut di `/var/www/...` atau `/srv/www/...`.
8. Lihat hasil pada panel bel atau histori di VPS Commander.

Seluruh pemeriksaan tetap read-only. Tidak ada perintah hapus, update, restart, restore atau deployment otomatis. Scan SSH nyata belum diuji bersama server milik pengguna.

## 6. Konfigurasi Telegram Alerts (opsional)

Untuk alert saja tanpa menjalankan perintah Telegram:

```dotenv
HERMAS_ALERT_TELEGRAM_ENABLED=true
HERMAS_TELEGRAM_ENABLED=false
HERMAS_TELEGRAM_TOKEN=123456789:bot-token-anda
HERMAS_TELEGRAM_CHAT_IDS=123456789
```

Token/ID tersebut hanya contoh. Pastikan chat ID sesuai allowlist. Restart aplikasi. **Hanya alert baru yang dibuat setelah fitur ini aktif** yang masuk antrean pengiriman; tidak ada pengiriman ulang untuk alert historis. Teks notifikasi dibatasi pada status/kategori pemeriksaan, **tanpa mengirim IP, hostname, file, atau detail SSH**. Telegram timeout diberi status `unknown` sehingga operator perlu memeriksa dan tidak terjadi retry otomatis berisiko ganda.

Tidak ada tombol Telegram untuk menyetujui perubahan kode atau eksekusi perintah root.

## 7. Membuat snapshot dan memulihkan workspace lokal

1. Login sebagai admin, klik ikon **hard-drive**.
2. Klik **Buat snapshot source lokal**. Hermas menyimpan maksimal 200 file source yang diizinkan, ukuran per file ≤4 MiB dan total ≤32 MiB.
3. Pada snapshot tersimpan, klik **Preview restore**. Sistem memverifikasi hash arsip dan file lalu menampilkan `create`, `overwrite`, atau `unchanged`.
4. Setelah memastikan perubahan, ketik **RESTORE** dan klik **Konfirmasi restore workspace**. Token preview berlaku 10 menit. Jika file berubah sesudah preview, restore ditolak.
5. Periksa source dan jalankan test setelah restore. Ini bukan operasi atomik seluruh workspace; jika ada kegagalan, rollback file lokal dilakukan sebaik mungkin tetapi tetap memerlukan verifikasi.

**Penting:** Snapshot tidak mengambil `.env`, file hidden, database, symlink, dan sejumlah nama yang terlihat menyimpan credentials. Deteksi nama file tidak menjamin tidak ada secrets di file lain. Simpan `data/` secara privat, dan tetap miliki backup terpisah untuk database dan server.

**Belum tersedia:** snapshot filesystem VPS, SQL dump MySQL/MariaDB, restore database, restore WordPress remote, atau deployment produksi otomatis.

## 8. API yang bisa digunakan

Semua endpoint butuh cookie login, mutasi butuh header `X-Hermas-CSRF` dari `/api/login` atau `/api/me`. Contoh:

| Method | Endpoint | Izin |
|---|---|---|
| GET / POST | `/api/accounts` | Admin |
| POST | `/api/accounts/{id}` | Admin |
| GET | `/api/operations/summary` | Viewer+ |
| GET / POST | `/api/operations/monitors` | Viewer / Admin |
| POST | `/api/operations/monitors/{id}/enabled` | Admin |
| GET | `/api/operations/alerts` | Viewer+ |
| POST | `/api/operations/alerts/{id}/acknowledge` | Operator+ |
| GET | `/api/operations/deliveries` | Admin |
| GET / POST | `/api/snapshots` | Admin |
| GET | `/api/snapshots/{id}/preview` | Admin |
| POST | `/api/snapshots/{id}/restore` | Admin |

Semua endpoint lama untuk agent, tools, tasks, approval, Git, workflow dan VPS tetap ada. Role viewer/operator ditolak pada endpoint yang hanya untuk admin.

## 9. Pengujian manual dan otomatis

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q hermas
node --check static/app.js
```

Pengujian yang disertakan mencakup migrasi database, pencabutan sesi, RBAC, scheduler, alert de-dup, opt-in Telegram, snapshot, preview yang kedaluwarsa/berubah, dan restore lokal. Tes otomatis memakai mock/offline; **belum menguji provider LLM nyata, SSH staging, Telegram publik, Docker daemon, maupun interaksi browser lengkap**.

## 10. Rekomendasi pemakaian

Untuk sekarang gunakan Hermas dalam **mode demo** dan staging. Jangan memberikan akses root atau menaruh API key/SSH private key di workspace/chat. Setelah semua versi selesai, lakukan audit keamanan dan uji sistem end-to-end sebelum menghubungkan situs atau VPS produksi.
