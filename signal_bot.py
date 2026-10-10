"""
SIGNAL BOT - bozorni tahlil qilib Telegram'ga signal yuboradi.
Order OCHMAYDI. Faqat signal beradi, qaror sizniki.

Strategiya (ko'p tasdiqli):
  - Trend: EMA20 va EMA50 (yo'nalish) + EMA200 (katta trend filtri)
  - Kirish: RSI(14) momentum tasdig'i
  - MACD tasdig'i
  - Stop-loss / Take-profit: ATR(14) asosida (R:R = 1:2)

Ishga tushirish:
  pip install -r requirements.txt
  .env faylini to'ldiring (TELEGRAM_TOKEN, CHAT_ID)
  python signal_bot.py
"""

import os
import time
import logging
from datetime import datetime, timezone

import requests
import pandas as pd
import yfinance as yf

# ---------------- SOZLAMALAR ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# Yahoo Finance tikerlari
SYMBOLS = {
    "XAUUSD (Oltin)": "GC=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
}

INTERVAL = "15m"        # sham davri: 5m, 15m, 30m, 1h
PERIOD = "30d"          # tarix uzunligi
CHECK_EVERY_SEC = 120   # har necha sekundda tekshirish
ATR_SL_MULT = 1.5       # stop-loss = 1.5 * ATR
RR = 2.0                # take-profit = 2 * risk
MIN_SCORE = 3           # signal uchun kamida nechta tasdiq (max 4)
COOLDOWN_CANDLES = 8    # bir juftlikda signal orasidagi minimal shamlar soni

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("signal_bot")


# ---------------- INDIKATORLAR ----------------
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-12)
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["Close"].shift()
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def macd(close: pd.Series):
    line = ema(close, 12) - ema(close, 26)
    signal = ema(line, 9)
    return line, signal


# ---------------- MA'LUMOT ----------------
def fetch(ticker: str) -> pd.DataFrame | None:
    try:
        df = yf.download(
            ticker,
            period=PERIOD,
            interval=INTERVAL,
            progress=False,
            auto_adjust=True,
        )
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        return df if len(df) > 220 else None
    except Exception as e:
        log.warning("Ma'lumot olishda xato (%s): %s", ticker, e)
        return None


# ---------------- TAHLIL ----------------
def analyze(df: pd.DataFrame):
    """
    Oxirgi YOPILGAN sham bo'yicha signal qaytaradi.
    (Oxirgi qator hali shakllanayotgan bo'lishi mumkin, shuning uchun -2 ishlatiladi.)
    """
    close = df["Close"]
    df = df.copy()
    df["ema20"] = ema(close, 20)
    df["ema50"] = ema(close, 50)
    df["ema200"] = ema(close, 200)
    df["rsi"] = rsi(close)
    df["atr"] = atr(df)
    df["macd"], df["macd_sig"] = macd(close)

    cur = df.iloc[-2]
    prev = df.iloc[-3]
    candle_time = df.index[-2]

    buy_score = 0
    sell_score = 0
    reasons_buy, reasons_sell = [], []

    # 1) EMA20/EMA50 kesishuvi yoki yo'nalishi
    if cur.ema20 > cur.ema50:
        buy_score += 1
        reasons_buy.append("EMA20 > EMA50")
    else:
        sell_score += 1
        reasons_sell.append("EMA20 < EMA50")

    # 2) Katta trend filtri (EMA200)
    if cur.Close > cur.ema200:
        buy_score += 1
        reasons_buy.append("Narx EMA200 dan yuqori")
    else:
        sell_score += 1
        reasons_sell.append("Narx EMA200 dan past")

    # 3) RSI momentum
    if 50 < cur.rsi < 68:
        buy_score += 1
        reasons_buy.append(f"RSI {cur.rsi:.0f} (o'sish momentumi)")
    elif 32 < cur.rsi < 50:
        sell_score += 1
        reasons_sell.append(f"RSI {cur.rsi:.0f} (pasayish momentumi)")

    # 4) MACD
    if cur.macd > cur.macd_sig:
        buy_score += 1
        reasons_buy.append("MACD signal chizig'idan yuqori")
    else:
        sell_score += 1
        reasons_sell.append("MACD signal chizig'idan past")

    # Yangi kesishuv bo'lsa kuchliroq (signal "yangi" bo'lishi uchun)
    bull_cross = prev.ema20 <= prev.ema50 and cur.ema20 > cur.ema50
    bear_cross = prev.ema20 >= prev.ema50 and cur.ema20 < cur.ema50
    macd_bull_cross = prev.macd <= prev.macd_sig and cur.macd > cur.macd_sig
    macd_bear_cross = prev.macd >= prev.macd_sig and cur.macd < cur.macd_sig
    fresh_buy = bull_cross or macd_bull_cross
    fresh_sell = bear_cross or macd_bear_cross

    direction = None
    score = 0
    reasons = []
    if buy_score >= MIN_SCORE and fresh_buy and buy_score > sell_score:
        direction, score, reasons = "BUY", buy_score, reasons_buy
    elif sell_score >= MIN_SCORE and fresh_sell and sell_score > buy_score:
        direction, score, reasons = "SELL", sell_score, reasons_sell

    if direction is None:
        return None

    price = float(cur.Close)
    risk = float(cur.atr) * ATR_SL_MULT
    if direction == "BUY":
        sl = price - risk
        tp = price + risk * RR
    else:
        sl = price + risk
        tp = price - risk * RR

    return {
        "direction": direction,
        "score": score,
        "price": price,
        "sl": sl,
        "tp": tp,
        "reasons": reasons,
        "time": candle_time,
    }


