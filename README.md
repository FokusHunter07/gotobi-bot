# Kantor GotobiAgus — 6 Bot Simulasi Trading → Telegram

Bot ini menjalankan **simulasi trading (paper trading) tanpa uang sungguhan**. Hasilnya dilaporkan ke Telegram dan ditampilkan di **Kantor** animasi (Telegram Mini App). Semua komponennya gratis: GitHub Actions, data tick bid/ask Dukascopy, Telegram Bot, dan GitHub Pages.

## Karyawan kantor
| Bot | Pasar | Aturan |
|---|---|---|
| ⚡ **Goto** | USD/JPY | SELL tanggal 5/10/15/20/25/30 (hari kerja) jam 07:55 WIB · SL 10 pip · close 08:20 WIB |
| ⏰ **Asli** | USD/JPY | Sama seperti Goto, tapi close 08:30 WIB (versi gotobi lama) |
| 🌙 **Kamis** | USD/JPY | BUY setiap Kamis 02:15 WIB · SL 10 pip · close 02:50 WIB |
| 🎉 **Jumat** | USD/JPY | BUY setiap Jumat 22:40 WIB · SL 15 pip · TP 15 pip · close Sabtu 02:40 WIB |
| ☕ **Senin** | USD/JPY | BUY setiap Senin 14:50 WIB · SL 20 pip · TP 20 pip · close 22:50 WIB |
| 🥇 **Emas** | XAU/USD | "Emas Malam": BUY jam 16:30 New York (03:30 WIB Mar–Nov / 04:30 WIB Nov–Mar), Senin–Kamis waktu NY · SL 0,5×ATR(14) harian · TP 1R · close 03:00 New York (14:00/15:00 WIB) |

**Akun virtual tiap bot:** modal $100 + setoran $50 setiap 2 bulan (20 Des, 20 Feb, …), risiko 1% saldo per trade.

**Akun Tim:** Goto, Asli, Kamis, Jumat, dan Senin berbagi satu saldo $100 (+$50 tiap 2 bulan), seperti EA nanti. Risiko maks($1, 1% saldo), dibulatkan ke lot 0,01. Emas tidak ikut sampai modal sekitar $3.300.

## Pesan Telegram
| Waktu (WIB) | Pesan |
|---|---|
| ±07:20 di tanggal gotobi | 🔔 Pengingat Goto & Asli |
| ±08:35 Kamis | 🌙 Hasil Kamis |
| ±08:35 Sabtu | 🎉 Hasil Jumat |
| ±08:35 Selasa | ☕ Hasil Senin |
| ±09:40 di tanggal gotobi | ⚡⏰ Hasil Goto & Asli |
| ±16:40 Selasa–Jumat | 🥇 Hasil Emas |
| Tanggal 1 tiap bulan | 🏆 Klasemen |

Jadwal GitHub kadang telat 5–30 menit. Hasil simulasi tetap akurat karena dihitung dari harga per menit pada jam yang tepat. Trade yang terlewat (misalnya karena data belum terbit) otomatis disusulkan di jadwal berikutnya, sampai 10 hari ke belakang.

## File
- `gotobi_bot.py`: mesin keenam bot + Akun Tim
- `index.html`: Kantor animasi (GitHub Pages)
- `trades.csv` (Goto), `trades_asli.csv`, `trades_kamis.csv`, `trades_jumat.csv`, `trades_senin.csv`, `trades_emas.csv`: catatan trade, diisi otomatis
- `xau_hourly.json`: data per jam emas untuk menghitung ATR, diperbarui otomatis
- `.github/workflows/gotobi.yml`: jadwal otomatis
- `xau_data.py` + `.github/workflows/xau-data.yml`, `fx_data.py` + `.github/workflows/fx-data.yml`: pengunduh data untuk backtest (opsional)
- `ea/GotobiTeam.mq5`: EA MT5 Akun Tim (5 bot USD/JPY), mulai dengan ModeUji = true

## Mode manual (Actions → Gotobi Paper Trading Bot → Run workflow)
- `test`: pesan uji + klasemen
- `report`: proses semua trade yang sudah selesai
- `summary`: klasemen
- `menu`: pasang tombol Kantor di chat

## Mengubah pengaturan
Bagian atas `gotobi_bot.py`: `START_BALANCE`, `RISK_PCT`, `DEPOSIT_USD`, `DEPOSIT_EVERY_MONTHS`. Aturan masing-masing bot ada di kelas `GotobiBot`, `KamisBot`, `WeeklyBot` (Jumat & Senin), dan `EmasBot`.

## Catatan penting
- Ini **simulasi**, bukan nasihat keuangan. Harga broker Anda bisa berbeda, terutama spread emas di sekitar jeda harian. Uji di akun demo broker Anda sebelum memakai uang sungguhan.
- Lot simulasi emas dengan modal $100 sangat kecil (sekitar 0,0003 lot). Di akun real hanya bisa ditiru di **akun cent**.
- Pola "Kamis", "Jumat", dan "Senin" ditemukan dari pemindaian data 2023–2026 dan belum punya penjelasan pasti. Karena itu bot-bot ini memang untuk diuji dulu. Backtest: Jumat 199 trade, menang 67%; Senin 198 trade, menang 64% (tahun 2025 lebih lemah, 56%).
- GitHub mematikan jadwal otomatis di repo yang tidak aktif selama 60 hari. Catatan trade dari bot biasanya membuat repo tetap aktif.
