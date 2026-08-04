# Post-Fix Report — answers to the four questions

**Date:** 2026-07-31
**Fixes applied:** 1–4 from the capability report, plus two found while testing.
**Tests:** 235 app + 32 infra = **267 passing** (was 222).
**Method:** every answer below was executed, not inferred.

> **Standing caveat:** all runs are on x86_64 Linux with CPython 3.11, not on
> an ARM phone with the p4a runtime. Logic transfers; anything touching
> Android's kernel, SELinux or the APK layout does not. This matters a lot for
> question 2 — see the warning there.

---

## What was fixed

| # | Bug | Before | After |
|---|---|---|---|
| 1 | `cd` ignored by Python | wrote to a different dir; `ls`/`cat` blind to it | `os.chdir()` — everything agrees |
| 2 | `&&`, `;`, `2>&1`, `&`, `$( )` | **silently ignored**, exit 0 | rejected, exit 2, explains why |
| 3 | module name guessed | `rich` uninstallable | read from the wheel |
| 4 | typo → Python traceback | `SyntaxError: invalid syntax` | `zmux: gti: command not found` (127) |
| 5 | *(found while testing)* installed pkg not importable | `zpip install X` then `import X` failed | on `sys.path` |
| 6 | *(found while testing)* no storage access | app-private island | `zmux-setup-storage` |

Verified:

```
$ /bin/true && /bin/touch built.txt
zmux: '&&' (conditional AND) is not supported — ZMUX has no shell language.
exit=2 · built.txt created? False        ← no longer lies

$ cd demo && (in python) open('note.txt','w')…
$ cat note.txt
written by python                        ← same filesystem now

$ gti status
zmux: gti: command not found             ← exit 127
```

Classifier accuracy on the full matrix: **0 misclassified** — `undefined_var + 1`,
`x = = 5`, `[a for a in bad]` still raise real Python errors; `git-foo --bar`,
`npmm install express`, `curlx https://…` all report *command not found*.

---

## 1. Apakah sudah layak?

**Layak untuk dipakai sendiri — belum layak dirilis ke publik.**

**Layak, karena:**
- Tidak ada lagi kegagalan senyap. Yang tiga paling berbahaya sudah jadi error
  jujur.
- Satu filesystem, bukan dua. Ini yang paling merusak kepercayaan sebelumnya.
- Pesan error sekarang berbahasa terminal, bukan traceback Python.
- 267 test hijau, stabil.

**Belum layak dirilis, karena satu alasan besar dan satu menengah:**

1. **Nol pengujian di perangkat.** Semua ini x86_64 Linux. Target Anda Infinix
   Smart 9 HD (Android Go, ARMv7). Yang belum terbukti di sana: multi-session,
   streaming, `zmux-setup-storage`, dan wrapper `BIN_DIR` (Android 10+ memblokir
   `exec()` di direktori app — lihat `docs/CAPABILITY_REPORT.md` §4).
2. **Tetap bukan shell.** `$VAR`, glob, `~`, dan job control masih tidak ada —
   sekarang gagal dengan jujur, tapi tetap tidak ada. Untuk itu jawabannya PTY
   (`docs/RUST_KOTLIN_ANALYSIS.md`), bukan menambal lagi.

**Rekomendasi:** pakai sendiri sekarang, uji di HP, jangan umumkan sebagai
pengganti Termux.

---

## 2. Bisa jalankan command? `git clone`, `curl`, install module, akses storage?

### Command ringan: ya, betulan

Diuji: `ls`, `cat`, `mkdir`, `cp`, `mv`, `grep`, `sed`, `find`, `ps`, `wc`,
`tr`, `head`, pipeline `|`, redirection `>` `>>` `<`. Semua proses OS asli
dengan exit code asli.

### `git clone` dan `curl`: berhasil di sini — **tapi baca peringatannya**

```
$ git clone --depth 1 https://github.com/pallets/itsdangerous repo
Cloning into 'repo'...                       exit=0
$ ls repo
CHANGES.rst  LICENSE.txt  README.md  docs  pyproject.toml  src  tests
$ git -C repo log --oneline -1
672971d Merge branch 'stable'                exit=0

$ curl -sS -o head.json -w '%{http_code}' https://pypi.org/pypi/six/json
200 · 40256 bytes                            exit=0
```

