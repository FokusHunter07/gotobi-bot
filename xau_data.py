"""
Pengunduh data XAU/USD (emas) dari Dukascopy -> data/xauusd_m5.csv.gz
=====================================================================
Dipakai untuk backtest & bot divisi XAU (Box Theory).
- Mengunduh candle 1 menit BID dan ASK per hari (gratis, tanpa API key)
- Menggabungkannya menjadi candle 5 menit
- Hanya mengunduh hari yang belum ada (bisa dijalankan berulang)

Format file (harga dalam sen, dibagi 100 = USD):
  t      = waktu buka candle (menit sejak 1970-01-01 UTC)
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
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

SYMBOL = "XAUUSD"
START = dt.date(2023, 1, 1)
SCALE = 1000.0                    # harga Dukascopy XAUUSD dalam 1/1000
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "xauusd_m5.csv.gz")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"}


def get(url):
    for attempt in range(8):
        try:
            r = requests.get(url, headers=UA, timeout=40)
            if r.status_code == 404:
                return None
            if r.status_code < 500 and r.status_code != 429:
                r.raise_for_status()
                return r.content
        except requests.RequestException:
            pass
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Gagal mengunduh {url}")


ORDERS = {"OCLH": (0, 1, 2, 3), "OHLC": (0, 3, 2, 1)}   # posisi (open, close, low, high)


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
    if not c:
        return []
    return parse_candles(lzma.decompress(c))


def fetch_day(d):
    bid = day_candles(d, "BID")
    ask = day_candles(d, "ASK")
    return d, bid, ask


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    existing = {}
    if os.path.exists(OUT):
        with gzip.open(OUT, "rt") as f:
            for row in csv.DictReader(f):
                existing[int(row["t"])] = row
    have_days = {dt.datetime.utcfromtimestamp(t * 60).date() for t in existing}

    today = dt.datetime.utcnow().date()
    days = []
    d = START
    while d < today:                       # hari ini belum lengkap, jadi dilewati
        if d.weekday() != 5 and d not in have_days:   # Sabtu selalu tutup
            days.append(d)
        d += dt.timedelta(days=1)
    # hari terakhir yang tersimpan diunduh ulang agar lengkap
    if have_days:
        days.append(max(have_days))
    days = sorted(set(days))
    print(f"Mengunduh {len(days)} hari...")

    minute = {}
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for d, bid, ask in ex.map(fetch_day, days):
            base = int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() // 60)
            askmap = {t: (o, c, l, h) for t, o, c, l, h in ask}
            for t, o, c, l, h in bid:
                a = askmap.get(t)
                if not a:
                    continue
                ao, ac, al, ah = a
                minute[base + t // 60] = (o, h, l, c, ao, ah, al)
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(days)} hari")

    # gabung ke candle 5 menit
    m5 = {}
    for t in sorted(minute):
        o, h, l, c, ao, ah, al = minute[t]
        k = t - t % 5
        b = m5.get(k)
        if b is None:
            m5[k] = [o, h, l, c, ao, ah, al, 1]
        else:
            b[1] = max(b[1], h); b[2] = min(b[2], l); b[3] = c
            b[5] = max(b[5], ah); b[6] = min(b[6], al); b[7] += 1

    rows = dict(existing)
    for k, b in m5.items():
        rows[k] = {"t": k, "bo": round(b[0] * 100), "bh": round(b[1] * 100), "bl": round(b[2] * 100),
                   "bc": round(b[3] * 100), "ao": round(b[4] * 100), "ah": round(b[5] * 100),
                   "al": round(b[6] * 100)}

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["t", "bo", "bh", "bl", "bc", "ao", "ah", "al"])
    w.writeheader()
    for k in sorted(rows, key=int):
        w.writerow(rows[k])
    with gzip.open(OUT, "wt") as f:
        f.write(buf.getvalue())
    first = dt.datetime.utcfromtimestamp(int(min(rows, key=int)) * 60)
    last = dt.datetime.utcfromtimestamp(int(max(rows, key=int)) * 60)
    print(f"Selesai: {len(rows)} candle 5 menit, {first:%Y-%m-%d} s/d {last:%Y-%m-%d %H:%M} UTC -> {OUT}")


if __name__ == "__main__":
    sys.exit(main())
