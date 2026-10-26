"""
Kantor GotobiAgus - Bot Simulasi Trading (paper trading) -> laporan ke Telegram
==============================================================================
Empat "karyawan", masing-masing punya akun virtual sendiri:

  ⚡ Goto  (USD/JPY)  SELL tgl 5/10/15/20/25/30 jam 07:55 WIB, SL 10 pip, close 08:20 WIB
  ⏰ Asli  (USD/JPY)  sama seperti Goto tetapi close 08:30 WIB (versi gotobi lama)
  🌙 Kamis (USD/JPY)  BUY setiap Kamis 02:15 WIB, SL 10 pip, close 02:50 WIB
  🥇 Emas  (XAU/USD)  "Emas Malam": BUY jam 16:30 New York (03:30/04:30 WIB) Senin-Kamis NY,
                      SL 0,5 x ATR(14) harian, TP 1R, close jam 03:00 New York (14:00/15:00 WIB)

Akun virtual tiap bot: modal awal $100 + setoran $50 tiap 2 bulan, risiko 1% saldo per trade.
Data: tick bid/ask asli dari Dukascopy (gratis, tanpa API key). Yahoo = cadangan untuk USD/JPY.

Mode:
  python gotobi_bot.py report   -> simulasikan semua trade yang sudah selesai & belum tercatat, kirim hasil
  python gotobi_bot.py signal   -> pengingat sinyal gotobi pagi (Goto & Asli)
  python gotobi_bot.py summary  -> klasemen & ringkasan semua bot
  python gotobi_bot.py test     -> pesan uji
  python gotobi_bot.py menu     -> pasang tombol menu 'Kantor' (Mini App) di chat bot

Variabel lingkungan (GitHub Secrets / workflow):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, WEBAPP_URL
"""

import csv
import datetime as dt
import json
import lzma
import os
import struct
import sys
import time
from zoneinfo import ZoneInfo

import requests

# ====================== PENGATURAN UMUM ======================
HERE = os.path.dirname(os.path.abspath(__file__))
WIB = dt.timezone(dt.timedelta(hours=7))
UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")
HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

START_BALANCE = 100.0          # modal awal virtual tiap bot
RISK_PCT = 1.0                 # % saldo yang dirisikokan per trade
SIM_START = dt.date(2026, 10, 20)   # tanggal mulai akun virtual
DEPOSIT_USD = 50.0             # setoran rutin virtual
DEPOSIT_EVERY_MONTHS = 2       # tiap N bulan sejak SIM_START (20 Des, 20 Feb, ...)
LOOKBACK_DAYS = 10             # trade yang terlewat dalam N hari terakhir akan disusulkan
YAHOO_SPREAD_PIPS = 1.0        # asumsi spread jika terpaksa memakai data Yahoo (tanpa harga ask)
GOTOBI_DAYS = {5, 10, 15, 20, 25, 30}

BOTS = {
    "goto":  {"nama": "Goto",  "emoji": "⚡", "pasar": "USD/JPY", "file": "trades.csv",
              "aturan": "SELL tgl gotobi 07:55 WIB · SL 10 pip · close 08:20 WIB"},
    "asli":  {"nama": "Asli",  "emoji": "⏰", "pasar": "USD/JPY", "file": "trades_asli.csv",
              "aturan": "SELL tgl gotobi 07:55 WIB · SL 10 pip · close 08:30 WIB"},
    "kamis": {"nama": "Kamis", "emoji": "🌙", "pasar": "USD/JPY", "file": "trades_kamis.csv",
              "aturan": "BUY tiap Kamis 02:15 WIB · SL 10 pip · close 02:50 WIB"},
    "emas":  {"nama": "Emas",  "emoji": "🥇", "pasar": "XAU/USD", "file": "trades_emas.csv",
              "aturan": "BUY 16:30 New York (03:30/04:30 WIB) Sen–Kam NY · SL 0,5×ATR · TP 1R · "
                        "close 03:00 New York (14:00/15:00 WIB)"},
}
ORDER = ["goto", "asli", "kamis", "emas"]

