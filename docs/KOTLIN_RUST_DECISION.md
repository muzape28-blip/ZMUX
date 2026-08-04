# Kotlin & Rust untuk ZMUX — Analisis Setelah Membaca Semua Referensi

**Tanggal:** 2026-08-04
**Branch:** `arena/019fcda3-zmux`
**Metode:** membaca kode ZABAWHEELS yang sebenarnya (7.544 baris Python di `app/zmux/`),
bukan menebak dari dokumentasi.

---

## 0. Temuan yang mengubah arah sesi ini

Checkout `ZMUX` ini **kosong** (cuma `README.md`). Semua referensi ada di
`muzape28-blip/ZABAWHEELS`, jadi gw clone dan baca langsung dari sana.

Dan di sana ada satu dokumen yang **menolak rencana sesi ini secara eksplisit**:

> `docs/RUST_KOTLIN_ANALYSIS.md` (2026-07-31)
> **"Do not adopt Rust or Kotlin. Adopt the PTY instead."**

Ini bukan opini sambil lalu — dokumen itu punya angka, eksperimen, dan tabel biaya.
Menariknya: **rekomendasi utamanya sudah dijalankan.** `app/zmux/realpty.py` sekarang ada,
PTY-nya nyata (`os.openpty` + `fork` + `setsid` + `TIOCSCTTY`), lengkap dengan 6 probe
on-device. Jadi argumen terkuat dokumen itu — *"PTY tidak butuh native code"* — **sudah
terbukti benar dan sudah selesai**.

Yang tersisa dari dokumen itu justru satu celah yang dia akui sendiri:

> "**The honest one is the UI**: Jetpack Compose would be smoother and more native than a
> WebView, and would remove the loopback-server/token/CSP machinery."

**Jadi posisi sekarang: Kotlin untuk UI = sah. Rust = tetap ditolak.** Alasannya di §3.

---

## 1. Apa yang sebenarnya ada di ZABAWHEELS

| Modul | Baris | Fungsi |
|---|---:|---|
| `zpip.py` | 1.170 | package manager (path-traversal guard, SHA-256 wajib, transaksional + rollback) |
| `ws_server.py` | 406 | WebSocket RFC-6455 murni Python, loopback + token |
| `realpty.py` | ~380 | **PTY asli** — fork/setsid/TIOCSCTTY + 6 probe |
| `sessions.py` | ~215 | multi-session, cap 8, scrollback replay |
| `linuxenv.py`, `zpip`, `elfscan.py` | — | PRoot/Alpine, ELF scanning |
| **Total** | **7.544** | |

Ini bukan proyek kecil, dan **engine-nya sudah benar**. Yang lemah cuma lapisan UI.

---

## 2. Protokol WebSocket yang SEBENARNYA

Ini bagian di mana asumsi gw di sesi sebelumnya **salah total**. Perbandingan:

| Aspek | Tebakan gw (sesi lalu) | **Kenyataan** (`ws_server.py`) |
|---|---|---|
| Auth | header `Authorization: Bearer` | **`?token=` di query string URL** |
| Gagal auth | close frame | **HTTP 401 sebelum upgrade** |
| Kunci kontrol | `"type"` | **`"action"`** |
| Resize | `{"type":"resize","rows","cols"}` | **`{"action":"resize","cols","rows"}`** |
| Multi-session | tidak ada | **ada** — `session.new/switch/close/list` |
| Saat connect | tidak ada | **reset alt-buffer → scrollback → sessions** |
| Port | 8001 tetap | **dinamis**, `HTTP_PORT + 1` lalu fallback |

Kontrak lengkap sekarang ditranskrip ke `ZmuxProtocol.kt` beserta rujukan baris Python-nya.

### Detail yang gampang bikin bug

1. **Sniff JSON itu ketat.** `_handle_client_message` hanya menganggap pesan sebagai kontrol
   kalau teks *setelah di-trim* diawali `{` **dan** diakhiri `}`. Kalau tidak, itu input PTY
   mentah. Jadi ngetik `{"a":1}` di shell tetap aman.
2. **`RESET_TERMINAL_SCREEN`** = `\x1b[?1049l\x1b[?47l\x1b[?1047l\x1b[2J\x1b[H`. Dikirim saat
   reconnect supaya tab shell biasa tidak mewarisi layar Vim/less yang basi.