> ### ⚠️ Ini berhasil karena mesin uji saya Linux dan **punya** `git` dan `curl`.
>
> **Android tidak mengirim keduanya.** AOSP hanya menyediakan toybox di
> `/system/bin` — `ls`, `cat`, `grep`, `sed`, `ps`, dan sejenisnya. **Tidak ada
> `git`, tidak ada `curl`, tidak ada `gcc`, tidak ada binary `python`.**
>
> Di HP Anda, `git clone` akan menghasilkan:
> ```
> zmux: git: command not found
> ```
> Itu benar dan jujur (berkat Fix 4) — tapi tetap saja tidak ada `git`.

**ZMUX tidak bisa memasang binary sendiri.** Sejak targetSdk 29, Android
memblokir `exec()` di direktori app (pelanggaran W^X); binary harus berada di
`nativeLibraryDir`, yang hanya bisa diisi saat build. Termux mengatasinya
dengan mem-bootstrap seluruh userland Linux — persis yang secara sadar tidak
dilakukan ZMUX.

**Yang bisa dilakukan sebagai gantinya — dan ini bukan hiburan:**

```python
# pengganti curl, stdlib, tanpa binary apa pun
import urllib.request, json
d = json.loads(urllib.request.urlopen('https://pypi.org/pypi/six/json').read())
print(d['info']['version'])        # → 1.17.0   (diuji, berhasil)
```

Untuk `git`, padanan murni-Python adalah **dulwich** (pure-python git). Saat ini
belum ada di index — `zpip search dulwich` → *No packages found*. **Ini item
roadmap paling bernilai** untuk permintaan Anda: satu wheel universal
memberikan `clone`/`commit`/`push` tanpa binary sama sekali.

### Install module: berhasil (dan Fix 5 membuatnya benar-benar berguna)

```
$ zpip install typing-extensions
typing_ext...  0.1 MiB 2103 KiB/s [####################] 100%
Successfully installed typing-extensions-4.16.0
$ python
>>> import typing_extensions; typing_extensions.__file__
'…/user_packages/typing_extensions.py'      ← dari disk, bukan APK
```

Sebelum Fix 5 baris `import` itu gagal.

### Akses storage: **sudah diimplementasikan** — `zmux-setup-storage`

Padanan `termux-setup-storage`, dibuat sesuai permintaan Anda:

```
$ zmux-setup-storage
ZMUX storage setup
Permission: requested
Location:   ~/storage
  ~/storage/downloads  -> /storage/emulated/0/Download   [ok]
  ~/storage/shared     -> /storage/emulated/0            [ok]
```

Keputusan desain yang perlu Anda ketahui:
- **Opt-in.** Izin dideklarasikan tapi tidak pernah diminta sampai perintah ini
  dijalankan. Sebelum itu, sandbox ZMUX tidak berubah.
- **`maxSdkVersion=28`.** Sejak Android 10, izin lama ini tidak memberi apa-apa
  (scoped storage), jadi memintanya di versi baru hanya kebisingan.
- **Jujur saat gagal.** Di Android 11+ sebagian besar direktori mungkin tetap
  tidak terjangkau; perintah ini melaporkannya, bukan diam.
- Ini **mengorbankan klaim "INTERNET only"** — sesuai persetujuan Anda, dan
  sudah dicatat di CHANGELOG.

---

## 3. Apakah hasil command masih di-wrap?

**Tidak. Tidak pernah di-wrap, dan sekarang mengalir langsung.**

Yang sampai ke layar adalah byte mentah dari proses anak, diteruskan apa adanya
ke xterm.js. Tidak ada parsing, tidak ada reformat, tidak ada template.

Bukti terukur:

| Yang diuji | Hasil |
|---|---|
| PID anak vs PID kita | `5696` vs `5683` — proses terpisah |
| Process group | `pgid == pid` — grup sendiri |
| Exit code sembarang | `sh -c 'exit 42'` → **42** |
| Kematian oleh sinyal | `kill -TERM $$` → `-15`, `[process terminated by signal 15]` |
| Streaming | tiga `print` berjarak 0.5 s tiba di **t=0.0 / 0.5 / 1.0 s** |
| stderr | terpisah dari stdout, ditandai benar |
| Bar progres `\r` | repaint di tempat, bukan menumpuk baris |