FIELDS = ["tanggal", "hari", "arah", "entry", "stop_loss", "take_profit", "exit", "jam_entry_wib",
          "jam_keluar_wib", "hasil", "pips", "R", "risiko_usd", "pl_usd", "saldo", "lot", "sumber",
          "setoran", "modal_disetor"]

# ====================== TELEGRAM ======================
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[DRY-RUN] TELEGRAM_TOKEN/CHAT_ID belum diisi. Pesan:\n" + text + "\n")
        return
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if WEBAPP_URL:
        data["reply_markup"] = json.dumps({"inline_keyboard": [[
            {"text": "🏢 Buka Kantor", "web_app": {"url": WEBAPP_URL}}]]})
    for _ in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)
            if r.ok:
                return
            print("Telegram error:", r.status_code, r.text)
        except requests.RequestException as e:
            print("Telegram exception:", e)
        time.sleep(3)


# ====================== DATA HARGA (DUKASCOPY) ======================
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"}
SCALE = {"USDJPY": 1000.0, "XAUUSD": 1000.0}
_session = requests.Session()
_session.headers.update(UA)
_tick_cache = {}


class DataNotReady(Exception):
    """Data untuk jam yang dibutuhkan belum dipublikasikan sumber data."""


def _get(url):
    """Unduh file Dukascopy. None = 404 (belum ada). Coba ulang jika server sibuk."""
    last = None
    for attempt in range(6):
        try:
            r = _session.get(url, timeout=40)
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                return r.content
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        print(f"  {url.split('datafeed/')[-1]}: {last}, coba lagi ({attempt + 1}/6)")
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Dukascopy gagal: {last}")


def hour_ticks(symbol, hour_utc):
    """List (detik_epoch, ask, bid) untuk satu jam UTC."""
    key = (symbol, hour_utc)
    if key in _tick_cache:
        return _tick_cache[key]
    url = (f"https://datafeed.dukascopy.com/datafeed/{symbol}/{hour_utc.year}/{hour_utc.month - 1:02d}/"
           f"{hour_utc.day:02d}/{hour_utc.hour:02d}h_ticks.bi5")
    c = _get(url)
    if c is None:
        raise DataNotReady(f"{symbol} jam {hour_utc:%Y-%m-%d %H}:00 UTC belum tersedia")
    out = []
    if c:
        raw = lzma.decompress(c)
        base = hour_utc.timestamp()
        sc = SCALE[symbol]
        for off in range(0, len(raw) - 19, 20):
            ms, ask, bid, _av, _bv = struct.unpack(">IIIff", raw[off:off + 20])
            out.append((base + ms / 1000.0, ask / sc, bid / sc))
    _tick_cache[key] = out
    return out


def minute_bars(symbol, start_utc, end_utc):
    """Candle 1 menit bid/ask dari tick. Kunci = datetime UTC awal menit.
    bo/bh/bl/bc = bid open/high/low/close, ao/ah/al/ac = ask."""
    bars = {}
    h = start_utc.replace(minute=0, second=0, microsecond=0)
    while h <= end_utc:
        for ts, ask, bid in hour_ticks(symbol, h):
            t = dt.datetime.fromtimestamp(ts, UTC).replace(second=0, microsecond=0)
            b = bars.get(t)
            if b is None:
                bars[t] = {"bo": bid, "bh": bid, "bl": bid, "bc": bid,
                           "ao": ask, "ah": ask, "al": ask, "ac": ask}
            else:
                b["bh"] = max(b["bh"], bid); b["bl"] = min(b["bl"], bid); b["bc"] = bid
                b["ah"] = max(b["ah"], ask); b["al"] = min(b["al"], ask); b["ac"] = ask
        h += dt.timedelta(hours=1)
    return bars


