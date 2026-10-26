"""
Pengunduh data XAU/USD (emas) dari Dukascopy -> data/xauusd_m5.csv.gz
=====================================================================
Dipakai untuk backtest & bot divisi XAU (Box Theory).
- Mengunduh candle 1 menit BID dan ASK per hari (gratis, tanpa API key)
- Menggabungkannya menjadi candle 5 menit
- Hanya mengunduh hari yang belum ada -> aman dijalankan berulang (melanjutkan sisa)
- Pelan & sopan ke server: jika server sibuk, hari itu dilewati dan dicoba lagi di run berikutnya
- Batas waktu per run: hasil yang sudah didapat tetap disimpan

Format file (harga dalam sen, dibagi 100 = USD):
  t           = waktu buka candle (menit sejak 1970-01-01 UTC)
  bo,bh,bl,bc = BID open/high/low/close
  ao,ah,al    = ASK open/high/low
"""

import csv
import datetime as dt
import gzip
import io
import lzma
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor

import requests

SYMBOL = "XAUUSD"
START = dt.date(2023, 1, 1)
SCALE = 1000.0                     # harga Dukascopy XAUUSD dalam 1/1000
WORKERS = 2                        # jangan terlalu banyak, server Dukascopy membatasi
TIME_BUDGET_MIN = 75               # berhenti mengambil hari baru setelah ini, lalu simpan
SAVE_EVERY = 60                    # simpan progres setiap N hari
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "xauusd_m5.csv.gz")
FIELDS = ["t", "bo", "bh", "bl", "bc", "ao", "ah", "al"]
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"}
ORDERS = {"OCLH": (0, 1, 2, 3), "OHLC": (0, 3, 2, 1)}   # posisi (open, close, low, high)

session = requests.Session()
session.headers.update(UA)


class Busy(Exception):
    """Server sibuk / gagal terus untuk file ini."""


def get(url):
    """Unduh satu file. None = tidak ada data (404). Busy = gagal setelah beberapa percobaan."""
    for attempt in range(7):
        try:
            r = session.get(url, timeout=40)
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                return r.content
        except requests.RequestException:
            pass
        time.sleep(min(60, 4 * 2 ** attempt))       # 4, 8, 16, 32, 60, 60 detik
    raise Busy(url)


def parse_candles(raw):
    """Kembalikan list (detik_offset, open, close, low, high). Candle tanpa volume dibuang.
    Urutan harga dideteksi otomatis: pilih urutan di mana low <= open/close <= high."""
    recs = []
    for off in range(0, len(raw) - 23, 24):
        t, p1, p2, p3, p4, v = struct.unpack(">IIIIIf", raw[off:off + 24])
        if v > 0:
            recs.append((t, (p1 / SCALE, p2 / SCALE, p3 / SCALE, p4 / SCALE)))
    best, best_ok = "OCLH", -1
    for name, (io_, ic, il, ih) in ORDERS.items():
        ok = sum(1 for _, p in recs if p[il] <= min(p[io_], p[ic]) and p[ih] >= max(p[io_], p[ic]))
        if ok > best_ok:
            best, best_ok = name, ok
    io_, ic, il, ih = ORDERS[best]
    return [(t, p[io_], p[ic], p[il], p[ih]) for t, p in recs]


def day_candles(d, side):
    url = (f"https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{d.year}/{d.month - 1:02d}/"
           f"{d.day:02d}/{side}_candles_min_1.bi5")
    c = get(url)
    time.sleep(0.4)                                  # jeda sopan antar permintaan
    if not c:
        return []
    return parse_candles(lzma.decompress(c))


def fetch_day(d):
    """Kembalikan (tanggal, dict menit->candle) atau (tanggal, None) jika gagal."""
    try:
        bid = day_candles(d, "BID")
        ask = day_candles(d, "ASK")
    except Busy:
        return d, None
    except lzma.LZMAError:
        return d, None
    base = int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() // 60)
    askmap = {t: (o, c, l, h) for t, o, c, l, h in ask}
    minute = {}
    for t, o, c, l, h in bid:
        a = askmap.get(t)
        if not a:
            continue
        ao, _ac, al, ah = a
        minute[base + t // 60] = (o, h, l, c, ao, ah, al)
    return d, minute


def to_m5(minute):
    m5 = {}
    for t in sorted(minute):
        o, h, l, c, ao, ah, al = minute[t]
        k = t - t % 5
        b = m5.get(k)
        if b is None:
            m5[k] = [o, h, l, c, ao, ah, al]
        else:
            b[1] = max(b[1], h); b[2] = min(b[2], l); b[3] = c
            b[5] = max(b[5], ah); b[6] = min(b[6], al)
    return {k: {"t": k, "bo": round(b[0] * 100), "bh": round(b[1] * 100), "bl": round(b[2] * 100),
                "bc": round(b[3] * 100), "ao": round(b[4] * 100), "ah": round(b[5] * 100),
                "al": round(b[6] * 100)} for k, b in m5.items()}


def load():
    rows = {}
    if os.path.exists(OUT):
        with gzip.open(OUT, "rt") as f:
            for row in csv.DictReader(f):
                rows[int(row["t"])] = row
    return rows


def save(rows):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    for k in sorted(rows, key=int):
        w.writerow(rows[k])
    tmp = OUT + ".tmp"
    with gzip.open(tmp, "wt") as f:
        f.write(buf.getvalue())
    os.replace(tmp, OUT)


def main():
    t0 = time.time()
    rows = load()
    have_days = {dt.datetime.fromtimestamp(t * 60, dt.timezone.utc).date() for t in rows}

    today = dt.datetime.now(dt.timezone.utc).date()
    days = []
    d = START
    while d < today:                                  # hari ini belum lengkap
        if d.weekday() != 5 and d not in have_days:   # Sabtu selalu tutup
            days.append(d)
        d += dt.timedelta(days=1)
    if have_days:                                     # unduh ulang hari terakhir agar lengkap
        days.append(max(have_days))
    days = sorted(set(days))
    print(f"Sudah ada {len(have_days)} hari. Perlu diunduh: {len(days)} hari.", flush=True)

    done, failed, stopped = 0, [], False
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        pending = list(days)
        while pending:
            if (time.time() - t0) / 60 > TIME_BUDGET_MIN:
                stopped = True
                break
            batch, pending = pending[:SAVE_EVERY], pending[SAVE_EVERY:]
            for d, minute in ex.map(fetch_day, batch):
                done += 1
                if minute is None:
                    failed.append(d)
                elif minute:
                    rows.update(to_m5(minute))
            save(rows)
            print(f"  {done}/{len(days)} hari diproses, gagal sementara: {len(failed)}, "
                  f"{(time.time() - t0) / 60:.1f} menit", flush=True)

    save(rows)
    if rows:
        first = dt.datetime.fromtimestamp(int(min(rows, key=int)) * 60, dt.timezone.utc)
        last = dt.datetime.fromtimestamp(int(max(rows, key=int)) * 60, dt.timezone.utc)
        print(f"Tersimpan {len(rows)} candle 5 menit: {first:%Y-%m-%d} s/d {last:%Y-%m-%d %H:%M} UTC")
    left = len(days) - done + len(failed)
    if stopped or failed:
        print(f"BELUM LENGKAP: {left} hari tersisa/gagal. Jalankan workflow ini sekali lagi "
              f"untuk melanjutkan (data yang sudah ada tidak diunduh ulang).")
    else:
        print("LENGKAP: semua hari sudah terunduh.")


if __name__ == "__main__":
    main()
