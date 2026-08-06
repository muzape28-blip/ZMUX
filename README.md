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

## 🛡️ Perbaikan "Permission Denied" Total (Android W^X) & Auto-Reopen Linux

Gejala lama saat aplikasi ditutup lalu dibuka lagi:

```
: /data/user/0/com.zmux.terminal.debug/files/.zmuxrc[1]:
  /data/user/0/com.zmux.terminal.debug/files/bin/clear: Permission denied
```

**Akar masalah:** APK ini menargetkan SDK 34, dan sejak Android 10 (target SDK 29+) kernel/SELinux melakukan blokir `execve()` untuk *setiap* file reguler di direktori privat aplikasi (`/data/user/0/<pkg>/files/...`) — aturan **W^X**. `chmod 755` tidak menolong; `execve` selalu dijawab `EACCES` → mksh mencetak `Permission denied`. Backend Python memang menulis wrapper CLI (`files/bin/clear`, `help`, `zpip`, ...) dan shell bootstrap Kotlin menaruh `files/bin` di urutan **pertama** `PATH`, sehingga `clear` di `.zmuxrc` menabrak wrapper tersebut.

**Perbaikan menyeluruh (semua jalur):**
1. **Shell bootstrap (Kotlin)** — `PATH` kini *system-only* (`/system/bin:/system/xbin:/vendor/bin`); `files/bin` tidak pernah ada di `PATH` sehingga `clear` kembali ke toybox. `linux-setup` tetap jalan lewat interpreter `sh <path>` (membaca file tidak butuh izin exec).
2. **Auto-reopen lingkungan Linux** — jika rootfs Alpine/Debian sudah terinstal dan terverifikasi (`bin/sh` guest dengan symlink absolut busybox ikut diresolve, marker `etc/.zmux-rootfs`, `libproot.so` executable), `createNewSession()` langsung membuka PTY PRoot → guest `/bin/sh -l`. Pengguna tidak lagi terdampar di shell bootstrap setelah *close/re-open*, dan tab `[+]` baru ikut konsisten.
3. **Peluncur PRoot Kotlin setara Python** — self-heal SONAME `libtalloc` ke `files/lib`, bind storage hanya untuk path yang benar-benar *readable+executable* (`/sdcard` dsb. dilewati tanpa izin), bind `resolv.conf`, dan mountpoint guest dibuat otomatis.
4. **Runtime Python** — `zmux.paths.android_exec_blocked()` mendeteksi sandbox W^X: wrapper tetap ditulis + `chmod 0755` (untuk dipakai via interpreter/desktop), tetapi `BIN_DIR` **tidak** lagi di-prepend ke `PATH`, konten lama di `os.environ["PATH"]` di-scrub saat import, dan semua pembangun env (`zmux.env.build_path`, `terminal._build_env`) menjamin tidak ada entri `PATH` milik direktori privat aplikasi.

**Gate pengujian baru** — `tests/test_wx_permission_safety.py` (15 gates): simulasi runtime Android pada interpreter bersih, emulasi resolusi mksh, *eksekusi byte-asli `.zmuxrc` yang diekstrak dari sumber Java* di bawah `bash` dengan direktori `bin` berisi jebakan (negatif-kontrol ikut membuktikan layout lama pasti menembak jebakan), dan kontrak statik kode Kotlin/Java yang terpasang.

```bash
make test   # 9/9 protokol + 8/8 installer + 15/15 W^X + 23/23 APP_DIR — semua hijau
```

---

## 📍 Perbaikan "Rootfs path disappeared" (APP_DIR Chaquopy vs `filesDir`)

Gejala: `linux-setup` sukses total (checksum terverifikasi, `rootfs installed and verified
successfully!`), tetapi layar langsung menampilkan:

```
[Chaquopy Error] Rootfs path disappeared before PRoot launch:
/data/user/0/com.zmux.terminal.debug/files/linux/rootfs
```