def yahoo_usdjpy(start_utc, end_utc):
    """Cadangan untuk USD/JPY: candle 1 menit Yahoo (tanpa ask -> pakai asumsi spread)."""
    params = {"interval": "1m", "period1": int(start_utc.timestamp()),
              "period2": int(end_utc.timestamp()) + 60, "includePrePost": "false"}
    last = None
    for host in ("query1", "query2"):
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/JPY=X",
                             params=params, headers=UA, timeout=30)
            r.raise_for_status()
            res = r.json()["chart"]["result"]
            if not res:
                return {}
            res = res[0]
            q = res["indicators"]["quote"][0]
            sp = YAHOO_SPREAD_PIPS * 0.01
            bars = {}
            for i, t in enumerate(res.get("timestamp") or []):
                o, hi, lo, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if None in (o, hi, lo, c):
                    continue
                k = dt.datetime.fromtimestamp(t, UTC).replace(second=0, microsecond=0)
                bars[k] = {"bo": o, "bh": hi, "bl": lo, "bc": c,
                           "ao": o + sp, "ah": hi + sp, "al": lo + sp, "ac": c + sp}
            return bars
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
    raise RuntimeError(f"Yahoo gagal: {last}")


def get_bars(symbol, start_utc, end_utc, allow_yahoo=False):
    """Kembalikan (bars, sumber). DataNotReady jika data belum terbit."""
    try:
        return minute_bars(symbol, start_utc, end_utc), "Dukascopy"
    except DataNotReady:
        raise
    except Exception as e:  # noqa: BLE001
        if not allow_yahoo:
            raise RuntimeError(str(e)) from e
        print(f"Dukascopy error ({e}), mencoba Yahoo...")
        return yahoo_usdjpy(start_utc, end_utc), "Yahoo"


def first_bar(bars, t, tol_min):
    for k in range(tol_min + 1):
        tt = t + dt.timedelta(minutes=k)
        if tt in bars:
            return tt
    return None


# ====================== CACHE ATR EMAS ======================
XAU_CACHE = os.path.join(HERE, "xau_hourly.json")
ORDERS = {"OCLH": (0, 1, 2, 3), "OHLC": (0, 3, 2, 1)}


def _parse_min_candles(raw):
    recs = []
    for off in range(0, len(raw) - 23, 24):
        t, p1, p2, p3, p4, v = struct.unpack(">IIIIIf", raw[off:off + 24])
        if v > 0:
            recs.append((t, (p1 / 1000.0, p2 / 1000.0, p3 / 1000.0, p4 / 1000.0)))
    best, best_ok = "OCLH", -1
    for name, (io_, ic, il, ih) in ORDERS.items():
        ok = sum(1 for _, p in recs if p[il] <= min(p[io_], p[ic]) and p[ih] >= max(p[io_], p[ic]))
        if ok > best_ok:
            best, best_ok = name, ok
    io_, ic, il, ih = ORDERS[best]
    return [(t, p[io_], p[ih], p[il], p[ic]) for t, p in recs]     # (detik, o, h, l, c)


def load_xau_cache():
    if os.path.exists(XAU_CACHE):
        with open(XAU_CACHE, encoding="utf-8") as f:
            return json.load(f)
    return {"last_day": None, "bars": {}}


