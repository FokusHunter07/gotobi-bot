"""
Gotobi v2 Paper-Trading Bot (USD/JPY) -> laporan ke Telegram
=============================================================
Strategi : SELL USD/JPY jam 07:55 WIB, SL 10 pip, close jam 08:20 WIB
Tanggal  : 5, 10, 15, 20, 25, 30 (hanya hari kerja, Senin-Jumat)
Akun     : virtual, modal awal $100, risiko 1% per trade (compounding)
Data     : Dukascopy tick bid/ask (utama) + Yahoo Finance 1 menit (cadangan), gratis tanpa API key

Mode:
  python gotobi_bot.py signal   -> kirim pengingat sinyal (jika hari ini tanggal gotobi)
  python gotobi_bot.py report   -> simulasikan trade yang belum tercatat (7 hari terakhir) & kirim hasil
  python gotobi_bot.py summary  -> kirim ringkasan performa keseluruhan
  python gotobi_bot.py test     -> kirim pesan uji ke Telegram
  python gotobi_bot.py menu     -> pasang tombol menu 'Kantor' (Mini App) di chat bot

Variabel lingkungan (diisi lewat GitHub Secrets):
  TELEGRAM_TOKEN   = token bot dari @BotFather
  TELEGRAM_CHAT_ID = chat id Anda
  WEBAPP_URL       = alamat halaman Kantor (otomatis diisi oleh workflow)
"""

import csv
import datetime as dt
import json
import lzma
import os
import struct
import sys
import time

import requests

# ---------------- PENGATURAN STRATEGI ----------------
SYMBOL = "JPY=X"              # USD/JPY di Yahoo Finance
GOTOBI_DAYS = {5, 10, 15, 20, 25, 30}
ENTRY_UTC = (0, 55)           # 07:55 WIB
EXIT_UTC = (1, 20)            # 08:20 WIB
SL_PIPS = 10
PIP = 0.01
SPREAD_PIPS = 1.0             # asumsi spread, hanya dipakai jika data dari Yahoo (tanpa harga ask)
START_BALANCE = 100.0
RISK_PCT = 1.0                # % saldo yang dirisikokan per trade
LOOKBACK_DAYS = 7             # trade yang terlewat dalam 7 hari terakhir akan disusulkan
TRADES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.csv")
WIB = dt.timezone(dt.timedelta(hours=7))
HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
FIELDS = ["tanggal", "hari", "entry", "stop_loss", "exit", "jam_keluar_wib", "hasil",
          "pips", "R", "risiko_usd", "pl_usd", "saldo", "lot", "sumber"]


# ---------------- TELEGRAM ----------------
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()   # alamat halaman Kantor (GitHub Pages)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[DRY-RUN] TELEGRAM_TOKEN/CHAT_ID belum diisi. Pesan:\n" + text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if WEBAPP_URL:
        data["reply_markup"] = json.dumps({"inline_keyboard": [[
            {"text": "🏢 Buka Kantor", "web_app": {"url": WEBAPP_URL}}]]})
    for attempt in range(3):
        try:
            r = requests.post(url, data=data, timeout=20)
            if r.ok:
                return
            print("Telegram error:", r.status_code, r.text)
        except requests.RequestException as e:
            print("Telegram exception:", e)
        time.sleep(3)


# ---------------- KALENDER ----------------
def is_gotobi(d):
    return d.day in GOTOBI_DAYS and d.weekday() < 5


def utc_dt(d, hm):
    return dt.datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=dt.timezone.utc)


def next_gotobi(from_date):
    d = from_date
    for _ in range(40):
        if is_gotobi(d):
            return d
        d += dt.timedelta(days=1)
    return None


# ---------------- DATA HARGA ----------------
# Setiap candle 1 menit disimpan sebagai dict: bo/bh/bl/bc = bid open/high/low/close,
# ao/ah = ask open/high. Posisi SELL dibuka di harga bid, ditutup (beli kembali) di harga ask.
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


class DataNotReady(Exception):
    """Data untuk jam yang dibutuhkan belum dipublikasikan sumber data."""