# ---------------- TELEGRAM ----------------
def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log.error("TELEGRAM_TOKEN yoki CHAT_ID kiritilmagan!")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if r.status_code != 200:
            log.error("Telegram xato: %s", r.text)
            return False
        return True
    except Exception as e:
        log.error("Telegram yuborishda xato: %s", e)
        return False


def format_signal(name: str, sig: dict, decimals: int) -> str:
    emoji = "🟢" if sig["direction"] == "BUY" else "🔴"
    reasons = "\n".join(f"• {r}" for r in sig["reasons"])
    return (
        f"{emoji} <b>{sig['direction']} signal: {name}</b>\n"
        f"Taymfreym: {INTERVAL}\n\n"
        f"Kirish: <code>{sig['price']:.{decimals}f}</code>\n"
        f"Stop-loss: <code>{sig['sl']:.{decimals}f}</code>\n"
        f"Take-profit: <code>{sig['tp']:.{decimals}f}</code>\n"
        f"Risk:Foyda = 1:{RR:.0f}\n"
        f"Kuch: {sig['score']}/4\n\n"
        f"<b>Sabablar:</b>\n{reasons}\n\n"
        f"⚠️ Bu moliyaviy maslahat emas. Har bir savdoda kapitalning "
        f"1-2% dan ko'p riskka qo'ymang."
    )


# ---------------- ASOSIY SIKL ----------------
def main():
    log.info("Bot ishga tushdi. Juftliklar: %s", ", ".join(SYMBOLS))
    send_telegram(
        "✅ Signal bot ishga tushdi.\n"
        f"Kuzatilmoqda: {', '.join(SYMBOLS)}\nTaymfreym: {INTERVAL}"
    )

    last_signal_time: dict[str, pd.Timestamp] = {}
    last_candle_seen: dict[str, pd.Timestamp] = {}

    while True:
        for name, ticker in SYMBOLS.items():
            df = fetch(ticker)
            if df is None:
                continue

            candle_time = df.index[-2]
            # Bir shamni ikki marta tahlil qilmaymiz
            if last_candle_seen.get(name) == candle_time:
                continue
            last_candle_seen[name] = candle_time

            try:
                sig = analyze(df)
            except Exception as e:
                log.warning("Tahlil xatosi (%s): %s", name, e)
                continue

            if sig is None:
                log.info("%s: signal yo'q", name)
                continue

            # Cooldown: ketma-ket takroriy signallarni oldini olish
            prev_t = last_signal_time.get(name)
            if prev_t is not None:
                gap = (df.index.get_loc(candle_time)
                       - df.index.get_loc(prev_t)
                       if prev_t in df.index else COOLDOWN_CANDLES)
                if gap < COOLDOWN_CANDLES:
                    log.info("%s: cooldown, o'tkazib yuborildi", name)
                    continue

            decimals = 2 if "XAU" in name or "JPY" in name else 5
            if send_telegram(format_signal(name, sig, decimals)):
                last_signal_time[name] = candle_time
                log.info("%s: %s signal yuborildi", name, sig["direction"])

        time.sleep(CHECK_EVERY_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot to'xtatildi.")