def update_xau_cache():
    """Tambahkan candle per jam (BID) untuk hari UTC yang sudah selesai. Data untuk ATR harian emas."""
    cache = load_xau_cache()
    today = dt.datetime.now(UTC).date()
    d = dt.date.fromisoformat(cache["last_day"]) + dt.timedelta(days=1) if cache["last_day"] \
        else today - dt.timedelta(days=40)
    changed = False
    while d < today:
        if d.weekday() != 5:          # Sabtu pasar tutup
            url = (f"https://datafeed.dukascopy.com/datafeed/XAUUSD/{d.year}/{d.month - 1:02d}/"
                   f"{d.day:02d}/BID_candles_min_1.bi5")
            try:
                c = _get(url)
            except RuntimeError as e:
                print("Cache emas:", e)
                break
            if c is None:
                print(f"Cache emas: data {d} belum tersedia")
                break
            if c:
                base = int(dt.datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())
                hb = {}
                for t, o, h, l, cl in _parse_min_candles(lzma.decompress(c)):
                    k = (base + t) // 3600 * 3600
                    b = hb.get(k)
                    if b is None:
                        hb[k] = [o, h, l, cl]
                    else:
                        b[1] = max(b[1], h); b[2] = min(b[2], l); b[3] = cl
                for k, b in hb.items():
                    cache["bars"][str(k)] = [round(x, 2) for x in b]
        cache["last_day"] = d.isoformat()
        changed = True
        d += dt.timedelta(days=1)
    if changed:
        cutoff = int((dt.datetime.now(UTC) - dt.timedelta(days=150)).timestamp())
        cache["bars"] = {k: v for k, v in cache["bars"].items() if int(k) >= cutoff}
        with open(XAU_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
    return cache


def ny_session(t_utc):
    """Tanggal sesi harian (tutup 17:00 New York)."""
    n = t_utc.astimezone(NY)
    return n.date() + dt.timedelta(days=1) if n.hour >= 17 else n.date()


def xau_atr(entry_utc, cache):
    """ATR(14) dari sesi-sesi harian sebelum sesi entry (sama dengan backtest)."""
    sess = {}
    for k, (o, h, l, c) in sorted(((int(k), v) for k, v in cache["bars"].items())):
        s = ny_session(dt.datetime.fromtimestamp(k, UTC))
        x = sess.get(s)
        if x is None:
            sess[s] = [o, h, l, c, 1]
        else:
            x[1] = max(x[1], h); x[2] = min(x[2], l); x[3] = c; x[4] += 1
    es = ny_session(entry_utc)
    valid = [(s, v) for s, v in sorted(sess.items()) if s < es and v[4] >= 5]
    if len(valid) < 15:
        return None
    trs = []
    for i in range(1, len(valid)):
        _, (o, h, l, c, n) = valid[i]
        pc = valid[i - 1][1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-14:]) / 14


# ====================== SIMULASI TIAP BOT ======================
def utc_at(d, h, m):
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=UTC)


def sim_time_exit(bars, side, entry_t, exit_t, sl_dist, tp_dist=None, tol_entry=3, tol_exit=3):
    """Simulasi umum. side +1 BUY (masuk di ask, keluar di bid), -1 SELL.
    SL dicek lebih dulu dari TP dalam satu candle (konservatif)."""
    et = first_bar(bars, entry_t, tol_entry)
    if et is None:
        return None
    if not any(t >= exit_t - dt.timedelta(minutes=2) for t in bars):
        return None                                   # data belum sampai jam keluar
    b0 = bars[et]
    e = b0["ao"] if side > 0 else b0["bo"]
    sl = e - side * sl_dist
    tp = e + side * tp_dist if tp_dist else None
    t = et
    while t < exit_t:
        b = bars.get(t)
        if b:
            if side > 0:
                if b["bl"] <= sl:
                    return e, sl, tp, sl, et, t, "Kena SL"
                if tp and b["bh"] >= tp:
                    return e, sl, tp, tp, et, t, "Kena TP"
            else:
                if b["ah"] >= sl:
                    return e, sl, tp, sl, et, t, "Kena SL"
                if tp and b["al"] <= tp:
                    return e, sl, tp, tp, et, t, "Kena TP"
        t += dt.timedelta(minutes=1)
    xt = first_bar(bars, exit_t, tol_exit)
    if xt:
        x = bars[xt]["bo"] if side > 0 else bars[xt]["ao"]
    else:
        prev = max(k for k in bars if k < exit_t)
        xt = prev
        x = bars[prev]["bc"] if side > 0 else bars[prev]["ac"]
    return e, sl, tp, x, et, xt, "Tutup waktu"


class Bot:
    key = ""
    symbol = "USDJPY"
    side = -1
    unit = "pip"

    def candidates(self, now_utc):
        """List (tanggal_kunci, entry_utc, exit_utc) dalam LOOKBACK_DAYS."""
        raise NotImplementedError

    def run(self, d, entry_utc, exit_utc):
        raise NotImplementedError

    @property
    def cfg(self):
        return BOTS[self.key]


