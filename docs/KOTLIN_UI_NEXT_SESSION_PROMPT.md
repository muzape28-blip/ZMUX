# PROMPT INSTRUKSI — Sesi Selanjutnya
## Implementasi Kotlin Native UI untuk ZMUX (Level B)

**Copy-paste prompt ini ke sesi agent berikutnya.**

---

### 1. KONTEKS LENGKAP (WAJIB DIBACA)

Kamu sedang bekerja di repo `muzape28-blip/ZMUX` pada branch `arena/019fcd8f-zmux`.

**Status saat ini (per 2026-08-04):**
- Seluruh core ZMUX sudah di-import dari https://github.com/muzape28-blip/ZABAWHEELS
- Engine terminal sudah menggunakan **real PTY** (`app/zmux/realpty.py`)
- UI saat ini masih **WebView + xterm.js** (`app/templates/terminal.html`)
- Sudah ada analisis mendalam:
  - `docs/KOTLIN_UI_ANALYSIS.md`
  - `docs/DISCUSSION_KOTLIN_UI.md`
  - `docs/ZABAWHEELS_TREASURE_FOR_KOTLIN.md`
  - `docs/RUST_KOTLIN_ANALYSIS.md` (dari ZABAWHEELS)
  - `docs/REFERENCE_MINING.md` (harta karun referensi)

**Kesimpulan konsensus:**
- **Hanya ganti lapisan UI** dengan Kotlin.
- **JANGAN** sentuh engine PTY / PRoot / Alpine / zpip.
- Rekomendasi utama: **Level B** — Kotlin + `TerminalView` Termux + backend Python tetap.
- Mulai dengan **jalur transisi** (WebSocket) sebelum pindah ke Chaquopy.

---

### 2. TUJUAN SESI INI

Buat **proof-of-concept (PoC)** Kotlin Native Terminal UI yang bisa menggantikan WebView + xterm.js.

**Pendekatan yang direkomendasikan (P1a):**
1. Buat proyek Android Studio baru di `experimental/zmux-kotlin/`
2. Gunakan `com.termux:terminal-view` (Apache-2.0)
3. Hubungkan via **WebSocket** ke backend Python yang sudah ada (`ws_server.py` port 8001)
4. Nanti (sesi berikutnya) bisa upgrade ke Chaquopy untuk menghapus WebSocket layer.

**Tujuan utama sesi ini:**
- Skeleton proyek Android Studio yang **bisa build**
- `TerminalView` yang menerima input dan menampilkan output
- Koneksi WebSocket ke server ZMUX yang sudah jalan
- Minimal 1 sesi terminal yang berfungsi (bisa ketik perintah & lihat output)

---

### 3. LANGKAH-LANGKAH YANG HARUS DILAKUKAN (IKUTI URUTAN INI)

#### Langkah 0 — Persiapan
- Baca dulu file-file ini (dalam urutan):
  1. `docs/ZABAWHEELS_TREASURE_FOR_KOTLIN.md`
  2. `docs/DISCUSSION_KOTLIN_UI.md`
  3. `docs/KOTLIN_UI_ANALYSIS.md`
  4. `docs/REFERENCE_MINING.md` (bagian tentang Termux terminal-view & ReTerminal)
  5. `app/zmux/realpty.py` (pahami bagaimana PTY bekerja)
  6. `app/zmux/ws_server.py` (pahami protokol WebSocket)

#### Langkah 1 — Struktur Proyek
Buat struktur berikut di `experimental/zmux-kotlin/`:

```
experimental/zmux-kotlin/
├── app/
│   ├── build.gradle.kts
│   ├── src/main/
│   │   ├── AndroidManifest.xml
│   │   ├── java/com/zmux/terminal/
│   │   │   ├── ZmuxTerminalActivity.kt
│   │   │   ├── ZmuxTerminalView.kt
│   │   │   ├── WebSocketPtyBridge.kt
│   │   │   └── MainActivity.kt (atau langsung pakai ZmuxTerminalActivity)
│   │   └── res/
│   └── src/main/python/          # (opsional untuk Chaquopy nanti)
├── build.gradle.kts
├── settings.gradle.kts
├── gradle.properties
└── README.md
```

#### Langkah 2 — Dependencies (build.gradle.kts)
Gunakan versi stabil:
- `com.termux:terminal-view:0.118.0` (atau versi terbaru yang kompatibel)
- OkHttp untuk WebSocket
- Kotlin + AndroidX
- Target: `minSdk 26`, `compileSdk 34`, `targetSdk 34`
- ABI: `armeabi-v7a` + `arm64-v8a`

#### Langkah 3 — Implementasi Inti
Buat komponen berikut:

