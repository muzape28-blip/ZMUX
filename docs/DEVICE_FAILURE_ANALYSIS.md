# Analisis Kegagalan On-Device ZMUX (2026-07-31)

> **Konteks:** Laporan pengujian perangkat nyata pertama (Infinix Smart 9 HD,
> ARMv7 / `lib/arm` — `armeabi-v7a`). Semua pengujian sebelumnya berjalan di
> x86_64 Linux (lihat peringatan di `docs/POST_FIX_REPORT.md`), jadi tiga
> kegagalan berikut adalah data on-device pertama yang menyentuh path proot,
> storage, dan zpip.
>
> Metode: deep research ke sumber primer (AOSP bionic linker, NDK docs,
> python-for-android pinned commit, pyjnius 1.7.0, termux-packages, komunitas
> yang meng-embed proot di APK) sebelum menulis fix. Setiap klaim punya
> referensi.

---

## Ringkasan

| # | Perintah | Error | Akar masalah |
|---|----------|-------|--------------|
| 1 | `linux apk add git openssh-client` | `CANNOT LINK EXECUTABLE ".../lib/arm/libproot.so": library "libtalloc.so.2" not found` | Nama file talloc di APK (`libtalloc.so`) ≠ nama yang diminta linker (`libtalloc.so.2`, SONAME talloc) |
| 2 | `zmux-setup-storage` | `ClassNotFoundException: org.kivy.android.PythonActivity` di `DexPathList[[directory "."]]` | `autoclass()` pyjnius dipanggil dari worker thread → JNI `FindClass` jatuh ke system class loader |
| 3 | `zpip install nano` sukses, `nano` gagal | `nano` bukan executable; nama jatuh ke Python → error | PyPI `nano` adalah library Django, BUKAN editor GNU nano; dan GNU nano butuh TTY yang ZMUX tidak punya |

---

## 1. `linux apk add` → `libtalloc.so.2 not found`

### Gejala

```
- CANNOT LINK EXECUTABLE "/data/app/~~...==/com.zaba.zmux-...==/lib/arm/libproot.so":
  library "libtalloc.so.2" not found: needed by main executable
```

### Riset & akar masalah