class GotobiBot(Bot):
    symbol = "USDJPY"
    side = -1
    unit = "pip"

    def __init__(self, key, exit_hm):
        self.key = key
        self.exit_hm = exit_hm

    def candidates(self, now_utc):
        out = []
        today = now_utc.date()
        for back in range(LOOKBACK_DAYS, -1, -1):
            d = today - dt.timedelta(days=back)
            if d.day in GOTOBI_DAYS and d.weekday() < 5:
                out.append((d, utc_at(d, 0, 55), utc_at(d, *self.exit_hm)))
        return out

    def run(self, d, entry_utc, exit_utc):
        bars, src = get_bars("USDJPY", entry_utc.replace(minute=0), exit_utc, allow_yahoo=True)
        res = sim_time_exit(bars, -1, entry_utc, exit_utc, 0.10)
        return res, src, 0.10


class KamisBot(Bot):
    key = "kamis"
    symbol = "USDJPY"
    side = 1
    unit = "pip"

    def candidates(self, now_utc):
        out = []
        today = now_utc.date()
        for back in range(LOOKBACK_DAYS, -1, -1):
            d = today - dt.timedelta(days=back)
            if d.weekday() == 2:                       # Rabu UTC 19:15 = Kamis 02:15 WIB
                out.append((d + dt.timedelta(days=1), utc_at(d, 19, 15), utc_at(d, 19, 50)))
        return out

    def run(self, d, entry_utc, exit_utc):
        bars, src = get_bars("USDJPY", entry_utc.replace(minute=0), exit_utc, allow_yahoo=True)
        res = sim_time_exit(bars, 1, entry_utc, exit_utc, 0.10)
        return res, src, 0.10


class EmasBot(Bot):
    key = "emas"
    symbol = "XAUUSD"
    side = 1
    unit = "$"
    SL_ATR = 0.5
    TP_R = 1.0

    def __init__(self):
        self.cache = None

    def candidates(self, now_utc):
        out = []
        today_ny = now_utc.astimezone(NY).date()
        for back in range(LOOKBACK_DAYS, -1, -1):
            nd = today_ny - dt.timedelta(days=back)
            if nd.weekday() > 3:                       # hanya Senin-Kamis (waktu New York)
                continue
            entry = dt.datetime(nd.year, nd.month, nd.day, 16, 30, tzinfo=NY).astimezone(UTC)
            nx = nd + dt.timedelta(days=1)
            exit_ = dt.datetime(nx.year, nx.month, nx.day, 3, 0, tzinfo=NY).astimezone(UTC)
            out.append((entry.astimezone(WIB).date(), entry, exit_))
        return out

    def run(self, d, entry_utc, exit_utc):
        if self.cache is None:
            self.cache = update_xau_cache()
        atr = xau_atr(entry_utc, self.cache)
        if atr is None:
            raise DataNotReady("data ATR emas belum cukup")
        bars, src = get_bars("XAUUSD", entry_utc, exit_utc)
        sl = self.SL_ATR * atr
        res = sim_time_exit(bars, 1, entry_utc, exit_utc, sl, self.TP_R * sl, tol_entry=10, tol_exit=15)
        return res, src, sl


ENGINES = {"goto": GotobiBot("goto", (1, 20)), "asli": GotobiBot("asli", (1, 30)),
           "kamis": KamisBot(), "emas": EmasBot()}


# ====================== SETORAN RUTIN ======================
def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    last = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30,
            31, 31, 30, 31, 30, 31][m - 1]
    return dt.date(y, m, min(d.day, last))


def total_deposited(upto):
    n, k = 0, 1
    while add_months(SIM_START, DEPOSIT_EVERY_MONTHS * k) <= upto:
        n += 1
        k += 1
    return START_BALANCE + DEPOSIT_USD * n


def next_deposit(after):
    k = 1
    while add_months(SIM_START, DEPOSIT_EVERY_MONTHS * k) <= after:
        k += 1
    return add_months(SIM_START, DEPOSIT_EVERY_MONTHS * k)


def disetor(t):
    v = t.get("modal_disetor")
    return float(v) if v not in (None, "") else START_BALANCE


# ====================== PENYIMPANAN & STATISTIK ======================
def path(key):
    return os.path.join(HERE, BOTS[key]["file"])


def load_trades(key):
    p = path(key)
    if not os.path.exists(p):
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return sorted(csv.DictReader(f), key=lambda r: r["tanggal"])