**Akar masalah:** Chaquopy bukan python-for-android. Ia tidak pernah menyetel `ANDROID_PRIVATE`/
`ANDROID_ARGUMENT`/`ANDROID_APP_PATH`, dan mengekstrak sumber Python aplikasi ke
`<filesDir>/chaquopy/AssetFinder/app`. Akibatnya `zmux.paths.resolve_app_dir()` jatuh ke langkah
`__file__` (`Path(__file__).parent.parent`), menemukan direktori ekstraksi itu — yang memang
*writable* — lalu memakainya sebagai `APP_DIR`. Rootfs terpasang di
`<AssetFinder>/linux/rootfs`, sementara `ZmuxTerminalActivity` mencari di `filesDir`. Ketidakcocokan
yang sama berlaku untuk **semua** turunan `APP_DIR`: wrapper `bin`, `.zmux_auth_token` (dibaca
`ZmuxBackendLocator`), `cache`, dan `logs`.

**Perbaikan (satu sumber kebenaran, Kotlin = host yang berwenang):**
1. **Kontrak `APP_DIR` (Kotlin)** — `ZmuxTerminalActivity.onCreate` mengekspor
   `Os.setenv("ANDROID_PRIVATE", filesDir, true)` **sebelum** `Python.start()` dan sebelum impor
   modul `zmux` pertama (`APP_DIR` diresolve saat *import*). Efek samping positif: penjaga W^X
   (`android_exec_blocked()`) yang sebelumnya diam-diam mati di Chaquopy kini aktif.
2. **Tidak ada tebakan path di Kotlin** — `launchLinuxSession()` dan `detectInstalledLinux()`
   meminta `zmux.linuxenv.rootfs_dir()` / `home_dir()`. Pesan galat kini mencetak path yang
   benar-benar dikembalikan Python, bukan literal buatan tangan.
3. **Jaring pengaman Python** — `resolve_app_dir()` menolak pohon aset Chaquopy sebagai root
   runtime; tanpa ekspor host pun `APP_DIR` jatuh ke `HOME` (= `filesDir` di Chaquopy). Jalur p4a
   (`ANDROID_ARGUMENT`/`ANDROID_APP_PATH`) dan desktop/CI tidak berubah.
4. **Migrasi instalasi lama** — `linuxenv.migrate_legacy_install()` mengadopsi rootfs yang
   terdampar di pohon AssetFinder lewat *rename* (satu filesystem, O(1) — tidak pernah menyalin di
   UI thread), menggabungkan isi `home` lama tanpa menimpa berkas yang sudah ada, membersihkan
   direktori kosong, dan melaporkan apa pun yang sengaja tidak disentuh. Dipanggil saat startup dan
   di awal `install()`.
5. **Kepemilikan berkas di `files/bin`** — karena `APP_DIR` kini sama dengan `filesDir`, generator
   wrapper Python dan skrip bootstrap Java menulis ke direktori yang sama; skrip host diganti nama
   menjadi `zmux-linux-setup` sehingga wrapper `linux-setup` bikinan Python tidak bisa lagi
   menimpanya.

**Gate pengujian baru** — `tests/test_app_dir_alignment.py` (23 gates): reproduksi layout Chaquopy
apa adanya (probe interpreter bersih dengan `files/chaquopy/AssetFinder/app`), prioritas
`resolve_app_dir()`, kesetaraan seluruh path yang dikonsumsi Kotlin, migrasi instalasi lama, tabrakan
nama berkas `files/bin`, serta kontrak statik sumber Kotlin (tanpa literal `linux/rootfs`, path dari
`linuxenv`, urutan `Os.setenv` sebelum `Python.start`). Suite ini juga ikut dijalankan otomatis dari
`tests/test_wx_permission_safety.py` (protokol `load_tests`, gagal keras bila filenya hilang),
sehingga langkah CI yang sudah ada tetap memverifikasinya.

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
