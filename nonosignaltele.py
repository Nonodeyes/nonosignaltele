import asyncio
import logging
import aiohttp
from datetime import datetime, timedelta
from telegram import Bot
from telegram.ext import Application
 
TELEGRAM_TOKEN  = "8892446543:AAEcKd1xdSa9ym09dR7puQT7wdrVXxPzmGY"
TELEGRAM_CHAT   = "-1003953608700"  # Groupe nonosignaltele
 
logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO)
 
# ════════════════════════════════════════════════════════════
# PARAMÈTRES — IDENTIQUES AU ROBOT MT5 nonolescenaristeGoe
# ════════════════════════════════════════════════════════════
EMA_FAST       = 20
EMA_SLOW       = 50
FIB_LOOKBACK   = 50
OTE_LO         = 0.50
OTE_HI         = 0.786
OTE_TOLERANCE  = 0.05
TP1_RR         = 1.0
TP2_RR         = 2.0
TP3_RR         = 3.0
MAX_TRADES_DAY = 5
 
# Symboles à surveiller
SYMBOLS = {
    "XAUUSD": {
        "ticker":     "GC=F",
        "fvg_min":    0.5,
        "name":       "GOLD",
        "emoji":      "🥇",
        "decimals":   2,
    },
    "BTCUSD": {
        "ticker":     "BTC-USD",
        "fvg_min":    10.0,
        "name":       "BTC",
        "emoji":      "₿",
        "decimals":   0,
    },
}
 
# État par symbole
state = {
    sym: {
        "last_signal_time": None,
        "last_signal_dir":  None,
        "trades_today":     0,
        "last_trade_day":   None,
        "last_fvg_top":     0,
        "last_fvg_bot":     0,
    }
    for sym in SYMBOLS
}
 
# ════════════════════════════════════════════════════════════
# RÉCUPÉRATION DES DONNÉES (Yahoo Finance)
# ════════════════════════════════════════════════════════════
 
async def get_candles(ticker, interval="5m", period="5d"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={period}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                data = await r.json()
                result = data["chart"]["result"][0]
                timestamps = result["timestamp"]
                closes = result["indicators"]["quote"][0]["close"]
                highs  = result["indicators"]["quote"][0]["high"]
                lows   = result["indicators"]["quote"][0]["low"]
                opens  = result["indicators"]["quote"][0]["open"]
                candles = []
                for i in range(len(timestamps)):
                    if closes[i] and highs[i] and lows[i] and opens[i]:
                        candles.append({
                            "time":  datetime.fromtimestamp(timestamps[i]),
                            "open":  opens[i],
                            "high":  highs[i],
                            "low":   lows[i],
                            "close": closes[i],
                        })
                return candles
    except Exception as e:
        logging.error(f"Erreur candles {ticker}: {e}")
        return []
 
async def get_current_price(ticker):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
                return data["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except:
        return 0
 
# ════════════════════════════════════════════════════════════
# CALCUL EMA
# ════════════════════════════════════════════════════════════
 
def calc_ema(closes, period):
    if len(closes) < period:
        return closes[-1] if closes else 0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema
 
def get_trend(candles, tf_count=50):
    if len(candles) < tf_count:
        return "NONE"
    closes = [c["close"] for c in candles[-tf_count:]]
    highs  = [c["high"]  for c in candles[-tf_count:]]
    lows   = [c["low"]   for c in candles[-tf_count:]]
    ema_fast = calc_ema(closes, EMA_FAST)
    ema_slow = calc_ema(closes, EMA_SLOW)
    h1, h3 = highs[-1], highs[-3] if len(highs) >= 3 else highs[-1]
    l2, l4 = lows[-2],  lows[-4]  if len(lows)  >= 4 else lows[-1]
    bull_ema = ema_fast > ema_slow
    bear_ema = ema_fast < ema_slow
    bull_str = h1 > h3 and l2 > l4
    bear_str = h1 < h3 and l2 < l4
    if bull_ema and bull_str: return "BULL"
    if bear_ema and bear_str: return "BEAR"
    if bull_ema: return "BULL"
    if bear_ema: return "BEAR"
    return "NONE"
 
# ════════════════════════════════════════════════════════════
# FIBONACCI OTE
# ════════════════════════════════════════════════════════════
 
def calc_fibonacci(candles_h1):
    if len(candles_h1) < FIB_LOOKBACK:
        return None
    recent  = candles_h1[-FIB_LOOKBACK:]
    sw_high = max(c["high"] for c in recent)
    sw_low  = min(c["low"]  for c in recent)
    bar_h = max(range(len(recent)), key=lambda i: recent[i]["high"])
    bar_l = min(range(len(recent)), key=lambda i: recent[i]["low"])
    if sw_high <= sw_low:
        return None
    bullish = bar_l > bar_h
    rng = sw_high - sw_low
    if bullish:
        f50  = sw_high - rng * 0.500
        f786 = sw_high - rng * 0.786
        f100 = sw_low
    else:
        f50  = sw_low + rng * 0.500
        f786 = sw_low + rng * 0.786
        f100 = sw_high
    raw_lo = min(f50, f786)
    raw_hi = max(f50, f786)
    tol    = rng * OTE_TOLERANCE
    return {
        "ote_lo":  raw_lo - tol,
        "ote_hi":  raw_hi + tol,
        "f100":    f100,
        "bullish": bullish,
        "sw_high": sw_high,
        "sw_low":  sw_low,
    }
 
# ════════════════════════════════════════════════════════════
# FVG
# ════════════════════════════════════════════════════════════
 
def find_fvg(candles_m5, trend, fvg_min, sym):
    s = state[sym]
    lookback = min(20, len(candles_m5) - 2)
    for i in range(2, lookback):
        bot = candles_m5[-i-1]["high"]
        top = candles_m5[-i+1]["low"]
        if top > bot:
            size = top - bot
            if size >= fvg_min and trend == "BULL":
                if abs(top - s["last_fvg_top"]) < fvg_min and abs(bot - s["last_fvg_bot"]) < fvg_min:
                    continue
                return {"top": top, "bot": bot, "bullish": True}
        top2 = candles_m5[-i-1]["low"]
        bot2 = candles_m5[-i+1]["high"]
        if top2 > bot2:
            size = top2 - bot2
            if size >= fvg_min and trend == "BEAR":
                if abs(top2 - s["last_fvg_top"]) < fvg_min and abs(bot2 - s["last_fvg_bot"]) < fvg_min:
                    continue
                return {"top": top2, "bot": bot2, "bullish": False}
    return None
 
# ════════════════════════════════════════════════════════════
# CALCUL ATR
# ════════════════════════════════════════════════════════════
 
def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return candles[-1]["high"] - candles[-1]["low"]
    trs = []
    for i in range(1, period + 1):
        c = candles[-i]
        prev_close = candles[-i-1]["close"]
        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - prev_close),
            abs(c["low"]  - prev_close)
        )
        trs.append(tr)
    return sum(trs) / period
 