def save_trades(key, trades):
    with open(path(key), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for t in sorted(trades, key=lambda r: r["tanggal"]):
            w.writerow({k: t.get(k, "") for k in FIELDS})


def stats(trades):
    n = len(trades)
    if n == 0:
        return None
    Rs = [float(t["R"]) for t in trades]
    wins = sum(r > 0 for r in Rs)
    bal = float(trades[-1]["saldo"])
    dep = disetor(trades[-1])
    peak, mdd, idx = 1.0, 0.0, 1.0
    for r in Rs:
        idx *= 1 + RISK_PCT / 100 * r
        peak = max(peak, idx)
        mdd = min(mdd, (idx - peak) / peak)
    gw = sum(r for r in Rs if r > 0)
    gl = -sum(r for r in Rs if r <= 0)
    profit = bal - dep
    return dict(n=n, wins=wins, wr=wins / n * 100, sls=sum(t["hasil"] == "Kena SL" for t in trades),
                totR=sum(Rs), bal=bal, dep=dep, profit=profit, ret=profit / dep * 100,
                mdd=mdd * 100, pf=gw / gl if gl > 0 else float("inf"))


def stats_text(key, trades):
    s = stats(trades)
    c = BOTS[key]
    if not s:
        return f"{c['emoji']} {c['nama']}: belum ada trade tercatat."
    pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"📊 <b>Performa {c['nama']} sejak awal</b>\n"
            f"Trade: {s['n']} | Menang: {s['wins']} | Win rate: {s['wr']:.1f}%\n"
            f"Kena SL: {s['sls']} | Total: {s['totR']:+.2f}R | Profit factor: {pf}\n"
            f"Saldo virtual: ${s['bal']:.2f} | Modal disetor: ${s['dep']:.2f}\n"
            f"Profit bersih: ${s['profit']:+.2f} ({s['ret']:+.1f}% dari setoran) | Max DD: {s['mdd']:.1f}%")


