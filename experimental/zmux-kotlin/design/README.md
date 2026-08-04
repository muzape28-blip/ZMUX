# ZMUX Ember — desain UI native

Identitas visual untuk client Kotlin. **Bukan port** dari tema GitHub-dark milik WebView
(`#0d1117` / `#58a6ff` / `#3fb950`) — ini permukaan produk yang berbeda, jadi dikasih wajah
sendiri.

Buka `preview.html` di browser buat lihat hasilnya (boot screen, terminal, dan state).

---

## Tiga keputusan yang mendasari semuanya

### 1. True black `#000000`, bukan abu gelap

Bukan gaya-gayaan. Di panel OLED/AMOLED — yang sekarang umum bahkan di kelas budget — piksel
hitam itu **benar-benar mati**, nggak makan daya. Buat terminal yang kebuka berjam-jam di HP
Android Go, itu baterai beneran.

WebView nggak bisa melakukan ini dengan jujur: compositor browser selalu melukis surface.
Native bisa.

### 2. Duotone: ember & teal

Satu aturan yang dipakai konsisten di seluruh UI:

| Warna | Arti | Dipakai di |
|---|---|---|
| **Ember `#FF8A3D`** | **kamu** — sistem nunggu input | kursor, tab aktif, CTRL yang ke-latch |
| **Teal `#5EE9D5`** | **mesin** — backend siap/kerja | status connected, dot sesi busy |
| Rose `#FF5D73` | destruktif | `^C`, `^D`, hold-to-close |
| Amber `#FFC857` | transisi | connecting, reconnecting |

Efeknya: sekali lirik langsung tahu terminal lagi nunggu **kamu** atau nunggu **mesin**. Ini
informasi yang di UI lama harus dibaca teksnya dulu.

### 3. Teks putih hangat `#E8E3DD`, bukan abu dingin

Dipadu latar hitam pekat, ini lebih adem buat sesi panjang dibanding `#c9d1d9` di `#0d1117`.

---

## Komponen yang digambar tangan (bukan widget bawaan)

Semua ini `View` custom dengan `onDraw()` sendiri. Alasannya: `Button` bawaan Android nggak
bisa mengekspresikan state-state ini tanpa tumpukan selector XML, padahal tiap state itu
informasi nyata.

### `SessionTabView` — hold-to-close beneran kelihatan

Ide bagus dari UI web yang gw bangun ulang secara native:

- **tap** → pindah sesi
- **tahan** → isian rose menyapu dari kiri ke kanan, di 100% sesi ditutup + haptic
- **lepas lebih awal / geser keluar** → batal

Bedanya dari long-press biasa: **progresnya kelihatan**. Kamu tahu persis kapan bakal nutup
dan masih sempat batal.

### `StatusPillView` — lampu yang bernapas

Lampu status bukan titik statis:

- state transisi (connecting / reconnecting) → **bernapas** pakai sinus pelan
- state settled (connected / disconnected) → **diam**

Jadi *gerak = nunggu*, *diam = selesai*. Bisa kebaca dari sudut mata tanpa perlu baca
tulisannya.

### `KeyCapView` — tiga state yang punya makna

- **latched** (CTRL siap buat tombol berikutnya) → isian ember, mustahil kelupaan
- **repeating** (panah/backspace) → garis bawah ember muncul pas auto-repeat jalan, jadi
  kelihatan tombolnya lagi nembak, bukan nebak
- **danger** (`^C`, `^D`) → teks rose; tombol destruktif nggak boleh keliatan sama kayak `/`

Timing auto-repeat sama persis dengan `terminal.html`: 400 ms lalu 55 ms.

### `BootBrandView` — monogram Z sebagai prompt

Huruf **Z** distroke pakai gradien ember→teal, dengan kursor ember blinking di sebelahnya.
Vektor, bukan PNG — tajam di semua density dan **nol tambahan ukuran APK**, penting buat
target Android Go.

Gradiennya sengaja: ember ketemu teal di titik prompt — user dan mesin ketemu di situ.

---

## Perbedaan struktural dari UI WebView

| | WebView (ZABAWHEELS) | Ember (native) |
|---|---|---|
| Connect form | nggak ada (token disuntik ke HTML) | **boot overlay** merangkap form |
| Boot splash | PNG logo | monogram vektor beranimasi |
| Tab close | hold + progress CSS | hold + progress digambar Canvas |
| Status | titik + teks | pill dengan lampu bernapas |
| Latar | `#0d1117` | `#000000` (AMOLED) |
| Aksen | biru/hijau GitHub | duotone ember/teal |

Yang **dipertahankan**: tabel 21 tombol virtual (port 1:1 dari `KEY_ROWS`), sticky CTRL, dan
timing auto-repeat. Itu keputusan bagus, nggak ada alasan diubah.

---

## Palet lengkap

```
bg          #000000    surface     #12101A    sunk      #0A0910
outline     #241F2E    text        #E8E3DD    dim       #6B6478
ember       #FF8A3D    teal        #5EE9D5    rose      #FF5D73
amber       #FFC857    violet      #C792EA
```

16 warna ANSI ada di `ZmuxTheme.ANSI_16`, disetel supaya nggak "bergetar" di atas hitam pekat.
Catatan penting: ANSI black dinaikkan ke `#1A1720`, bukan `#000000` murni — kalau nggak,
output `ls` yang pakai warna hitam bakal **tak terlihat** di latar hitam.

---

## Status

Palet diterapkan lewat `TerminalEmulator.mColors.mCurrentColors` — cara yang sama dipakai
Termux sendiri buat `colors.properties`, jadi ini memakai library sesuai maksudnya, bukan
akal-akalan.

Pemanggilannya dibungkus `runCatching`: field itu pernah berpindah antar rilis Termux, dan
kegagalan tema **tidak boleh** menjatuhkan terminal. Kalau gagal, warna default Termux yang
dipakai.

> Belum dikompilasi (sandbox nggak punya JDK/Android SDK). `preview.html` adalah rekonstruksi
> HTML/CSS yang setia dari kode `onDraw()`, bukan screenshot dari device.
