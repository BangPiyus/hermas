# Contributing to Hermas Agentic AI

Terima kasih sudah ingin berkontribusi. Proyek ini masih **MVP untuk eksperimen**, bukan produk yang aman untuk produksi tanpa pengawasan.

## Alur kontribusi (tanpa akses tulis langsung)

1. Baca `README.md`, `SECURITY.md`, dan cek Issues agar pekerjaan tidak duplikat.
2. Cari Issue berlabel `good first issue` atau buka Issue dahulu untuk perubahan besar.
3. **Fork** repository ini, lalu buat branch dari `main`, misalnya `feat/log-export` atau `fix/session-expiry`.
4. Ubah kode, dokumentasikan perubahan, dan **tambahkan pengujian** untuk bug/perilaku baru.
5. Jalankan:

   ```bash
   python -m pip install -r requirements-dev.txt
   python -m pytest -q
   python -m compileall -q hermas tests
   ```

6. Commit menggunakan pesan bermakna, misalnya `fix: prevent duplicate approval execution`.
7. Push branch ke fork Anda, lalu buat **Pull Request** ke `main` upstream.
8. Tunggu pemeriksaan CI dan review. Maintainer berhak meminta perubahan sebelum merge.

## Prinsip kontribusi

- Jaga kompatibilitas API, upgrade database, dan perilaku fitur lama sebisa mungkin.
- Semua operasi destruktif harus mempunyai approval eksplisit, batas akses, dan audit.
- Jangan mengaktifkan SSH, Telegram, atau akses jaringan eksternal sebagai default.
- Jangan commit `.env`, token API, private key, database, log, dump, atau data pengguna.
- Jangan menambahkan dependensi luar tanpa menjelaskan risiko supply-chain dan lisensinya.
- Hindari perubahan fitur besar dalam satu Pull Request; buat PR kecil yang mudah direview.
- Untuk perubahan keamanan, sertakan threat model sederhana dan pengujian penolakan akses.
- Jangan mengeksekusi kode dari PR yang tidak dipercaya pada VPS atau workstation berisi kredensial produksi.

## Cara menjalankan secara lokal

Lihat `README.md` atau `PANDUAN-INDONESIA.md`. Gunakan provider `demo` untuk pengujian tanpa API key. Python 3.11+.

## Apa yang bisa dikerjakan?

Mulai dari perbaikan dokumentasi, tes, bug UI, validasi input, observabilitas, performance, memori, atau keamanan. Pekerjaan yang memerlukan akses server produksi **tidak** diterima sebagai prasyarat PR.

## Lisensi kontribusi

Dengan mengirim kontribusi, Anda menyatakan berhak menyumbangkan perubahan tersebut dan setuju kontribusinya didistribusikan sesuai lisensi MIT proyek ini. Jika membawa kode orang lain, sertakan atribusi dan lisensi yang kompatibel.