# ====================== MODE ======================
def cmd_report():
    now = dt.datetime.now(UTC)
    any_new = False
    for key in ORDER:
        eng = ENGINES[key]
        c = BOTS[key]
        trades = load_trades(key)
        done = {t["tanggal"] for t in trades}
        new = []
        for d, entry, exit_ in eng.candidates(now):
            if d < SIM_START or d.isoformat() in done:
                continue
            if now < exit_ + dt.timedelta(minutes=5):
                continue                                   # trade belum selesai
            try:
                res, src, sl_dist = eng.run(d, entry, exit_)
            except DataNotReady as e:
                print(f"{c['nama']} {d}: {e}. Dicoba lagi di jadwal berikutnya.")
                continue
            except Exception as e:  # noqa: BLE001
                print(f"{c['nama']} {d}: gagal mengambil data -> {e}. Dicoba lagi nanti.")
                continue
            if res is None:
                print(f"{c['nama']} {d}: tidak ada harga di jam entry (pasar tutup/libur?)")
                continue
            e, sl, tp, x, et, xt, hasil = res
            last = trades[-1] if trades else None
            prev_dep = disetor(last) if last else START_BALANCE
            balance = float(last["saldo"]) if last else START_BALANCE
            dep_now = total_deposited(d)
            setoran = max(0.0, dep_now - prev_dep)
            balance += setoran
            side = eng.side
            move = side * (x - e)
            R = move / sl_dist
            risk_usd = balance * RISK_PCT / 100
            pl = round(risk_usd * R, 2)
            if eng.symbol == "USDJPY":
                lot = risk_usd / (sl_dist / 0.01 * 1000 / e)      # 1 lot: 1 pip = 1000 JPY
                gerak = move / 0.01
                dec = 3
            else:
                lot = risk_usd / (sl_dist * 100)                   # 1 lot emas = 100 oz
                gerak = move
                dec = 2
            row = {
                "tanggal": d.isoformat(), "hari": HARI[d.weekday()], "arah": "BUY" if side > 0 else "SELL",
                "entry": f"{e:.{dec}f}", "stop_loss": f"{sl:.{dec}f}", "take_profit": f"{tp:.{dec}f}" if tp else "",
                "exit": f"{x:.{dec}f}", "jam_entry_wib": et.astimezone(WIB).strftime("%H:%M"),
                "jam_keluar_wib": xt.astimezone(WIB).strftime("%H:%M"), "hasil": hasil,
                "pips": f"{gerak:.1f}", "R": f"{R:.2f}", "risiko_usd": f"{risk_usd:.2f}", "pl_usd": f"{pl:.2f}",
                "saldo": f"{balance + pl:.2f}", "lot": f"{lot:.4f}", "sumber": src,
                "setoran": f"{setoran:.2f}", "modal_disetor": f"{max(dep_now, prev_dep):.2f}",
            }
            trades.append(row)
            trades.sort(key=lambda r: r["tanggal"])
            done.add(row["tanggal"])
            new.append(row)
        if not new:
            print(f"{c['nama']}: tidak ada trade baru.")
            continue
        save_trades(key, trades)
        any_new = True
        for r in new:
            if float(r.get("setoran") or 0) > 0:
                send_telegram(f"💰 <b>{c['emoji']} {c['nama']}: setoran virtual +${float(r['setoran']):.0f}</b> "
                              f"masuk. Modal disetor sekarang ${float(r['modal_disetor']):.0f}.")
            icon = "✅" if float(r["R"]) > 0 else "❌"
            unit = "pip" if eng.unit == "pip" else "$"
            gerak = f"{float(r['pips']):+.1f} pip" if unit == "pip" else f"{float(r['pips']):+.2f}$/oz"
            tp_txt = f" | TP {r['take_profit']}" if r["take_profit"] else ""
            send_telegram(
                f"{icon} <b>{c['emoji']} {c['nama']} ({c['pasar']})</b> — {r['hari']}, {r['tanggal']}\n"
                f"{r['arah']} @ {r['entry']} jam {r['jam_entry_wib']} WIB | SL {r['stop_loss']}{tp_txt}\n"
                f"Keluar @ {r['exit']} jam {r['jam_keluar_wib']} WIB ({r['hasil']})\n"
                f"Hasil: <b>{gerak} = {float(r['R']):+.2f}R = ${float(r['pl_usd']):+.2f}</b>\n"
                f"Lot simulasi: {r['lot']} (risiko ${r['risiko_usd']}) · data: {r['sumber']}\n\n"
                + stats_text(key, [t for t in trades if t["tanggal"] <= r["tanggal"]]))
    if any_new:
        send_telegram(team_text())


# ====================== AKUN TIM (satu dompet untuk semua bot) ======================
TEAM_MIN_RISK = 1.0      # risiko minimal per trade ($) saat saldo di bawah $100
DEF_IN = {"goto": "07:55", "asli": "07:55", "kamis": "02:15", "emas": "03:30"}
DEF_OUT = {"goto": "08:20", "asli": "08:30", "kamis": "02:50", "emas": "14:00"}


def team():
    """Simulasi satu saldo bersama: risiko = maks($1, 1% saldo saat entry).
    Posisi yang belum ditutup tidak memengaruhi saldo untuk trade berikutnya."""
    def ts(d, hm):
        h, m = map(int, (hm or "00:00").split(":"))
        return dt.datetime.fromisoformat(d).replace(hour=h, minute=m)
    ev = []
    for k in ORDER:
        for t in load_trades(k):
            ev.append((ts(t["tanggal"], t.get("jam_entry_wib") or DEF_IN[k]), ORDER.index(k), k,
                       float(t["R"]), ts(t["tanggal"], t.get("jam_keluar_wib") or DEF_OUT[k])))
    ev.sort()
    deps = []
    k = 1
    today = dt.datetime.now(WIB).date()
    while add_months(SIM_START, DEPOSIT_EVERY_MONTHS * k) <= today:
        deps.append(dt.datetime.combine(add_months(SIM_START, DEPOSIT_EVERY_MONTHS * k), dt.time(12)))
        k += 1
    bal, dep, opened, per, wins = START_BALANCE, START_BALANCE, [], {x: 0.0 for x in ORDER}, 0
    di = 0
    for ent, _, key, R, ex in ev:
        for o in sorted(o for o in opened if o[0] <= ent):
            bal += o[1]
            opened.remove(o)
        while di < len(deps) and deps[di] <= ent:
            bal += DEPOSIT_USD; dep += DEPOSIT_USD; di += 1
        risk = max(TEAM_MIN_RISK, bal * RISK_PCT / 100)
        pl = round(risk * R, 2)
        opened.append((ex, pl))
        per[key] += pl
        wins += R > 0
    bal += sum(o[1] for o in opened)
    while di < len(deps):
        bal += DEPOSIT_USD; dep += DEPOSIT_USD; di += 1
    return dict(bal=bal, dep=dep, profit=bal - dep, n=len(ev), wr=wins / len(ev) * 100 if ev else 0,
                per=per, risk=max(TEAM_MIN_RISK, bal * RISK_PCT / 100))


