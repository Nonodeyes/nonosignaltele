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
FVG_MIN_PIPS   = 0.5     # En $ pour XAUUSD
TP1_RR         = 1.0
TP2_RR         = 2.0
TP3_RR         = 3.0
MAX_TRADES_DAY = 5

# Mémoire anti-doublon
last_signal_time  = None
last_signal_dir   = None
trades_today      = 0
last_trade_day    = None
last_fvg_top      = 0
last_fvg_bot      = 0

# ════════════════════════════════════════════════════════════
# RÉCUPÉRATION DES DONNÉES XAUUSD (Yahoo Finance)
# ════════════════════════════════════════════════════════════

async def get_candles(interval="5m", period="5d"):
    """Récupère les bougies XAUUSD depuis Yahoo Finance"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval={interval}&range={period}"
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

                # Nettoyer les None
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
        logging.error(f"Erreur candles: {e}")
        return []

async def get_current_price():
    """Prix actuel XAUUSD"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1m&range=1d",
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
    """Calcule EMA"""
    if len(closes) < period:
        return closes[-1] if closes else 0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def get_trend(candles, tf_count=50):
    """Calcule la tendance via EMA20/EMA50 — identique au robot MT5"""
    if len(candles) < tf_count:
        return "NONE"

    closes = [c["close"] for c in candles[-tf_count:]]
    highs  = [c["high"]  for c in candles[-tf_count:]]
    lows   = [c["low"]   for c in candles[-tf_count:]]

    ema_fast = calc_ema(closes, EMA_FAST)
    ema_slow = calc_ema(closes, EMA_SLOW)

    # Structure HH/HL ou LH/LL
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
# FIBONACCI OTE — IDENTIQUE AU ROBOT MT5
# ════════════════════════════════════════════════════════════

def calc_fibonacci(candles_h1):
    """Calcule la zone OTE sur H1 — même logique que le robot"""
    if len(candles_h1) < FIB_LOOKBACK:
        return None

    recent = candles_h1[-FIB_LOOKBACK:]
    sw_high = max(c["high"] for c in recent)
    sw_low  = min(c["low"]  for c in recent)

    bar_h = max(range(len(recent)), key=lambda i: recent[i]["high"])
    bar_l = min(range(len(recent)), key=lambda i: recent[i]["low"])

    if sw_high <= sw_low:
        return None

    bullish = bar_l > bar_h  # creux plus récent = retracement haussier
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
# FVG — IDENTIQUE AU ROBOT MT5
# ════════════════════════════════════════════════════════════

def find_fvg(candles_m5, trend):
    """Trouve le FVG sur M5 — même logique que le robot"""
    global last_fvg_top, last_fvg_bot

    lookback = min(20, len(candles_m5) - 2)

    for i in range(2, lookback):
        # FVG HAUSSIER
        bot = candles_m5[-i-1]["high"]
        top = candles_m5[-i+1]["low"]
        if top > bot:
            size = top - bot
            if size >= FVG_MIN_PIPS and trend == "BULL":
                # Anti-doublon
                if abs(top - last_fvg_top) < 0.1 and abs(bot - last_fvg_bot) < 0.1:
                    continue
                return {"top": top, "bot": bot, "bullish": True}

        # FVG BAISSIER
        top2 = candles_m5[-i-1]["low"]
        bot2 = candles_m5[-i+1]["high"]
        if top2 > bot2:
            size = top2 - bot2
            if size >= FVG_MIN_PIPS and trend == "BEAR":
                if abs(top2 - last_fvg_top) < 0.1 and abs(bot2 - last_fvg_bot) < 0.1:
                    continue
                return {"top": top2, "bot": bot2, "bullish": False}

    return None

# ════════════════════════════════════════════════════════════
# CALCUL ATR
# ════════════════════════════════════════════════════════════

def calc_atr(candles, period=14):
    """Calcule l'ATR"""
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
# ANALYSE COMPLÈTE — MÊME LOGIQUE QUE LE ROBOT MT5
# ════════════════════════════════════════════════════════════

async def analyze():
    """Analyse complète identique au robot MT5"""
    global trades_today, last_trade_day, last_fvg_top, last_fvg_bot

    # Reset journalier
    today = datetime.now().date()
    if last_trade_day != today:
        trades_today  = 0
        last_trade_day = today

    if trades_today >= MAX_TRADES_DAY:
        logging.info("Limite 5 trades/jour atteinte")
        return None

    # Récupérer les données
    candles_h4 = await get_candles("1h",  "30d")  # Approximation H4
    candles_h1 = await get_candles("1h",  "10d")
    candles_m5 = await get_candles("5m",  "5d")

    if not candles_h1 or not candles_m5:
        return None

    price = await get_current_price()
    if not price:
        return None

    # 1. TENDANCE MTF
    trend_major  = get_trend(candles_h4, 100)
    trend_medium = get_trend(candles_h1, 50)
    trend_minor  = get_trend(candles_m5, 30)

    # Alignement souple (identique au robot)
    if   trend_major  != "NONE": trend_global = trend_major
    elif trend_medium != "NONE": trend_global = trend_medium
    else:                        trend_global = trend_minor

    if trend_global == "NONE":
        logging.info("Pas de tendance globale")
        return None

    # 2. FIBONACCI OTE
    fib = calc_fibonacci(candles_h1)
    if not fib:
        logging.info("Fibonacci non calculé")
        return None

    in_ote = fib["ote_lo"] <= price <= fib["ote_hi"]
    if not in_ote:
        logging.info(f"Prix hors OTE [{fib['ote_lo']:.2f} - {fib['ote_hi']:.2f}]")
        return None

    # 3. FVG
    fvg = find_fvg(candles_m5, trend_global)
    if not fvg:
        logging.info("Pas de FVG valide")
        return None

    # Vérifier alignement FVG/tendance
    if trend_global == "BULL" and not fvg["bullish"]:
        logging.info("FVG non aligné avec tendance BULL")
        return None
    if trend_global == "BEAR" and fvg["bullish"]:
        logging.info("FVG non aligné avec tendance BEAR")
        return None

    # 4. CALCUL SL/TP (identique au robot MT5)
    atr   = calc_atr(candles_h1, 14)
    sl_buf = atr * 0.5

    is_buy = (trend_global == "BULL")

    if is_buy:
        sl = fvg["bot"] - sl_buf
    else:
        sl = fvg["top"] + sl_buf

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

    return {
        "direction": "LONG 📈" if is_buy else "SHORT 📉",
        "is_buy":    is_buy,
        "price":     round(price, 2),
        "sl":        round(sl,    2),
        "tp1":       round(tp1,   2),
        "tp2":       round(tp2,   2),
        "tp3":       round(tp3,   2),
        "rr1":       rr1,
        "rr2":       rr2,
        "rr3":       rr3,
        "atr":       round(atr,   2),
        "trend":     trend_global,
        "fvg":       fvg,
        "ote_lo":    round(fib["ote_lo"], 2),
        "ote_hi":    round(fib["ote_hi"], 2),
    }

