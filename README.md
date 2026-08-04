# ZMUX

**ZMUX** adalah aplikasi terminal Android (*ZMUX Ember*) bernavigasi dan bergaya antarmuka asli (Native Kotlin UI) yang berjalan di atas engine PTY berbasis Python (`realpty.py` + PRoot/Alpine Linux).

---

## 🚀 CI/CD & Aktivitas Build GitHub Actions

### Mengapa Tidak Ada Aktivitas Build Sebelumnya?
1. Sebelumnya, repositori belum memiliki konfigurasi workflow di `.github/workflows/`.
2. Saat mencoba menambahkan workflow CI secara otomatis dari bot ini, GitHub menolak dengan pesan error:
   `refusing to allow a GitHub App to create or update workflow .github/workflows/ci.yml without workflows permission`
   Hal ini karena koneksi GitHub App di Arena saat ini belum mengaktifkan izin **`workflows`**.

### Bagaimana Repositori Ini Dioptimalkan ("Optimalkan Paksa")
Untuk tetap mengoptimalkan repositori secara paksa dan menyediakan alur CI/CD yang langsung siap pakai, kami telah menyiapkan seluruh konfigurasi workflow otomatis di dalam direktori **`ci/workflows/`**:

> **Cara mengaktifkan di GitHub:**  
> Pindahkan atau salin folder `ci/workflows/` menjadi `.github/workflows/` secara langsung via web GitHub (atau hubungkan ulang koneksi GitHub di Arena dengan izin *workflows* diaktifkan).

### 1. Workflow CI & Build Otomatis (`ci/workflows/ci.yml`)
Workflow CI dirancang untuk berjalan otomatis pada setiap `push`, `pull_request`, dan manual via `workflow_dispatch`:
- **`protocol-check` (RFC-6455 Protocol Verification)**
  - Menjalankan pengujian kepatuhan protokol RFC-6455 menggunakan Python 3.11 (`tools/protocol_check.py` melawan `tools/mock_pty_ws_server.py`).
  - Memvalidasi **9/9 gate protokol lulus 100%**, termasuk simulasi otentikasi `?token=`, kontrol `action`, ubah ukuran (`TIOCSWINSZ`), dan interupsi `Ctrl+C` (`0x03`).
- **`build-android-apk` (Build Android Kotlin Native UI APK & Lint)**
  - Menggunakan **Ubuntu Latest** dengan **JDK 17** dan **Gradle 8.7**.
  - Mengonfigurasi dan membuat Gradle Wrapper secara otomatis di lingkungan CI.
  - Menjalankan `./gradlew :app:assembleDebug --stacktrace` pada proyek `experimental/zmux-kotlin/`.
  - **Mengunggah Artefak APK (`zmux-kotlin-debug-apk`)**: Setiap build yang sukses akan menghasilkan artefak APK yang dapat langsung diunduh dari halaman *Actions* di GitHub.
  - Menjalankan analisis diagnostik **Android Lint** (`:app:lintDebug`) dan mengunggah laporan hasil analisis (`zmux-kotlin-lint-report`).
- **`ci-summary`**
  - Menampilkan ringkasan status build dan daftar artefak di *Job Summary* GitHub Actions.

### 2. Workflow Rilis APK (`ci/workflows/release.yml`)
- Dirancang untuk berjalan saat penanda rilis (tag `v*`) di-push ke repositori.
- Mem-build aplikasi Android dan menyediakan artefak APK rilis (`zmux-kotlin-release-apk`).

---

## 🛠️ Quickstart & Local Development

Untuk kenyamanan pengembangan lokal dan kesetaraan dengan lingkungan CI, repositori ini dilengkapi dengan **`Makefile`** di root direktori:

```bash
# 1. Menjalankan verifikasi kepatuhan protokol RFC-6455 (9/9 gate lulus)
make test

# 2. Mem-build aplikasi Android (APK Debug) di experimental/zmux-kotlin/
make build

# 3. Menjalankan analisis Android Lint
make lint

# 4. Membersihkan artefak build
make clean
```

### Memeriksa Protokol Secara Manual (Tanpa Make)
```bash
python3 experimental/zmux-kotlin/tools/mock_pty_ws_server.py --port 8011 --token dev &
python3 experimental/zmux-kotlin/tools/protocol_check.py --port 8011 --token dev
```

---

## 📂 Struktur Repositori & Referensi Dokumentasi

- [`docs/KOTLIN_RUST_DECISION.md`](docs/KOTLIN_RUST_DECISION.md)  
  Analisis dan keputusan arsitektur: menggunakan **Kotlin untuk lapisan UI** dan memanfaatkan `terminal-emulator` Termux (Apache-2.0) sebagai parser ANSI & buffer layar, sekaligus mempertahankan engine **Python AGPL-3.0** (`realpty.py`, `zpip`, PRoot/Alpine) tanpa adopsi Rust.
- [`docs/PROGRESS_KOTLIN_UI_POC.md`](docs/PROGRESS_KOTLIN_UI_POC.md)  
  Laporan kemajuan, skema arsitektur, dan spesifikasi protokol WebSocket ZMUX.
- [`experimental/zmux-kotlin/`](experimental/zmux-kotlin/)  
  Implementasi PoC aplikasi Android Kotlin Native UI (*ZMUX Ember*), termasuk kontrak protokol (`ZmuxProtocol.kt`), jembatan WebSocket (`WebSocketPtyBridge.kt`), sesi terminal tanpa fork lokal (`ZmuxTerminalSession.kt`), serta tombol virtual (`ZmuxKeys.kt`).
- [`Makefile`](Makefile)  
  Perintah standar pengembangan untuk pengujian protokol dan build aplikasi.