def team_text():
    t = team()
    parts = " · ".join(f"{BOTS[k]['emoji']} {t['per'][k]:+.2f}" for k in ORDER)
    return (f"🤝 <b>Akun Tim</b> (1 dompet untuk 4 bot)\n"
            f"Saldo ${t['bal']:.2f} | disetor ${t['dep']:.0f} | profit ${t['profit']:+.2f} "
            f"({t['profit'] / t['dep'] * 100:+.1f}%)\n"
            f"{t['n']} trade · menang {t['wr']:.0f}% · risiko berikutnya ${t['risk']:.2f}\n"
            f"Kontribusi: {parts}")


def klasemen_text():
    rows = []
    for key in ORDER:
        s = stats(load_trades(key))
        rows.append((key, s))
    rows.sort(key=lambda x: -(x[1]["ret"] if x[1] else -1e9))
    medal = ["🥇", "🥈", "🥉", "4️⃣"]
    lines = ["🏆 <b>Klasemen Kantor GotobiAgus</b>"]
    for i, (key, s) in enumerate(rows):
        c = BOTS[key]
        if s:
            lines.append(f"{medal[i]} {c['emoji']} <b>{c['nama']}</b> ({c['pasar']}): {s['ret']:+.1f}% · "
                         f"WR {s['wr']:.0f}% · {s['n']} trade · saldo ${s['bal']:.2f}")
        else:
            lines.append(f"{medal[i]} {c['emoji']} <b>{c['nama']}</b> ({c['pasar']}): belum ada trade")
    nd = next_deposit(dt.datetime.now(WIB).date())
    lines.append(f"\n💰 Setoran virtual berikutnya: ${DEPOSIT_USD:.0f} pada {nd:%d-%m-%Y}")
    lines.append("\n" + team_text())
    return "\n".join(lines)


def cmd_summary():
    send_telegram(klasemen_text())


def cmd_signal():
    today = dt.datetime.now(WIB).date()
    if not (today.day in GOTOBI_DAYS and today.weekday() < 5):
        print("Hari ini bukan tanggal gotobi.")
        return
    send_telegram(
        f"🔔 <b>Hari ini tanggal GOTOBI</b> ({HARI[today.weekday()]}, {today:%d-%m-%Y})\n"
        f"⚡ Goto & ⏰ Asli: SELL USD/JPY jam <b>07:55 WIB</b>, SL 10 pip\n"
        f"• Goto close <b>08:20 WIB</b> · Asli close <b>08:30 WIB</b>\n"
        f"Hasil simulasi dikirim setelah data tersedia (±09:40 WIB).")


def cmd_menu():
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
    send_telegram("🏢 Tombol <b>Kantor</b> sudah terpasang di kiri bawah kolom chat.")


def cmd_test():
    send_telegram("✅ Kantor GotobiAgus aktif: ⚡ Goto, ⏰ Asli, 🌙 Kamis, 🥇 Emas siap bekerja.\n\n" + klasemen_text())


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"signal": cmd_signal, "report": cmd_report, "summary": cmd_summary, "test": cmd_test,
     "menu": cmd_menu}.get(mode, cmd_report)()