# ════════════════════════════════════════════════════════════
# ANALYSE PAR SYMBOLE
# ════════════════════════════════════════════════════════════
 
async def analyze_symbol(sym):
    cfg = SYMBOLS[sym]
    s   = state[sym]
 
    today = datetime.now().date()
    if s["last_trade_day"] != today:
        s["trades_today"]  = 0
        s["last_trade_day"] = today
 
    if s["trades_today"] >= MAX_TRADES_DAY:
        logging.info(f"{sym}: limite 5 trades/jour atteinte")
        return None
 
    ticker = cfg["ticker"]
    candles_h4 = await get_candles(ticker, "1h",  "30d")
    candles_h1 = await get_candles(ticker, "1h",  "10d")
    candles_m5 = await get_candles(ticker, "5m",  "5d")
 
    if not candles_h1 or not candles_m5:
        return None
 
    price = await get_current_price(ticker)
    if not price:
        return None
 
    trend_major  = get_trend(candles_h4, 100)
    trend_medium = get_trend(candles_h1, 50)
    trend_minor  = get_trend(candles_m5, 30)
 
    if   trend_major  != "NONE": trend_global = trend_major
    elif trend_medium != "NONE": trend_global = trend_medium
    else:                        trend_global = trend_minor
 
    if trend_global == "NONE":
        return None
 
    fib = calc_fibonacci(candles_h1)
    if not fib:
        return None
 
    in_ote = fib["ote_lo"] <= price <= fib["ote_hi"]
    if not in_ote:
        return None
 
    fvg = find_fvg(candles_m5, trend_global, cfg["fvg_min"], sym)
    if not fvg:
        return None
 
    if trend_global == "BULL" and not fvg["bullish"]:
        return None
    if trend_global == "BEAR" and fvg["bullish"]:
        return None
 
    atr    = calc_atr(candles_h1, 14)
    sl_buf = atr * 0.5
    is_buy = (trend_global == "BULL")
 
    sl = fvg["bot"] - sl_buf if is_buy else fvg["top"] + sl_buf
    sl_dist = abs(price - sl)
    if sl_dist < atr * 0.3:
        sl = price - atr if is_buy else price + atr
        sl_dist = atr
 
    tp1 = price + sl_dist * TP1_RR if is_buy else price - sl_dist * TP1_RR
    tp2 = price + sl_dist * TP2_RR if is_buy else price - sl_dist * TP2_RR
    tp3 = price + sl_dist * TP3_RR if is_buy else price - sl_dist * TP3_RR
 
    rr1 = round(abs(tp1 - price) / sl_dist, 1) if sl_dist > 0 else 0
    rr2 = round(abs(tp2 - price) / sl_dist, 1) if sl_dist > 0 else 0
    rr3 = round(abs(tp3 - price) / sl_dist, 1) if sl_dist > 0 else 0
 
    d = cfg["decimals"]
    return {
        "sym":       sym,
        "name":      cfg["name"],
        "emoji":     cfg["emoji"],
        "direction": "LONG 📈" if is_buy else "SHORT 📉",
        "is_buy":    is_buy,
        "price":     round(price, d),
        "sl":        round(sl,    d),
        "tp1":       round(tp1,   d),
        "tp2":       round(tp2,   d),
        "tp3":       round(tp3,   d),
        "rr1":       rr1,
        "rr2":       rr2,
        "rr3":       rr3,
        "atr":       round(atr,   d),
        "trend":     trend_global,
        "fvg":       fvg,
        "ote_lo":    round(fib["ote_lo"], d),
        "ote_hi":    round(fib["ote_hi"], d),
    }
 
