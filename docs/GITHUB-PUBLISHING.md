# Publikasi Hermas ke GitHub dan membuka kontribusi

**Penting:** Panduan ini bersifat manual; pengunggahan belum dilakukan. Pemeriksaan lokal dapat lulus tetapi tidak menjamin keamanan produksi. File `.env`, SQLite, snapshot, file privat, dan SSH key tidak boleh diunggah.

## A. Paling sederhana: GitHub CLI

1. Masuk https://github.com, pasang Git dan GitHub CLI (`gh`).
2. Buka terminal di folder `hermas-agentic-ai` hasil ekstraksi paket GitHub-ready.
3. Jalankan:

   ```bash
   gh auth login
   git init
   git branch -M main
   git add .
   git status --short
   git commit -m "chore: initial public release of Hermas v0.6"
   gh repo create hermas-agentic-ai --public --source=. --remote=origin --push
   ```

   **Periksa `git status --short` sebelum commit!** Jika ada file rahasia terlanjur tercatat, batalkan staging dan bersihkan dari Git. Menghapus file setelah push tidak menghapus rahasia dari riwayat; lakukan rotasi token/kredensial.

## B. Tanpa GitHub CLI

1. Buat repository di https://github.com/new bernama `hermas-agentic-ai`, pilih **Public**. Jangan centang Initialize with README/.gitignore/license karena file sudah tersedia.
2. Di folder proyek jalankan:

   ```bash
   git init
   git branch -M main
   git add .
   git status --short
   git commit -m "chore: initial public release of Hermas v0.6"
   git remote add origin https://github.com/USERNAME/hermas-agentic-ai.git
   git push -u origin main
   ```

   Ganti `USERNAME` dengan username GitHub sendiri. GitHub tidak menerima password akun sebagai password Git HTTPS; gunakan Git Credential Manager, GitHub CLI, atau autentikasi yang didukung GitHub.

## C. Izinkan kontribusi umum tanpa akses tulis

- Repository harus **Public** untuk menerima fork publik.
- Pengunjung membuat **Fork → branch → Commit → Pull Request**; mereka tidak memerlukan undangan sebagai collaborator.
- Aktifkan **Settings → General → Features → Issues**; secara opsional aktifkan Discussions.
- `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, template issue/PR, dan CI sudah disediakan.
- Buka **Settings → Rules → Rulesets → New ruleset → New branch ruleset**, target `main`; aktifkan require pull request, require approval 1, block force push dan deletion. Konfirmasi CI sudah berjalan sebelum mewajibkan status check.
- Buat label `good first issue`, `help wanted`, `bug`, `enhancement` dan beberapa issue awal.
- Review kode dan CI sebelum merge, khususnya perubahan `hermas/tools.py`, `hermas/web.py`, dan akses SSH.

## D. Tim inti dengan akses tulis

Pada repository milik akun pribadi: **Settings → Collaborators → Add people** lalu undang username GitHub yang dipercayai. Undangan memberi hak tulis, jadi jangan lakukan pada semua orang. Jika butuh peran lebih granular, buat GitHub Organization dan atur team/role.

## E. Rilis dan pemeliharaan

- Setelah initial push, buat tag `v0.6.0` lalu GitHub Release dengan catatan bahwa ini MVP eksperimental.
- Aktifkan **Settings → Security → Code security and analysis** (misalnya secret scanning/Dependabot jika tersedia) dan **Private vulnerability reporting**.
- Baca PR/Issues secara berkala, buat changelog dan jalankan test sebelum merilis update.
- Jangan aktifkan GitHub Actions `pull_request_target` untuk menjalankan kode PR dari fork.

## F. Sebelum publikasi

- [ ] Nama pemilik/copyright dan lisensi MIT sudah disetujui (lihat `LICENSE`).
- [ ] Tidak ada kunci asli, credential, data pribadi, database, konfigurasi produksi, atau file backup dalam commit.
- [ ] README jelas menyatakan MVP dan batasan keamanan.
- [ ] `python -m pytest -q` lulus.
- [ ] GitHub Actions hanya punya izin read-only saat test PR.
- [ ] `main` dilindungi; PR dan review menjadi alur utama.