Satu-satunya pemrosesan: `\n` → `\r\n` (wajib untuk terminal mana pun) dan
warna ANSI diteruskan utuh ke xterm.js untuk dirender.

**Batasnya jujur:** karena tidak ada PTY, `isatty()` bernilai `False` bagi
proses anak, jadi program yang hanya mewarnai untuk TTY akan tampil polos, dan
program layar penuh (`top`, `vim`) tetap tidak jalan.

---

## 4. Kalau install module — cuma bundel, atau prebuild sungguhan?

**Bukan bundel. Diunduh dan dipasang sungguhan saat runtime.**

Alurnya, diverifikasi langkah demi langkah:

1. Ambil metadata dari **PyPI/index kurasi lewat HTTPS** (koneksi nyata, 200).
2. Unduh wheel dengan **bar progres langsung** (`2103 KiB/s`).
3. **Verifikasi SHA-256 wajib** — tidak cocok, transaksi dibatalkan.
4. Ekstrak ke staging dengan penolakan path-traversal.
5. **Smoke import** memakai nama modul asli dari wheel (Fix 3).
6. Commit atomik ke `user_packages/`, dengan **rollback penuh** bila gagal.
7. Catat versi, SHA-256, daftar file, dan modul ke database.

Buktinya ini bukan bundel: `user_packages/` **kosong di APK** dan hanya terisi
setelah install. File yang benar-benar tertulis:

```
typing_extensions.py
typing_extensions-4.16.0.dist-info/{METADATA,RECORD,WHEEL,licenses/LICENSE}
```

Dan rollback-nya nyata — inilah yang terjadi pada `rich` sebelum Fix 3:

```
Error: Dependency markdown-it-py failed: Smoke import failed:
ModuleNotFoundError: No module named 'markdown_it_py'
```

`mdurl` sudah terunduh, lalu **dicopot lagi** saat dependensi gagal. Itu
manajer paket transaksional bekerja sebagaimana mestinya.

**Bedanya dengan pip/apt biasa — ini penting:**

| | pip / apt | zpip |
|---|---|---|
| Wheel pure-Python | ✅ | ✅ |
| Wheel native (`.so`) | ✅ dari PyPI | ⚠️ **hanya dari index kurasi** |
| Kompilasi dari source | ✅ | ❌ tidak akan pernah (tak ada compiler di HP) |

Untuk paket native (NumPy, Pillow), zpip **menolak** wheel PyPI karena dibuat
untuk glibc/desktop, bukan untuk Bionic/Android ABI Anda. Wheel-nya harus
di-cross-compile ke dalam index ZABAWHEELS lebih dulu — **dan index stable saat
ini masih kosong** (`"packages": {}`). Jadi hari ini: pure-Python berjalan,
native belum tersedia.

---

## Ringkasan jujur

**Sudah diperbaiki:** empat bug senyap hilang, plus dua yang ditemukan saat
menguji. Terminalnya kini jujur tentang apa yang bisa dan tidak bisa.

**Yang harus Anda sadari:**
1. **`git`/`curl` tidak akan ada di HP** — Android tidak mengirimnya dan ZMUX
   tidak bisa memasang binary. Jalur yang realistis adalah padanan
   murni-Python (`urllib` sudah bekerja; `dulwich` perlu masuk index).
2. **Nol pengujian perangkat.** Ini tetap risiko terbesar.
3. **Index native kosong.** NumPy dkk belum bisa dipasang oleh siapa pun.

**Langkah berikutnya yang paling berdampak, berurutan:**
1. Uji APK di Infinix Anda — terutama `/dev/ptmx` (gerbang untuk rencana PTY)
   dan wrapper `BIN_DIR`.
2. Masukkan `dulwich` ke index → `git` tanpa binary.
3. Kalau `/dev/ptmx` lolos, kerjakan PTY: itu menghapus sisa celah shell
   (`$VAR`, glob, `~`, job control, TUI) sekaligus.