3. **401 tidak boleh di-retry.** Token salah tidak akan membaik dengan backoff.
   `WebSocketPtyBridge` menghentikan reconnect dan lapor `UNAUTHORIZED`.

---

## 3. Rust: tetap TIDAK. Ini alasan yang diperbarui.

`RUST_KOTLIN_ANALYSIS.md` menolak Rust karena PTY tidak butuh native. Itu sudah terbukti.
Tapi sekarang ada **alasan baru yang lebih kuat**, khusus untuk kasus UI native:

> Kalau UI pindah ke Kotlin + `terminal-view`, maka **`terminal-emulator` milik Termux
> (Apache-2.0) sudah menjadi parser ANSI + grid + scrollback yang matang dan teruji di jutaan
> device.** Menulis engine Rust seperti Rin (5.558 baris: `ansi.rs`, grid, cell buffer, cursor)
> berarti menulis ulang sesuatu yang **sudah kita dapat gratis**.

Jadi logikanya konsisten di dua rezim:

| Rezim UI | Siapa yang jadi terminal emulator | Perlu Rust? |
|---|---|---|
| WebView (sekarang) | xterm.js (JS) | Tidak |
| **Kotlin native (usulan)** | **`terminal-emulator` Termux (Apache-2.0)** | **Tidak** |
| Compose + surface sendiri (jalur Rin) | harus tulis sendiri | Ya — makanya Rin butuh Rust |

**Rust hanya masuk akal kalau kita menolak `terminal-emulator`.** Dan tidak ada alasan
menolaknya: Apache-2.0, kompatibel AGPL, dipakai ReTerminal, matang.

Biaya yang dihindari kalau Rust tidak diambil:
- tidak menambah `cargo-ndk` + crate lockfile ke `toolchain/runtime-lock.json`
- kontrak reproducible build (`runtime_id = zmux-py314-api26-p4a5c192d7b7308-r1`) tetap utuh
- CI tidak bertambah dari 120 menit
- kontributor tetap cukup "bisa Python + Kotlin", bukan "+ Rust + JNI + NDK"

> **Kesimpulan Rust: ditolak, dan sekarang alasannya lebih kuat dari sebelumnya.**
> Adopsi Kotlin justru *menghapus* satu-satunya alasan sisa untuk memakai Rust.

---

## 4. Kotlin: YA, tapi hanya lapisan UI

### Yang didapat
- Hilangnya mesin WebView: loopback HTTP server, CSP header, token di HTML, `xterm` vendor bundle
- Rendering native — penting untuk target Android Go (Infinix Smart 9 HD, ARMv7)
- Input keyboard yang benar (WebView + soft keyboard di Android itu terkenal bermasalah)
- Jalan menuju foreground Service supaya sesi tidak mati saat app di-background
  (masalah yang disorot `REFERENCE_MINING.md` T2)

### Yang HARUS dipertahankan (garis merah)
- `realpty.py`, `linuxenv.py`, `zpip.py`, PRoot/Alpine — **tidak disentuh**
- Pipeline Buildozer — **tidak dihapus**
- `zpip` + `zmux-info` + embedded runtime — diferensiator utama, tetap Python
- Lisensi AGPL-3.0; Termux dipakai sebagai **artefak Maven Apache-2.0**, bukan salinan kode GPLv3

---

## 5. Masalah nyata: token lintas-APK

Ini temuan penting dari membaca `security.py`.

Token ditulis ke `APP_DIR/.zmux_auth_token` dengan mode 0600. Storage privat Android itu
**per-UID**. Jadi APK Kotlin yang terpisah **secara fundamental tidak bisa** membaca token
milik APK Python. Itu sandbox bekerja sebagaimana mestinya, bukan bug.

| Skenario | Token | Status |
|---|---|---|
| Kotlin di dalam APK ZMUX (via Chaquopy) | `filesDir` sama → otomatis | ✅ target akhir |
| Dua APK terpisah (PoC ini) | harus disuplai manual | ⚠️ hanya untuk dev |

Cara ambil token saat dev (build debuggable):
```bash
adb shell run-as com.zabawheels.zmux cat files/.zmux_auth_token
```