def fetch_dukascopy(d):
    """Sumber utama: data tick Dukascopy (bid & ask asli, gratis).
    File per jam baru tersedia sekitar 30-90 menit setelah jam itu berakhir."""
    bars = {}
    for hour in (0, 1):
        url = (f"https://datafeed.dukascopy.com/datafeed/USDJPY/"
               f"{d.year}/{d.month - 1:02d}/{d.day:02d}/{hour:02d}h_ticks.bi5")
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code == 404:
            raise DataNotReady(f"Dukascopy jam {hour:02d} UTC belum tersedia")
        r.raise_for_status()
        if not r.content:
            continue  # tidak ada tick (pasar tutup)
        raw = lzma.decompress(r.content)
        base = dt.datetime(d.year, d.month, d.day, hour, tzinfo=dt.timezone.utc)
        for off in range(0, len(raw) - 19, 20):
            ms, ask, bid, _av, _bv = struct.unpack(">IIIff", raw[off:off + 20])
            ask, bid = ask / 1000.0, bid / 1000.0
            t = (base + dt.timedelta(milliseconds=ms)).replace(second=0, microsecond=0)
            b = bars.get(t)
            if b is None:
                bars[t] = {"bo": bid, "bh": bid, "bl": bid, "bc": bid, "ao": ask, "ah": ask}
            else:
                b["bh"] = max(b["bh"], bid); b["bl"] = min(b["bl"], bid); b["bc"] = bid
                b["ah"] = max(b["ah"], ask)
    return bars


def fetch_yahoo(d):
    """Sumber cadangan: Yahoo Finance candle 1 menit (tanpa harga ask -> pakai asumsi spread)."""
    start = int(utc_dt(d, (0, 30)).timestamp())
    end = int(utc_dt(d, (1, 45)).timestamp())
    params = {"interval": "1m", "period1": start, "period2": end, "includePrePost": "false"}
    last_err = None
    for host in ("query1", "query2"):
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{SYMBOL}",
                             params=params, headers=UA, timeout=30)
            r.raise_for_status()
            res = r.json()["chart"]["result"]
            if not res:
                return {}
            res = res[0]
            q = res["indicators"]["quote"][0]
            sp = SPREAD_PIPS * PIP
            bars = {}
            for i, t in enumerate(res.get("timestamp") or []):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if None in (o, h, l, c):
                    continue
                key = dt.datetime.fromtimestamp(t, dt.timezone.utc).replace(second=0, microsecond=0)
                bars[key] = {"bo": o, "bh": h, "bl": l, "bc": c, "ao": o + sp, "ah": h + sp}
            return bars
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(3)
    raise RuntimeError(f"Yahoo gagal: {last_err}")


def fetch_bars(d):
    """Coba Dukascopy dulu, lalu Yahoo. Kembalikan (bars, nama_sumber)."""
    errors = []
    try:
        bars = fetch_dukascopy(d)
        if bars:
            return bars, "Dukascopy"
        errors.append("Dukascopy: data kosong")
    except DataNotReady as e:
        errors.append(str(e))
    except Exception as e:  # noqa: BLE001
        errors.append(f"Dukascopy error: {e}")
    try:
        bars = fetch_yahoo(d)
        if bars:
            return bars, "Yahoo"
        errors.append("Yahoo: data kosong")
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
    raise RuntimeError(" | ".join(errors))


