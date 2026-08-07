# ZMUX vs Termux+proot-distro — Cross-Check Performa `apk add fastfetch`

Tanggal: 2026-08-07
Status: **analisis + diskusi — belum ada fix yang diterapkan** (sesuai permintaan).

> **UPDATE — perbandingan dengan ZABAWHEELS (versi webview) sudah ditambahkan
> di §3e dan mengonfirmasi temuan utama secara langsung.**

---

## 1. Ringkasan eksekutif

Di sesi interaktif (PTY), ZMUX *sudah* memakai arsitektur yang sama persis
dengan Termux + proot-distro: sebuah PTY asli yang menjalankan
`proot → /bin/sh -l` di dalam rootfs Alpine. Jadi `apk add fastfetch` di kedua
app **tidak melewati Python** — byte langsung mengalir ke PTY dan `apk`
dieksekusi oleh shell guest. Perbedaan performa **tidak berasal dari lapisan
PTY atau dispatcher command**, tapi dari **konfigurasi PRoot yang dibuat ZMUX**:

> **Temuan utama:** ZMUX memaksa `PROOT_NO_SECCOMP=1` di
> `linuxenv.proot_env()` (`linuxenv.py:566–569`). Ini mematikan akselerator
> **seccomp** PRoot dan memaksa **pure-ptrace mode**, yang secara dramatis
> jauh lebih lambat untuk workload padat syscall seperti `apk` (fork/exec +
> ekstraksi ribuan file). proot-distro **tidak** menonaktifkan seccomp secara
> default — var `PROOT_NO_SECCOMP` hanya diteruskan bila *user sudah
> mengekspornya sendiri*.

Faktor sekunder yang memperparah (tapi bukan penyebab utama) adalah **state
`apk` index yang masih dingin** di ZMUX (rootfs baru belum pernah `apk update`,
sehingga `apk add` pertama harus unduh `APKINDEX` main+community dari jaringan),
sementara di Termux kemungkinan sudah hangat.

---

## 2. Arsitektur eksekusi — bagaimana ZMUX menjalankan perintah

### 2a. Jalur interaktif (yang dipakai user) — ZMUX

`pty_session.py::_enter_linux_pty` meluncurkan `RealPtyProcess` dengan:

```python
argv = linuxenv.build_interactive_argv(self.shell.cwd)   # proot -> /bin/sh -l
env  = linuxenv.interactive_env()
```

`build_interactive_argv` (`linuxenv.py:811`) menghasilkan:

```python
argv = build_proot_argv(["/bin/sh", "-l"], host_cwd)     # [proot, -0, -r <rootfs>, -b /dev -b /proc -b /sys -b HOME:/root, -w cwd, /bin/sh, -l]
argv.insert(1, "--kill-on-exit")
argv.insert(2, "--link2symlink")
argv.insert(3, "--sysvipc")
```

Setelah shell hidup, `write_input()` mengirim setiap byte **mentah ke master
PTY** (`pty_session.py`) — tidak ada parsing command, tidak ada dispatcher
Python, tidak ada wrapper `zmux.cli`. Ini **identik** dengan cara Termux menjalankan
proot-distro. Jadi titik paling kiri rantai eksekusi tidak menambah overhead.

### 2b. Jalur non-interaktif / legacy — bukan yang dipakai user

Ada executor lama `python_shell.py` yang memang lewat Python (dan membungkus
`apk` menjadi `linux apk add ...`), tapi itu **bukan** shell produk. User
memakai shell PTY Alpine. Tidak relevan untuk kasus ini.

---

## 3. Perbandingan konfigurasi PRoot (sumber: source yang sudah di-clone)

### 3a. Faktor DOMINAN — seccomp accelerator

| | ZMUX | Termux proot-distro |
|---|---|---|
| `PROOT_NO_SECCOMP` | **di-set `"1"` selalu** (`linuxenv.py:569`) | **tidak di-set** — hanya diteruskan bila user sudah mengekspor sendiri (`commands/login/__init__.py`) |
| Mode tracing | **pure ptrace** (lambat) | **seccomp-accelerated** (cepat) |