# ════════════════════════════════════════════════════════════
# ENVOI TELEGRAM
# ════════════════════════════════════════════════════════════
 
async def send_signal(bot, signal):
    s   = state[signal["sym"]]
    now = datetime.now().strftime("%H:%M")
    sym = signal["sym"]
    d   = SYMBOLS[sym]["decimals"]
 
    msg  = f"🚀 <b>SIGNAL {signal['name']} {signal['emoji']} — {now}</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"{signal['direction']} | OTE ✅ | FVG ✅\n\n"
    msg += f"💰 <b>Entrée</b>   : ${signal['price']:,.{d}f}\n"
    msg += f"🛑 <b>Stop Loss</b>: ${signal['sl']:,.{d}f}\n\n"
    msg += f"🎯 <b>TP1</b> : ${signal['tp1']:,.{d}f} (R:R {signal['rr1']})\n"
    msg += f"🎯 <b>TP2</b> : ${signal['tp2']:,.{d}f} (R:R {signal['rr2']})\n"
    msg += f"🎯 <b>TP3</b> : ${signal['tp3']:,.{d}f} (R:R {signal['rr3']})\n\n"
    msg += f"📊 Tendance : {signal['trend']}\n"
    msg += f"📐 Zone OTE : ${signal['ote_lo']:,.{d}f} — ${signal['ote_hi']:,.{d}f}\n"
    msg += f"⚡ ATR H1   : ${signal['atr']:,.{d}f}\n\n"
    msg += f"⚠️ <i>Signal indicatif — gérez votre risque</i>"
 
    await bot.send_message(chat_id=TELEGRAM_CHAT, text=msg, parse_mode="HTML")
 
    s["last_fvg_top"] = signal["fvg"]["top"]
    s["last_fvg_bot"] = signal["fvg"]["bot"]
    s["trades_today"] += 1
    logging.info(f"✅ Signal {signal['name']} envoyé : {signal['direction']} @ {signal['price']}")
 
async def send_daily_report(bot):
    now = datetime.now().strftime("%H:%M")
    msg  = f"📊 <b>RAPPORT JOURNALIER — {now}</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n"
    for sym, cfg in SYMBOLS.items():
        s = state[sym]
        msg += f"{cfg['emoji']} {cfg['name']} : {s['trades_today']}/{MAX_TRADES_DAY} signaux\n"
    msg += f"📅 {datetime.now().strftime('%d/%m/%Y')}"
    await bot.send_message(chat_id=TELEGRAM_CHAT, text=msg, parse_mode="HTML")
 
# ════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ════════════════════════════════════════════════════════════
 
async def main_loop():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    await app.initialize()
    bot = app.bot
 
    await bot.send_message(
        chat_id=TELEGRAM_CHAT,
        text="🤖 <b>nonosignaltele démarré</b>\nSurveillance GOLD 🥇 + BTC ₿ en cours...\nSignaux basés sur OTE + FVG + Tendance MTF",
        parse_mode="HTML"
    )
    logging.info("Bot nonosignaltele démarré — GOLD + BTC")
 
    last_report_day = None
 
    while True:
        try:
            now = datetime.now()
 
            if now.hour == 21 and now.minute < 5 and last_report_day != now.date():
                last_report_day = now.date()
                await send_daily_report(bot)
 
            for sym in SYMBOLS:
                s = state[sym]
                logging.info(f"Analyse {sym}... {now.strftime('%H:%M')}")
                signal = await analyze_symbol(sym)
 
                if signal:
                    if s["last_signal_dir"] == signal["direction"] and s["last_signal_time"]:
                        elapsed = (now - s["last_signal_time"]).total_seconds()
                        if elapsed < 1800:
                            logging.info(f"{sym}: signal doublon ignoré ({int(elapsed/60)} min)")
                            continue
 
                    await send_signal(bot, signal)
                    s["last_signal_time"] = now
                    s["last_signal_dir"]  = signal["direction"]
                else:
                    logging.info(f"{sym}: pas de signal")
 
        except Exception as e:
            logging.error(f"Erreur boucle: {e}")
 
        await asyncio.sleep(300)
 
if __name__ == "__main__":
    print("nonosignaltele — Signal Bot GOLD + BTC")
    print("Même stratégie que nonolescenaristeGoe")
    print("Canal : nonosignaltele")
    asyncio.run(main_loop())