1. **`libproot.so` punya `DT_NEEDED = "libtalloc.so.2"`.** PRoot di-link ke
   talloc (allocator-nya), dan build talloc Samba (`wscript` talloc 2.4.2)
   menghasilkan SONAME `libtalloc.so.2` — terlihat eksplisit: library compat
   ditulis `soname='libtalloc.so.1'`, berarti library utama adalah major-2
   ([sumber wscript](https://github.com/deepin-community/talloc/blob/2.4.2-1deepin1/wscript)).

2. **Linker Android mencocokkan `DT_NEEDED` dengan nama file PERSIS.** Bionic
   mencari file bernama `libtalloc.so.2` di `LD_LIBRARY_PATH` + direktori
   sistem; fallback `name + ".so"` hanya berlaku kalau namanya *sudah*
   berakhiran `.so`, jadi `libtalloc.so.2` tidak pernah dicocokkan dengan
   `libtalloc.so` ([NDK docs](https://android.googlesource.com/platform/bionic/+/master/android-changes-for-ndk-developers.md):
   "A DT_NEEDED entry should be the same as the needed library's SONAME").

3. **Yang dikemas di APK adalah `libtalloc.so`, bukan `libtalloc.so.2`.**
   `scripts/build_proot_android.py` menyalin `sysroot/lib/libtalloc.so` (symlink
   dev di host) ke `out/libtalloc.so` via `shutil.copy2` — isi file-nya benar,
   tapi **namanya** tidak sesuai SONAME. Ditambah lagi, Android Gradle
   jniLibs tidak andal menyimpan file yang namanya tidak berakhiran `.so`
   (kasus komunitas: [r/termux — "how to include libtalloc.so.2 in jnilibs"](https://www.reddit.com/r/termux/comments/1kjuxcw/how_to_include_libtallocso2_in_jnilibs_of_my_app/)).

4. **Bukan masalah `LD_LIBRARY_PATH`.** `python_shell._exec_linux()` sudah
   menggabungkan `linuxenv.proot_env()` yang men-set
   `LD_LIBRARY_PATH=nativeLibraryDir` (lewat fallback `/proc/self/maps`, karena
   pyjnius di worker thread gagal — lihat bagian 2). Error tetap muncul karena
   file dengan nama `libtalloc.so.2` memang tidak ada di direktori itu.
   Kasus identik yang tidak terselesaikan: [restic-android #202](https://github.com/lhns/restic-android/issues/202)
   (`libdata_proot.so` + `libtalloc.so.2`).

### Fix yang diterapkan

- **`scripts/build_proot_android.py`** — dua hal baru:
  1. `patch_talloc_names()`: menulis ulang string `libtalloc.so.2` →
     `libtalloc.so\0` **di dalam ELF** (`libproot.so` NEEDED + `libtalloc.so`
     SONAME). Panjang byte sama, semua offset ELF tetap valid — trik yang
     dipakai komunitas (sed patch) untuk meng-embed proot di APK.
  2. `verify_needed()`: setelah build, `llvm-readelf -d` membuktikan semua
     `DT_NEEDED` terpenuhi oleh file yang dikemas (+ library sistem Android).
     Kalau talloc suatu saat naik ke `libtalloc.so.3`, **build gagal lebih
     dulu** di CI, bukan di HP pengguna.
- **`app/zmux/linuxenv.py`** — self-heal runtime `_ensure_talloc_compat()`:
  kalau `libtalloc.so` ada di `nativeLibraryDir` tapi `libtalloc.so.2` tidak
  (APK lama), salin ke direktori runtime yang bisa ditulis dan tambahkan ke
  depan `LD_LIBRARY_PATH`. Ini menyembuhkan APK lama tanpa rebuild, dan jadi
  sabuk-pengaman kalau packaging jniLibs menjatuhkan file versi-SONAME.

---

## 2. `zmux-setup-storage` → `ClassNotFoundException: org.kivy.android.PythonActivity`

### Gejala

```
[session error] JVM exception occurred: java.lang.ClassNotFoundException:
Didn't find class "org.kivy.android.PythonActivity" on path:
DexPathList[[directory "."],nativeLibraryDirectories=[/system/lib, ...]]
```

### Riset & akar masalah

1. **`DexPathList[[directory "."]]` = system class loader, bukan class loader
   aplikasi.** Kelas `org.kivy.android.PythonActivity` ADA di APK (app-nya
   jalan — webview bootstrap memakai kelas itu sebagai activity utama;
   [PythonActivity.java webview bootstrap](https://github.com/kivy/python-for-android/blob/5c192d7b7308487c2d3e3fcae63deba3131e7cb2/pythonforandroid/bootstraps/webview/build/src/main/java/org/kivy/android/PythonActivity.java)).
   Yang gagal adalah *penemuan* kelas itu dari thread tertentu.

2. **JNI `FindClass` di thread tanpa Java frame aplikasi memakai system class
   loader.** Kutipan langsung [Android perf-jni FAQ](https://developer.android.com/training/articles/perf-jni#faq_FindClass)
   (juga dikutip di [p4a issue #2533](https://github.com/kivy/python-for-android/issues/2533)):

   > "If you call FindClass from this thread, the JavaVM will start in the
   > 'system' class loader instead of the one associated with your application,
   > so attempts to find app-specific classes will fail."

   ZMUX menjalankan semua perintah di **worker thread**
   (`pty_session._exec_loop` — thread Python = pthread yang di-attach ke JVM
   tanpa Java frame aplikasi). `storage.request_permissions()` memanggil
   modul p4a `android.permissions`, yang melakukan
   `autoclass('org.kivy.android.PythonActivity')` langsung dari thread itu
   ([permissions.py p4a pinned](https://github.com/kivy/python-for-android/blob/5c192d7b7308487c2d3e3fcae63deba3131e7cb2/pythonforandroid/recipes/android/src/android/permissions.py))
   → ClassNotFoundException. Error yang sama persis dilaporkan untuk
   webview bootstrap di p4a #2533 dan [Stack Overflow](https://stackoverflow.com/questions/79215732/).

3. **Kenapa `linux-setup` tidak ikut gagal?** `linuxenv.native_library_dir()`
   membungkus `autoclass` dengan `try/except` dan jatuh ke pemindaian
   `/proc/self/maps` — jadi kegagalan Java-nya tertelan diam-diam.

4. **Mengapa "resolve lebih awal" menyembuhkan:** pyjnius menyimpan kelas yang
   sudah di-resolve di registry `MetaJavaClass` ([reflect.py pyjnius 1.7.0](https://github.com/kivy/pyjnius/blob/1.7.0/jnius/reflect.py)) —
   `autoclass()` berikutnya mengembalikan wrapper yang di-cache tanpa
   menyentuh `FindClass`. Webview bootstrap menjalankan Python di
   `PythonThread`; di thread utama itu ada Java frame aplikasi
   (`PythonMain.run`), jadi `FindClass` berhasil di sana.

### Fix yang diterapkan

- **`app/zmux/javabridge.py` (baru)** — `prime()` dipanggil sekali di
  `app/main.py` (thread utama Python, sebelum server/thread lain hidup),
  me-resolve `org.kivy.android.PythonActivity` dan meng-cache-nya. Akses
  berikutnya dari thread mana pun aman.
- **`app/zmux/storage.py`** — tidak lagi memakai modul p4a
  `android.permissions` (yang punya `autoclass` di import-time dan proxy
  `PythonJavaClass` — dua-duanya butuh `FindClass`). Sekarang memanggil
  `mActivity.requestPermissions([...])` langsung lewat bridge yang sudah
  di-prime; kalau bridge tidak tersedia, pesan error yang jujur (bukan
  traceback JVM).
- **`app/zmux/linuxenv.py`** — `native_library_dir()` mencoba bridge yang
  di-prime lebih dulu, baru pyjnius, baru `/proc/self/maps`.

---

## 3. `zpip install nano` sukses, `nano` gagal

### Riset & akar masalah

1. **`nano` di PyPI bukan editor.** Metadata PyPI
   ([pypi.org/pypi/nano/json](https://pypi.org/pypi/nano/json)):
   `"summary": "Does less! Loosely coupled mini-apps for django"` —
   library Django, tanpa console-script bernama `nano`. zpip dengan benar
   meng-install-nya (wheel universal lolos smoke test), tapi tidak ada
   executable `nano` yang muncul di PATH.
2. **Jadi `nano` jatuh ke Python.** `_is_external_command("nano")` → tidak
   ditemukan → dievaluasi sebagai Python → error membingungkan
   (`NameError`/`command not found`).
3. **Bahkan GNU nano yang asli tidak bisa jalan.** ZMUX adalah virtual
   terminal tanpa PTY (`/dev/ptmx`, `openpty` tidak dipakai) — sudah
   didokumentasikan di README: vim/htop/nano/less butuh TTY sejati. Jadi
   tidak ada "fix" yang membuat nano interaktif jalan; yang benar adalah
   edukasi + petunjuk pengganti.

### Fix yang diterapkan

- **`app/zmux/zpip.py`** — `PYPI_TOOL_COLLISIONS` + `_tool_collision_warning()`:
  `zpip install nano` tetap sukses (paketnya nyata) tapi mencetak WARNING
  jelas: "PyPI's 'nano' adalah library Django, BUKAN editor GNU nano".
- **`app/zmux/python_shell.py` + `pty_session.py`** — daftar
  `KNOWN_TUI_COMMANDS` (nano, vim, vi, emacs, htop, top, less, more, micro,
  joe, mcedit, ranger, screen, tmux): kalau nama itu diketik dan tidak ada
  executable-nya, terminal menjawab jujur "butuh TTY sungguhan, ZMUX tidak
  punya PTY" + alternatif (Python `open(...).write(...)`, `cat > file`)
  — bukan `NameError` yang membingungkan.

---

## File yang diubah

| File | Perubahan |
|------|-----------|
| `scripts/build_proot_android.py` | Patch NEEDED/SONAME talloc + verifikasi `DT_NEEDED` (build gagal lebih dulu kalau ada drift) |
| `app/zmux/javabridge.py` | Baru — bridge Java yang di-prime di main thread, aman dipakai thread mana pun |
| `app/main.py` | `javabridge.prime()` saat startup |
| `app/zmux/linuxenv.py` | Pakai bridge dulu; self-heal `libtalloc.so.2` untuk APK lama |
| `app/zmux/storage.py` | Request permission via `mActivity` langsung, tanpa modul p4a yang rawan `FindClass` |
| `app/zmux/python_shell.py`, `pty_session.py` | Hint jujur untuk TUI tanpa PTY |
| `app/zmux/zpip.py` | WARNING kolisi nama PyPI (nano) |
| `app/tests/*` | +35 test baru (javabridge, self-heal talloc dua arah, storage bridge, TUI hint, warning zpip, build marker) |
| `.github/workflows/build-zmux-apk.yml` | Cache key ikut script proot; step "Verify APK contents"; marker build |
| `app/zmux/buildinfo.py` | Baru — build marker (SHA + run id) untuk identifikasi APK di perangkat |

Status test: **319 passed, 25 skipped** (skip = integrasi proot/rootfs yang
butuh harness khusus).

---

## Update 2 — "APK hasil fix masih error sama" ternyata APK basi (2026-08-01)

Setelah build ulang via PR, user melaporkan `gates` **tetap** menunjukkan
`libtalloc.so.2 not found`. Investigasi menemukan bukti kuat bahwa APK yang
di-install **bukan** hasil build dari kode repo ini:

1. **Nama gate berbeda.** Output perangkat user: `[PASS] ptx: …`. Kode di
   seluruh riwayat repo ini (termasuk semua branch) menamai gate itu
   `ptmx`. `git log -S 'ptx'` kosong. Artinya binary di perangkat dibuat
   dari sumber yang tidak ada di repo ini.
2. **Analisis pipeline (buildozer 1.6.0 + p4a pinned) — teori "jniLibs"
   user sudah benar secara mekanisme:**
   - `buildozer/targets/android.py` `build_package()` menyalin
     `android.add_libs_<abi>` ke `dist/libs/<abi>/` **setiap build** lewat
     `buildops.file_copy` = `copyfile()` yang **selalu menimpa** ([sumber](https://github.com/kivy/buildozer/blob/1.6.0/buildozer/buildops.py)).
     Jadi `libproot.so`/`libtalloc.so` memang masuk struktur jniLibs APK.
   - Yang gagal bukan penempatan file, tapi **nama yang diminta linker**
     (`libtalloc.so.2` = SONAME talloc) vs nama file yang dikemas.
   - Analisis lanjutan (buildozer `buildops.file_copy` = `copyfile()` yang
     menimpa) menunjukkan dist basi **bukan** vektor utama: setiap
     `buildozer android debug` menyalin ulang `add_libs` ke dist, jadi
     libs di APK selalu yang terbaru dari `app/libs/`. Cacat sesungguhnya
     ada di sisi *binary* (NEEDED name) dan di *APK mana yang di-install*.

**Fix yang diterapkan (semua di dalam kode, bisa di-push):**

| Perubahan | Efek |
|---|---|
| `gates` G2 + `zmux-info` kini **membaca `DT_NEEDED` libproot.so di perangkat** (`app/zmux/elfscan.py`, ELF parser murni Python, ELF32/64 LE/BE) | Bukti langsung di HP: kalau NEEDED masih `libtalloc.so.2` → pesan `STALE BINARY`; `zmux-info` menampilkan `Proot NEEDED: libtalloc.so [STALE — reinstall]` |
| Self-heal `_ensure_talloc_compat` kini dua arah | APK yang mengemas `libtalloc.so.2` saja (atau `libtalloc.so` saja) tetap bisa jalan dengan runtime baru |
| `toolchain/runtime-lock.json` + kontrak proot (`packaged_needed: libtalloc.so`) | Mencatat kontrak; ikut mengubah cache-key CI |
| Patch workflow (cache key + "Verify APK contents" + marker build) disimpan di `docs/WORKFLOW_VERIFY_STEPS.md` | **Belum bisa di-push** — GitHub App sandbox tidak punya permission `workflows`. Terapkan manual via `git apply` setelah permission diberikan (lihat file tsb) |
| `app/zmux/buildinfo.py` + baris `Build:` di `zmux-info` | Menampilkan SHA commit ketika marker ada di APK |

**Cara verifikasi di perangkat (setelah install APK baru):**

```bash
zmux-info          # baris "Proot NEEDED:" harus "libtalloc.so" (tanpa STALE)
gates              # G2 harus PASS; kalau FAIL baca detailnya
```

`zmux-info` menampilkan `Proot NEEDED: libtalloc.so [STALE — reinstall]`
kalau binary-nya masih lama — itulah bukti definitif di perangkat, tanpa
perlu tahu SHA. Uninstall total (`Settings > Apps > ZMUX > Uninstall`) lalu
install APK dari PR terbaru sebelum menjalankan verifikasi, karena update
dengan data lama bisa menyimpan sisa build lama.

## Update 5 — UX pass: `cd` home, soft keyboard, wrapping, scroll (2026-08-01)

Setelah gates 5/5, user melaporkan empat masalah UX:

1. **`cd` (tanpa argumen) gagal: "outside home directory".** Akar: Android
   mengekspos app storage sebagai `/data/user/0/...` yang merupakan symlink
   ke `/data/data/...`. `_cmd_cd` membandingkan `HOME_DIR` mentah dengan
   `HOME_DIR.resolve()` → selalu "di luar home", padahal `cd <subdir>` jalan
   (karena `_path()` me-resolve). Fix: resolve kedua sisi sebelum cek.
2. **Soft keyboard menutupi banner/prompt.** WebView tidak me-resize saat
   IME terbuka (perilaku adjustPan). Fix: pantau `visualViewport`; saat IME
   buka/tutup, layout di-resize ke area terlihat + terminal di-fit ulang →
   prompt selalu di atas keyboard, topbar tetap terlihat.
3. **Batas layar ambigu; setelah `clear` prompt terlihat bergeser; wrapping
   `help`/`cat README.md` berantakan.** Akar: `fitTerminal()` mengukur font
   sebelum webfont selesai dimuat (fallback metrik) → cols terlalu besar →
   teks melewati tepi kanan. Fix: clamp `.xterm-screen` ke 100% lebar,
   re-fit saat `document.fonts.ready`, dan refresh canvas setelah `clear`.
4. **Scroll tersendat & kurang agresif.** Akar: `scrollToBottom()` dipanggil
   per-chunk output (reflow terus-menerus di HP kelas bawah); scrollback
   cuma 2000 baris. Fix: coalesce per animation frame, momentum touch,
   scrollback 6000.

Semua diperbaiki di frontend (`app/templates/terminal.html`) + `cd` di
`app/zmux/python_shell.py`. UI harness 44/44, Python 367 passed.

## Update 4 — gates 5/5 PASS + `git clone` yang terlihat "stuck" (2026-08-01)

**Hasil perangkat setelah fix panjang-byte:** `gates` = **5/5 PASS**
(proot-exec OK, Alpine boots, git clone shallow OK, apk runs), dan
`linux apk add git openssh-client` menginstal 19 paket. `zmux-info`
menunjukkan `Proot NEEDED: libtalloc.so, libdl.so, libc.so` — masalah
`libtalloc.so.2`/`DT_HASH` **selesai**.

**Masalah baru: `git clone` (full, tanpa `--depth`) terlihat stuck.**
Analisis + reproduksi lokal (proot host x86_64 + git): full clone selesai
dalam ~1 detik, jadi bukan clone-nya yang rusak. Penyebab UX:

1. **git menulis SEMUA progress ke stderr**, dan executor ZMUX hanya
   me-stream stdout → di ARMv7+proot yang lambat, layar diam sampai
   selesai → terlihat hang.
2. **Hang nyata juga mungkin:** `_read_stdout_streaming` menunggu EOF
   pipe stdout; kalau ada grandchild (helper git) yang mewarisi pipe,
   EOF tidak pernah datang → `reader.join(None)` = hang selamanya.
   (Diverifikasi: stack dump menunjukkan `handle.close()` deadlock dengan
   pump yang sedang `readline`.)

**Fix:** stderr kini di-stream live (progress git/apk/curl terlihat);
executor menunggu *proses* selesai (bukan EOF pipe), lalu memberi pump
waktu 1 detik untuk flush dan kembali — child yang bandel tidak bisa
menggantung sesi. Hint "process terminated by signal" ikut di-stream.

**Tips di perangkat:** gunakan `git clone --depth 1 <url>` untuk kecepatan
(shallow clone terbukti jalan di `gates`); full clone akan tetap terlihat
progresnya sekarang.

## Update 3 — akar masalah sesungguhnya: patch 1-byte-short merusak ELF (2026-08-01)

Test lanjutan user (APK build terbaru) menunjukkan **`libtalloc.so.2 not
found` sudah hilang** (artinya NEEDED berhasil diubah menjadi
`libtalloc.so`), tapi muncul error baru:

```
CANNOT LINK EXECUTABLE ".../libproot.so": empty/missing DT_HASH/DT_GNU_HASH
WARNING: linker: ... unused DT entry: unknown (type 0xab000 arg 0x1000005)
```

**Akar masalah: patch string-nya salah panjang.** `patch_talloc_names()`
mengganti `b"libtalloc.so.2"` (14 byte) dengan `b"libtalloc.so\x00"` —
tapi **"libtalloc.so" hanya 12 karakter**, jadi replacement-nya 13 byte.
File menyusut 1 byte, dan **seluruh section header / program header /
string setelah titik itu bergeser 1 byte** → `.dynamic` terbaca miring
(warning `unused DT entry` dengan tag sampah) dan linker tidak menemukan
hash table (`empty/missing DT_HASH/DT_GNU_HASH`).

**Reproduksi lokal (build talloc 2.4.2 + proot 4dba3af dari sumber, gcc
host):**

```
size before: 235872 | after: 235871   <- 1 byte hilang!
readelf: Error: Reading 1856 bytes extends past end of file for section headers
```

Setelah diganti dengan `b"libtalloc.so\x00\x00"` (14 byte, panjang sama):

```
size before: 235872 | after: 235872   <- tidak berubah
NEEDED: [libtalloc.so]  GNU_HASH: utuh  readelf: bersih
```

**Fix yang diterapkan:**

| Perubahan | Efek |
|---|---|
| Replacement → `b"libtalloc.so\x00\x00"` (14 byte) | Panjang identik → tidak ada pergeseran; diverifikasi lokal + readelf bersih |
| `patch_talloc_names()` gagal keras kalau ukuran file berubah | Bug kelas ini tidak akan pernah lolos lagi ke APK |
| `verify_needed()` mengharuskan `libproot.so` parse bersih + binding talloc persis `libtalloc.so` | CI menolak artifact yang korup |
| `zmux-info` & `gates` menampilkan "Proot status: ... corrupted build" kalau binary tidak bisa dibaca | APK korup langsung terlihat di HP, tidak diam-diam |

**Mengapa APK lama tetap gagal padahal sudah "build ulang":** user
meng-install di atas data lama → kode Python dari `private.tar` lama yang
terbawa (bukti: `zmux-info` tidak menampilkan baris `Proot NEEDED:`).
Sekarang baris itu selalu muncul (atau menampilkan pesan corrupted).

**Langkah verifikasi final:**

1. **Uninstall total** ZMUX (Settings → Apps → ZMUX → Uninstall).
2. Install APK dari build CI terbaru (commit dengan fix ini).
3. `zmux-info` → harus ada baris `Proot NEEDED: libtalloc.so` (tanpa STALE,
   tanpa status corrupted).
4. `gates` → G2/G3/G5 PASS.

## Yang perlu dilakukan user (setelah APK baru)

1. **Uninstall ZMUX lama dulu** (data lama bisa membingungkan), lalu install
   APK hasil build baru yang sudah lolos step "Verify APK contents".
2. Verifikasi:
   ```bash
   zmux-info                # baris Build: menampilkan SHA commit
   gates                    # G2 proot-exec harus PASS sekarang
   linux apk add git openssh-client
   zmux-setup-storage       # tidak boleh ClassNotFoundException lagi
   ```
3. Untuk nano: jangan `zpip install nano` (itu library Django) —
   gunakan Python untuk edit file, atau `cat > file`. TUI tidak akan pernah
   interaktif di ZMUX (tanpa PTY).

## Catatan verifikasi yang belum selesai

- Self-heal `libtalloc.so.2` (jalur APK lama) baru teruji unit-level di
  desktop; butuh konfirmasi di perangkat.
- `gates` G1 (ptmx) belum lolos → PTY engine masih di luar scope.
- Permission dialog di Android 10+ adalah no-op (manifest hanya deklarasi
  `maxSdkVersion=28`); `~/storage/app` (app-external) tetap bisa di-link
  tanpa permission.