Konfirmasi dari sumber:

- `termux/prooot` @ v5.1.107.89 (binary yang dipakai ZMUX) memiliki seccomp
  enabled-by-default (`src/ptrace/ptrace.c` `tracee->seccomp == ENABLED`), dan
  `PROOT_NO_SECCOMP` mematikannya.
- proot-distro `commands/login/__init__.py`:
  ```python
  if not minimal:
      for var in ("PROOT_NO_SECCOMP", "PROOT_VERBOSE"):
          val = os.environ.get(var)
          if val:
              child_env[var] = val
  ```
  → pada kondisi normal, `PROOT_NO_SECCOMP` **kosong**, jadi seccomp aktif.
- Referensi eksternal (Singularity issue #934): *"We should not set
  PROOT_NO_SECCOMP by default, as seccomp support provides **significant
  performance improvements** for proot."*

**Kenapa ini membuat `apk add` 20s vs <1s:** `apk add` = banyak fork/exec
(child processes untuk gunzip, ar, script trigger) + pembuatan ribuan file
(masing-masing = banyak syscall). Di pure-ptrace, PRoot harus berhenti dan
resume di (hampir) setiap syscall setiap tracee. Akselerator seccomp
meng-intercept syscall di kernel via filter seccomp tanpa full ptrace-stop.
Efeknya bisa **10–30× lebih lambat** — sangat cocok dengan gap 20s vs <1s.

### 3b. Faktor yang TIDAK berbeda (bukan penyebab)

- **DNS**: ZMUX `_ensure_guest_resolv_conf` (`linuxenv.py:671`) menulis
  `8.8.8.8` + `1.1.1.1`. proot-distro `helpers/rootfs.py` juga menulis
  `8.8.8.8` + `8.8.4.4`. Keduanya pakai Google public DNS → setara. Bukan
  pembeda.
- **Binary PRoot**: ZMUX memakai `termux/prooot v5.1.107.89` (dibangun ulang
  dari commit yang sama) — proyek yang sama dengan yang dipasang proot-distro.
  Bukan pembeda.
- **Flag dasar**: `-0`/`--change-id=0:0` (fake root), `--kill-on-exit`,
  `--link2symlink`, `--sysvipc`, bind `/dev /proc /sys` — semua ada di kedua
  sisi. ZMUX malah meng-bind lebih sedikit host dir, jadi sisi ini jika ada
  justru lebih ringan.
- **PTY**: kedua app pakai PTY asli.

### 3c. Faktor sekunder — state `apk` index (dingin vs hangat)

`linuxenv._bootstrap` (`linuxenv.py:1203`) hanya menulis `/etc/apk/repositories`
(main+community) dan **tidak menjalankan `apk update`** saat install rootfs.
Konsekuensi: `apk add fastfetch` pertama di ZMUX otomatis mengunduh
`APKINDEX.tar.gz` main + community dari jaringan (beberapa MB). proot-distro
juga tidak pre-update saat install, **tapi** kalau di Termux user sudah pernah
`apk update` / sudah install sebelumnya, index + fastfetch sudah hangat →
`apk add fastfetch` tinggal unduh paket kecil → <1s.

Perbandingan 20s vs <1s kemungkinan **tidak apples-to-apples** pada dimensi ini,
tapi faktor seccomp tetap yang paling signifikan untuk paket syscall-heavy.

### 3d. Perbedaan kecil yang tercatat (bukan performa)

proot-distro login juga menambahkan `--kernel-release=...` (fake kernel version)
dan `-L` (lstat fix untuk dpkg). ZMUX tidak menambahkan keduanya di
`build_interactive_argv`. Tidak memengaruhi kecepatan `apk`, hanya catatan
paritas fitur.

### 3e. ⭐ Perbandingan langsung dgn ZABAWHEELS (webview) — bukti kunci

Repo `muzape28-blip/ZABAWHEELS` (versi webview, sebelum lahirnya ZMUX sekarang)
di-clone dan dibandingkan file-per-file pada jalur eksekusi `apk`:

| Aspek | ZABAWHEELS (webview) | ZMUX sekarang (Kotlin) | Dampak |
|---|---|---|---|
| `PROOT_NO_SECCOMP` di `proot_env()` | **TIDAK ada sama sekali** (seccomp aktif → cepat) | **`"1"` di-hardcode** (`linuxenv.py:569`) → pure-ptrace → lambat | **ini bedanya** |
| `build_interactive_argv` | `--kill-on-exit --link2symlink --sysvipc /bin/sh -l` | **identik** | tidak berbeda |
| Wiring PTY | `_enter_linux_pty` → `RealPtyProcess` | **identik** | tidak berbeda |
| DNS resolv.conf | `8.8.8.8` + `1.1.1.1` | sama + `options timeout:2 attempts:2` | tidak berbeda |
| `_bootstrap` / `apk update` saat install | cuma tulis repositories, **tanpa `apk update`** | **identik** (juga tanpa `apk update`) | tidak berbeda |
| Pin binary proot | `4dba3af` | `a89b3732` (v5.1.107.89) | bukan penyebab performa (dua-duanya termux/prooot) |

**Kesimpulan:** ZABAWHEELS tidak pernah mengalami kasus lambat ini dan
**persis** yang membuatnya berbeda adalah tidak adanya `PROOT_NO_SECCOMP=1`.
Regresi performa diperkenalkan pada ZMUX sekarang ketika commit stability-fix
untuk SIGSYS Android 14/15 menambahkan `PROOT_NO_SECCOMP=1` ke `proot_env()`
(lihat log git `git log -S "PROOT_NO_SECCOMP"` → masuk lewat merge
`1edbd24`, cabang `arena/019fd816-zmux`). Semua komponen lain jalur `apk`
identik antara dua versi → konfirmasi tunggal atas temuan §3a.

---

## 4. Kenapa ZMUX menonaktifkan seccomp (konteks tradeoff)

`linuxenv.py:564-567`:
```
# Some Android kernels (esp. Android 14/15) deliver a fatal SIGSYS
# ("Bad system call") to proot's tracees when proot uses its seccomp
# accelerator. Disabling it keeps guests working; proot falls back to
# pure ptrace.
"PROOT_NO_SECCOMP": "1",
```

Jadi ini keputusan **stabilitas vs performa**: seccomp dapat membuat tracee
SIGSYS di kernel tertentu (kasus `proot-me/PRoot#106`, dan void-packages juga
men-disable seccomp untuk alasan serupa). ZMUX memilih "jalan lambat tapi aman".
proot-distro/Termux memilih seccomp aktif (dan menerima risiko/versi binary yang
lebih baru).

---

## 5. Kandidat solusi (belum diterapkan — untuk didiskusikan)

1. **Re-enable seccomp secara bertahap / probe-aware** (efek terbesar):
   - Jangan hardcode `PROOT_NO_SECCOMP=1`; jadikan default off, atau
   - lakukan *canary probe* (mis. `proot ... /bin/true`) sekali per boot: kalau
     sukses tanpa SIGSYS → biarkan seccomp aktif; kalau gagal → baru set
     `PROOT_NO_SECCOMP=1`. Ini persis pendekatan yang disarankan Singularity #934.
   - Upgrade binary `termux/prooot` ke versi yang memperbaiki SIGSYS pada kernel
     Android 14/15 (kompat dengan penanganan SIGSYS di `syscall/enter.c`) mungkin
     menghapus kebutuhan men-disable sama sekali.
2. **Panaskan `apk` index saat install** (`apk update` sekali setelah `_bootstrap`)
   → menghilangkan unduhan index di `apk add` pertama. Efek kecil tapi nyata.
3. **Catatan**: kalau target stabil di Android 14/15 lebih penting daripada
   kecepatan, opsi 1b (canary probe) memberi keduanya.

---

## 6. Kesimpulan

Penyebab paling mungkin gap **20s vs <1s** adalah
**`PROOT_NO_SECCOMP=1` yang di-hardcode di ZMUX** → PRoot jalan di pure-ptrace
(bukan seccomp) → `apk add` (padat fork/exec + syscall) jadi sangat lambat.
proot-distro default memakai seccomp. Faktor sekunder: index `apk` yang masih
dingin di ZMUX. Arsitektur PTY/shell-nya sendiri sudah setara dengan Termux.

Perbandingan dengan **ZABAWHEELS (webview)** mengonfirmasi hal ini secara
langsung: ZABAWHEELS **tidak pernah** men-set `PROOT_NO_SECCOMP`, seccomp aktif,
dan tidak pernah ada kasus lambat — seluruh komponen jalur `apk` lain identik
antara dua versi. Jadi regresi performa datang tepat dari ditambahkannya
`PROOT_NO_SECCOMP=1` pada ZMUX sekarang.

---

## 7. Status Implementasi (2026-08-07)

Ketiga perbaikan yang didiskusikan sudah diterapkan di cabang
`arena/019fdaf7-zmux` (semua tes Python lulus — 56/56):

### 7a. Performa — canary probe seccomp (paling berdampak)
- `linuxenv.py`: `proot_env()` tidak lagi hardcode `PROOT_NO_SECCOMP=1`.
  Dipecah jadi `_base_proot_env()` + probe `_seccomp_probe_broken()` yang
  menjalankan `proot -> /bin/true` sekali per proses (tanpa seccomp-toggle).
  Seccomp dibiarkan AKTIF (cepat) kalau probe bersih; `PROOT_NO_SECCOMP=1`
  (lambat tapi stabil) hanya dipakai kalau probe gagal/hang.
- `TerminalSessionHelper.java`: canary yang sama (`runSeccompProbe`), dijalankan
  **asinkron** lewat `warmSeccompProbe()` di thread background (pre-warm dari
  `detectInstalledLinux`) dan di-cache; `isSeccompBroken()` non-blocking membaca
  cache (default konservatif sampai probe selesai). Ini path yang dipakai shell
  user aslinya. Probe sengaja async karena canary bisa hang 10 detik — jika
  sinkron di UI thread (tempat session dibuat) itu akan ANR.

### 7b. Keyboard resize — debounce
- `ZmuxTerminalActivity.kt`: listener `addOnLayoutChangeListener` kini
  men-debounce `updateSize()` 100ms (hanya resize sekali setelah view settle),
  alih-alih resize tiap frame animasi IME. Menghilangkan reflow berulang yang
  mendorong prompt naik 4-5 baris.

### 7c. Prompt sederhana `Z$` (tanpa warna)
- Prompt Alpine → `Z$ ` polos; nama dir (basename) muncul hanya setelah `cd`
  via override `cd()` (menulis PS1 literal, jadi jalan tanpa ASH_EXPAND_PRMT).
  Byte-identik antara Python `_write_guest_prompt` dan Java
  `ensureGuestPrompt` (dua-duanya menulis `/etc/profile.d/zmux-prompt.sh`).
- Fallback env PS1 (Java + Python) dan prompt mksh fase setup juga `Z$ `.
- MOTD (banner sambutan satu kali) tetap berwarna — itu bukan prompt dan tidak
  memengaruhi kerapian baris; bisa disederhanakan terpisah kalau mau.

### Catatan jujur
- Fix 7b menangani akumulasi reflow; perilaku "layar membesar saat keyboard
  tutup" yang wajar tetap ada, tapi prompt kini konsisten di baris bawah.
- `PROOT_NO_SECCOMP` sekarang bisa kembali bernilai `1` di device yang kernel-nya
  memang membutuhkannya (Android 14/15) — jadi tidak ada regresi stabilitas,
  hanya tambah cepat di device yang seccomp-nya jalan.
- **Catatan re-review (2026-08-07):** versi awal canary probe Java berjalan
  **sinkron** (`waitFor(10s)`) di UI thread — ini rawan ANR di device yang
  seccomp-nya hang. Sudah diperbaiki menjadi **asinkron** (pre-warm di thread
  background + cache + fallback konservatif non-blocking). Probe Python hanya
  dipakai jalur WS/legacy (thread worker, bukan UI thread Android), jadi tidak
  menimbulkan ANR.