1. **ZmuxTerminalView.kt**
   - Extend / wrap `com.termux.view.TerminalView`
   - Hubungkan ke `TerminalEmulator`

2. **WebSocketPtyBridge.kt**
   - Koneksi WebSocket ke `ws://127.0.0.1:8001`
   - Kirim input user → WebSocket
   - Terima byte dari WebSocket → `terminalView.mEmulator.append()`
   - Handle resize (`{"type":"resize","rows":...,"cols":...}`)

3. **ZmuxTerminalActivity.kt**
   - Fullscreen / portrait
   - Inisialisasi TerminalView
   - Virtual keys sederhana (Ctrl, Esc, Tab, dll) — minimal dulu
   - Tombol "Connect" / auto-connect

**Catatan protokol WebSocket ZMUX (penting):**
- Lihat `app/zmux/ws_server.py` dan `app/zmux/pty_session.py` atau `realpty.py`
- Biasanya pakai pesan binary untuk data terminal
- Ada handshake auth token (lihat `docs/SECURITY.md`)

#### Langkah 4 — Testing & Integrasi
- Buat instruksi cara menjalankan backend ZMUX terlebih dahulu (Buildozer atau `python main.py`)
- Di emulator/device, jalankan app Kotlin dan hubungkan ke localhost
- Verifikasi:
  - Bisa ketik perintah (`ls`, `echo`, `python3`)
  - Output muncul real-time
  - Resize window bekerja
  - Ctrl+C berfungsi

#### Langkah 5 — Dokumentasi
- Update / buat `experimental/zmux-kotlin/README.md`
- Tambahkan langkah build & run
- Buat catatan "Known Issues" dan "Next Steps" (menuju Chaquopy)

#### Langkah 6 — (Opsional tapi bagus)
- Tambahkan basic virtual keyboard bar (dari pola di REFERENCE_MINING.md)
- Buat single-file `build.gradle.kts` yang mudah di-copy

---

### 4. ATURAN KETAT (JANGAN DILANGGAR)

- **JANGAN** menulis ulang `realpty.py`, `linuxenv.py`, `zpip`, atau engine PTY ke Kotlin.
- **JANGAN** hapus pipeline Buildozer yang sudah ada.
- Gunakan **Apache-2.0** `terminal-view` (bukan salin kode GPLv3 dari Termux full app).
- Target utama: **ARMv7 + Android Go** (Infinix Smart 9 HD class).
- Pertahankan diferensiator Python: zpip, `zmux-info`, embedded runtime.
- Lisensi akhir proyek tetap **AGPL-3.0**.

---

### 5. TEKNOLOGI YANG HARUS DIPAKAI

- **Kotlin** (bukan Java)
- **Jetpack Compose** (opsional di tahap awal, boleh pakai XML dulu jika lebih cepat)
- **`com.termux:terminal-view`** (dari Maven)
- **OkHttp WebSocket** (untuk transisi)
- Nanti (bukan sekarang): **Chaquopy** untuk integrasi Python langsung

---

### 6. DELIVERABLES YANG DIHARAPKAN

Di akhir sesi, repo harus punya:

1. Folder `experimental/zmux-kotlin/` yang **bisa di-build** di Android Studio
2. File `README.md` di dalam folder tersebut yang menjelaskan cara menjalankan
3. Minimal 1 activity yang menampilkan terminal native dan terhubung ke ZMUX backend via WebSocket
4. Update ke `docs/DISCUSSION_KOTLIN_UI.md` atau file baru yang mencatat progress
5. (Bonus) Screenshot atau catatan hasil testing di emulator/device

---

### 7. PROMPT TAMBAHAN (JIKA AGENT BINGUNG)

Kalau agent bertanya "dari mana mulai?", jawab:

> "Mulai dari Langkah 1. Buat struktur proyek Android Studio terlebih dahulu. Setelah `settings.gradle.kts` dan `build.gradle.kts` bisa sync, baru buat Activity + TerminalView. Jangan langsung ke Chaquopy."

---

### 8. REFERENSI CEPAT

- `app/zmux/realpty.py` — PTY engine
- `app/zmux/ws_server.py` — WebSocket server
- `docs/REFERENCE_MINING.md` — pola Termux & ReTerminal
- `docs/ZABAWHEELS_TREASURE_FOR_KOTLIN.md` — ringkasan harta karun
- Termux terminal-view Maven: `com.termux:terminal-view`

---

**Sekarang salin seluruh isi file ini dan tempelkan ke sesi agent berikutnya.**

Selamat bekerja! Fokus pada **transisi WebSocket dulu** — itu adalah jalan paling aman dan cepat untuk membuktikan konsep Level B.