# ════════════════════════════════════════════════════════════
# ENVOI TELEGRAM
# ════════════════════════════════════════════════════════════

async def send_signal(bot, signal):
    """Envoie le signal sur nonosignaltele"""
    global trades_today, last_fvg_top, last_fvg_bot

    now = datetime.now().strftime("%H:%M")

    msg  = f"🚀 <b>SIGNAL XAUUSD — {now}</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"{signal['direction']} | OTE ✅ | FVG ✅\n\n"
    msg += f"💰 <b>Entrée</b>   : ${signal['price']:.2f}\n"
    msg += f"🛑 <b>Stop Loss</b>: ${signal['sl']:.2f}\n\n"
    msg += f"🎯 <b>TP1</b> : ${signal['tp1']:.2f} (R:R {signal['rr1']})\n"
    msg += f"🎯 <b>TP2</b> : ${signal['tp2']:.2f} (R:R {signal['rr2']})\n"
    msg += f"🎯 <b>TP3</b> : ${signal['tp3']:.2f} (R:R {signal['rr3']})\n\n"
    msg += f"📊 Tendance : {signal['trend']}\n"
    msg += f"📐 Zone OTE : ${signal['ote_lo']} — ${signal['ote_hi']}\n"
    msg += f"⚡ ATR H1   : ${signal['atr']:.2f}\n\n"
    msg += f"⚠️ <i>Signal indicatif — gérez votre risque</i>"

    await bot.send_message(
        chat_id=TELEGRAM_CHAT,
        text=msg,
        parse_mode="HTML"
    )

    # Mémoriser le FVG utilisé
    last_fvg_top = signal["fvg"]["top"]
    last_fvg_bot = signal["fvg"]["bot"]
    trades_today += 1

    logging.info(f"✅ Signal envoyé : {signal['direction']} @ {signal['price']}")

async def send_daily_report(bot):
    """Rapport journalier à 21h00"""
    now = datetime.now().strftime("%H:%M")
    msg  = f"📊 <b>RAPPORT JOURNALIER — {now}</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🔢 Signaux envoyés : {trades_today}/{MAX_TRADES_DAY}\n"
    msg += f"📅 {datetime.now().strftime('%d/%m/%Y')}"
    await bot.send_message(chat_id=TELEGRAM_CHAT, text=msg, parse_mode="HTML")

# ════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ════════════════════════════════════════════════════════════

async def main_loop():
    """Boucle principale — scan toutes les 5 minutes"""
    global last_signal_time, last_signal_dir

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    await app.initialize()
    bot = app.bot

    # Message de démarrage
    await bot.send_message(
        chat_id=TELEGRAM_CHAT,
        text="🤖 <b>nonosignaltele démarré</b>\nSurveillance XAUUSD en cours...\nSignaux basés sur OTE + FVG + Tendance MTF",
        parse_mode="HTML"
    )
    logging.info("Bot nonosignaltele démarré")

    last_report_day = None

    while True:
        try:
            now = datetime.now()

            # Rapport journalier à 21h00
            if now.hour == 21 and now.minute < 5 and last_report_day != now.date():
                last_report_day = now.date()
                await send_daily_report(bot)

            # Analyse toutes les 5 minutes
            logging.info(f"Analyse en cours... {now.strftime('%H:%M')}")
            signal = await analyze()

            if signal:
                # Anti-doublon : pas 2 signaux identiques en moins de 30 min
                if last_signal_dir == signal["direction"] and last_signal_time:
                    elapsed = (now - last_signal_time).total_seconds()
                    if elapsed < 1800:
                        logging.info(f"Signal doublon ignoré ({int(elapsed/60)} min)")
                        await asyncio.sleep(300)
                        continue

                await send_signal(bot, signal)
                last_signal_time = now
                last_signal_dir  = signal["direction"]
            else:
                logging.info("Pas de signal — conditions non remplies")

        except Exception as e:
            logging.error(f"Erreur boucle: {e}")

        # Attendre 5 minutes (même fréquence que M5 du robot MT5)
        await asyncio.sleep(300)

if __name__ == "__main__":
    print("nonosignaltele — Signal Bot XAUUSD")
    print("Même stratégie que nonolescenaristeGoe")
    print("Canal : nonosignaltele")
    asyncio.run(main_loop())
