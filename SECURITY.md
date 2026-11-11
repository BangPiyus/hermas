# Security Policy

## Status keamanan

Hermas v0.6.0 adalah MVP **belum diaudit secara independen**. Jangan memberi hak root pada agent atau menjalankannya langsung di server produksi. Tool remote sebaiknya dibatasi read-only dan nonaktif secara default.

## Melaporkan kerentanan

1. **Jangan membuat Issue publik** untuk kerentanan yang belum diperbaiki, credential leak, atau eksploit yang dapat membahayakan pengguna.
2. Gunakan **Security → Advisories → Report a vulnerability** pada repository GitHub **jika pemilik repository sudah mengaktifkan private vulnerability reporting**.
3. Jika opsi tidak tersedia, hubungi maintainer melalui saluran privat yang dicantumkan pada profil/repository. Jangan kirim exploit atau credentials di kanal publik.
4. Sertakan versi, dampak, langkah reproduksi minimal yang tidak berbahaya, dan saran mitigasi jika ada.

Kami belum menjanjikan SLA respons atau hadiah bug bounty. Jika laporan valid, kami akan berusaha memperbaiki masalah sebelum mengumumkannya.

## Konfigurasi aman minimum

- Jangan commit `.env`/database/runtime; gunakan `.env.example` untuk template.
- Bind dashboard ke localhost dan gunakan SSH tunnel atau reverse proxy berautentikasi ketika perlu akses jarak jauh.
- Hanya gunakan user SSH non-root; verifikasi host key di luar band.
- Jalankan Docker sandbox pada lingkungan terisolasi; Docker sendiri bukan jaminan keamanan absolut.
- Batasi hak akses GitHub Actions dan review dependency baru.