# ---------------- SIMULASI ----------------
def simulate(d, bars, balance, source=""):
    """Simulasikan satu trade SELL. Kembalikan dict baris trade atau None jika data belum cukup."""
    entry_t = utc_dt(d, ENTRY_UTC)
    exit_t = utc_dt(d, EXIT_UTC)

    # harga entry = bid open candle 00:55 UTC (toleransi 3 menit jika candle kosong)
    entry = None
    for k in range(4):
        b = bars.get(entry_t + dt.timedelta(minutes=k))
        if b:
            entry = b["bo"]
            entry_t = entry_t + dt.timedelta(minutes=k)
            break
    if entry is None:
        return None
    # harus ada data sampai mendekati jam exit, kalau tidak berarti data belum lengkap
    if not any(t >= exit_t - dt.timedelta(minutes=2) for t in bars):
        return None

    sl = entry + SL_PIPS * PIP
    hit_t = None
    t = entry_t
    while t < exit_t:
        b = bars.get(t)
        if b and b["ah"] >= sl - 1e-9:      # harga ask menyentuh SL
            hit_t = t
            break
        t += dt.timedelta(minutes=1)

    if hit_t:
        exit_price, pips, hasil = sl, -float(SL_PIPS), "Kena SL"
        jam = hit_t.astimezone(WIB).strftime("%H:%M")
    else:
        b = bars.get(exit_t)
        if b:
            exit_price = b["ao"]
        else:
            prev = max(k for k in bars if k < exit_t)
            exit_price = bars[prev]["bc"] + (bars[prev]["ao"] - bars[prev]["bo"])
        pips = round((entry - exit_price) / PIP, 1)
        hasil = "Close 08:20"
        jam = exit_t.astimezone(WIB).strftime("%H:%M")

    R = pips / SL_PIPS
    risk_usd = balance * RISK_PCT / 100
    pl = round(risk_usd * R, 2)
    lot = risk_usd / (SL_PIPS * 1000 / entry)   # 1 lot standar: 1 pip = 1000 JPY
    return {
        "tanggal": d.isoformat(), "hari": HARI[d.weekday()], "entry": f"{entry:.3f}",
        "stop_loss": f"{sl:.3f}", "exit": f"{exit_price:.3f}", "jam_keluar_wib": jam,
        "hasil": hasil, "pips": f"{pips:.1f}", "R": f"{R:.2f}", "risiko_usd": f"{risk_usd:.2f}",
        "pl_usd": f"{pl:.2f}", "saldo": f"{balance + pl:.2f}", "lot": f"{lot:.4f}", "sumber": source,
    }