> **Ini argumen terkuat untuk Chaquopy**, dan gw catat karena baru kelihatan setelah baca
> `security.py`. Chaquopy tidak cuma "menghapus layer WebSocket" — dia menghapus seluruh
> masalah distribusi token, karena UI dan engine jadi satu UID.

---

## 6. Rekomendasi bertingkat

| Level | Isi | Risiko | Rekomendasi |
|---|---|---|---|
| **A** | Perbaiki UI WebView saja | Rendah | Tidak menyelesaikan masalah keyboard/Go |
| **B** | **Kotlin + `terminal-view`, backend Python via WebSocket** | Sedang | ✅ **jalur ini** |
| **C** | Kotlin + Chaquopy (WebSocket dihapus) | Sedang-tinggi | ✅ tujuan akhir, setelah B terbukti |
| **D** | Kotlin + engine Rust (jalur Rin) | Tinggi | ❌ ditolak — §3 |

**PoC sesi ini = Level B.**

---

## 7. Status PoC

Ada di `experimental/zmux-kotlin/`. Lihat README-nya untuk cara build & jalan.

| Komponen | Status |
|---|---|
| `ZmuxProtocol.kt` — kontrak asli + rujukan baris Python | ✅ |
| `WebSocketPtyBridge.kt` — `?token=`, `action`, 401 tanpa retry | ✅ |
| `ZmuxTerminalSession.kt` — `TerminalSession` **tanpa `fork()` lokal** | ✅ |
| `ZmuxKeys.kt` — 21 tombol port 1:1 dari `KEY_ROWS` terminal.html | ✅ |
| Tab strip multi-session dari frame `{"type":"sessions"}` | ✅ |
| `ZmuxBackendLocator.kt` — cari token + probe port | ✅ |
| `tools/mock_pty_ws_server.py` — meniru protokol asli termasuk 401 | ✅ |
| `tools/protocol_check.py` — **9/9 gate lulus** | ✅ |
| Kompilasi Kotlin | ❌ tidak ada JDK/Android SDK di sandbox |

### Hasil verifikasi protokol

```
[PASS] auth1-bad-token-401: status=401
[PASS] auth2-good-token-101: status=101
[PASS] replay1-reset-screen: RESET_TERMINAL_SCREEN received
[PASS] replay2-sessions-frame: {"type":"sessions","sessions":[{"id":"1","busy":false}],"active":"1","max":8}
[PASS] replay3-sessions-shape: max=8
[PASS] resize1-stty-size: b'stty size\r\n11 47\r\n'
[PASS] input1-shell-executes: b'echo ZMUX_$((6*7))\r\nZMUX_42\r\n'
[PASS] sigint1-ctrl-c: 0x03 interrupted foreground sleep
[PASS] session1-list-reply: 1 new text frame(s)

9/9 gates passed
```

Gate `resize1` dan `sigint1` itu yang paling berarti: keduanya membuktikan byte dari klien
benar-benar sampai ke **kernel line discipline**, bukan cuma di-echo balik.

---

## 8. Langkah berikutnya

1. **Import core ZABAWHEELS ke repo ZMUX ini** — prasyarat untuk semua hal lain.
2. **Sync Gradle** dan perbaiki signature `TerminalViewClient` / `TerminalSessionClient` yang
   bergeser antar tag Termux.
3. **Uji lawan `ws_server.py` asli**, bukan mock. Jalankan `protocol_check.py` melawan backend
   sungguhan — kalau 9/9 lulus di sana juga, bridge-nya benar.
4. **Verifikasi `/dev/ptmx` di Infinix Smart 9 HD.** `RUST_KOTLIN_ANALYSIS.md` menyebut ini
   sebagai gerbang penentu, dan `realpty.py` sudah menyediakan `zmux-pty-probe`. Ini tetap
   risiko terbuka untuk Android Go.
5. **Baru setelah itu**: Chaquopy (Level C), yang sekaligus menyelesaikan masalah token di §5.

---

## 9. Ringkasan satu kalimat

> **Kotlin: ya, untuk UI saja — karena WebView memang titik lemah nyata di Android Go.
> Rust: tidak — karena mengadopsi Kotlin justru memberi kita `terminal-emulator` Apache-2.0
> secara gratis, yang menghapus satu-satunya pekerjaan yang tadinya mau dikerjakan Rust.**
