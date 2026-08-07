# Dokumentasi ZMUX

Dokumentasi di sini dikelompokkan berdasarkan tujuannya, supaya jelas mana yang
mencatat **keputusan**, mana yang **laporan kemajuan**, dan mana yang **analisis**.

## Struktur

- **`decisions/`** — keputusan arsitektur dan alasan di baliknya (ADR).
  - `KOTLIN_RUST_DECISION.md` — kenapa Kotlin untuk lapisan UI, kenapa Rust tidak.
- **`progress/`** — laporan kemajuan dan status implementasi.
  - `PROGRESS_KOTLIN_UI_POC.md` — kemajuan PoC UI Kotlin + spesifikasi protokol WebSocket.
- **`analysis/`** — investigasi / cross-check teknis (bukan keputusan).
  - `CROSSCHECK_PERFORMANCE.md` — cross-check performa `apk add` vs Termux+proot-distro,
    akar masalah, dan status implementasi tiga perbaikan.

## Catatan

Beberapa dokumen lama mereferensikan `docs/*.md` lain (mis. `RUST_KOTLIN_ANALYSIS.md`,
`REFERENCE_MINING.md`) yang tidak pernah masuk ke repo ini — itu rujukan naratif ke
analisis dari repositori ZABAWHEELS, bukan tautan yang hidup.