# ---------------- PENYIMPANAN ----------------
def load_trades():
    if not os.path.exists(TRADES_FILE):
        return []
    with open(TRADES_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_trades(trades):
    trades = sorted(trades, key=lambda r: r["tanggal"])
    with open(TRADES_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(trades)


def stats(trades):
    n = len(trades)
    if n == 0:
        return None
    wins = sum(float(t["R"]) > 0 for t in trades)
    sls = sum(t["hasil"] == "Kena SL" for t in trades)
    totR = sum(float(t["R"]) for t in trades)
    bal = float(trades[-1]["saldo"])
    peak, mdd, eq = START_BALANCE, 0.0, START_BALANCE
    for t in trades:
        eq = float(t["saldo"])
        peak = max(peak, eq)
        mdd = min(mdd, (eq - peak) / peak)
    gw = sum(float(t["R"]) for t in trades if float(t["R"]) > 0)
    gl = -sum(float(t["R"]) for t in trades if float(t["R"]) <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    return dict(n=n, wins=wins, wr=wins / n * 100, sls=sls, totR=totR, bal=bal,
                ret=(bal / START_BALANCE - 1) * 100, mdd=mdd * 100, pf=pf)


def stats_text(trades):
    s = stats(trades)
    if not s:
        return "Belum ada trade tercatat."
    pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"📊 <b>Performa sejak awal</b>\n"
            f"Trade: {s['n']} | Menang: {s['wins']} | Win rate: {s['wr']:.1f}%\n"
            f"Kena SL: {s['sls']} | Total: {s['totR']:+.2f}R | Profit factor: {pf}\n"
            f"Saldo virtual: ${s['bal']:.2f} ({s['ret']:+.1f}%) | Max DD: {s['mdd']:.1f}%")


# ---------------- MODE ----------------
def cmd_signal():
    today = dt.datetime.now(WIB).date()
    if not is_gotobi(today):
        print("Hari ini bukan tanggal gotobi.")
        return
    send_telegram(
        f"🔔 <b>Sinyal Gotobi hari ini</b> ({HARI[today.weekday()]}, {today:%d-%m-%Y})\n"
        f"• SELL USD/JPY jam <b>07:55 WIB</b>\n"
        f"• Stop loss {SL_PIPS} pip\n"
        f"• Close jam <b>08:20 WIB</b>\n"
        f"Hasil simulasi dikirim setelah 08:20 WIB.")


def cmd_report():
    trades = load_trades()
    done = {t["tanggal"] for t in trades}
    today = dt.datetime.now(dt.timezone.utc).date()
    now_utc = dt.datetime.now(dt.timezone.utc)
    new = []
    for back in range(LOOKBACK_DAYS, -1, -1):
        d = today - dt.timedelta(days=back)
        if not is_gotobi(d) or d.isoformat() in done:
            continue
        if now_utc < utc_dt(d, EXIT_UTC) + dt.timedelta(minutes=3):
            continue  # trade hari ini belum selesai
        balance = float(sorted(trades, key=lambda r: r["tanggal"])[-1]["saldo"]) if trades else START_BALANCE
        try:
            bars, source = fetch_bars(d)
        except RuntimeError as e:
            print(f"{d}: data belum bisa diambil -> {e}. Akan dicoba lagi di jadwal berikutnya.")
            continue
        row = simulate(d, bars, balance, source)
        if row is None:
            print(f"{d}: data belum lengkap / pasar tutup, coba lagi nanti.")
            continue
        trades.append(row)
        trades.sort(key=lambda r: r["tanggal"])
        new.append(row)
    if not new:
        print("Tidak ada trade baru untuk dilaporkan.")
        return
    save_trades(trades)
    for r in new:
        icon = "✅" if float(r["R"]) > 0 else "❌"
        send_telegram(
            f"{icon} <b>Hasil simulasi Gotobi</b> — {r['hari']}, {r['tanggal']}\n"
            f"SELL @ {r['entry']} | SL {r['stop_loss']}\n"
            f"Keluar @ {r['exit']} jam {r['jam_keluar_wib']} WIB ({r['hasil']})\n"
            f"Hasil: <b>{float(r['pips']):+.1f} pip = {float(r['R']):+.2f}R = ${float(r['pl_usd']):+.2f}</b>\n"
            f"Lot simulasi: {r['lot']} (risiko ${r['risiko_usd']}) · data: {r.get('sumber', '-')}\n\n"
            + stats_text(trades))
    nxt = next_gotobi(dt.datetime.now(WIB).date() + dt.timedelta(days=1))
    if nxt:
        send_telegram(f"📅 Jadwal berikutnya: {HARI[nxt.weekday()]}, {nxt:%d-%m-%Y} jam 07:55 WIB")


def cmd_summary():
    trades = load_trades()
    txt = stats_text(trades)
    if trades:
        last = trades[-5:]
        txt += "\n\n🧾 <b>5 trade terakhir</b>\n" + "\n".join(
            f"{t['tanggal']}: {float(t['R']):+.2f}R ({t['hasil']})" for t in last)
    send_telegram(txt)


def cmd_menu():
    """Pasang tombol menu 'Kantor' di chat bot (cukup dijalankan sekali)."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id and WEBAPP_URL):
        print("TELEGRAM_TOKEN, TELEGRAM_CHAT_ID dan WEBAPP_URL harus terisi.")
        sys.exit(1)
    r = requests.post(f"https://api.telegram.org/bot{token}/setChatMenuButton", data={
        "chat_id": chat_id,
        "menu_button": json.dumps({"type": "web_app", "text": "Kantor", "web_app": {"url": WEBAPP_URL}})},
        timeout=20)
    print(r.status_code, r.text)
    if not r.ok:
        sys.exit(1)
    send_telegram("🏢 Tombol <b>Kantor</b> sudah terpasang di kiri bawah kolom chat. "
                  "Tekan untuk melihat Goto bekerja!")


def cmd_test():
    send_telegram("✅ Bot Gotobi aktif dan terhubung ke Telegram Anda.\n" + stats_text(load_trades()))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"signal": cmd_signal, "report": cmd_report, "summary": cmd_summary, "test": cmd_test,
     "menu": cmd_menu}.get(
        mode, cmd_report)()
