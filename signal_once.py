"""
Bir marta ishlaydigan versiya (GitHub Actions uchun).
Har ishga tushganda barcha juftliklarni tekshiradi, yangi signal bo'lsa
Telegram'ga yuboradi va to'xtaydi. signal_bot.py bilan bir papkada turishi kerak.
"""

import os
import json
from datetime import timedelta

import pandas as pd
import signal_bot as sb

STATE_FILE = "state.json"


def interval_minutes() -> int:
    s = sb.INTERVAL
    return int(s[:-1]) * (60 if s.endswith("h") else 1)


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def to_utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def main() -> None:
    if os.getenv("SEND_TEST", "").lower() == "true":
        ok = sb.send_telegram("✅ Test: signal bot bulutda ishlayapti.")
        sb.log.info("Test xabar yuborildi: %s", ok)

    state = load_state()
    step = interval_minutes()
    now = pd.Timestamp.now(tz="UTC")

    for name, ticker in sb.SYMBOLS.items():
        df = sb.fetch(ticker)
        if df is None:
            continue

        candle_time = df.index[-2]
        key = to_utc(candle_time).isoformat()
        entry = state.get(name, {})

        # Bu shamni allaqachon tahlil qilganmiz
        if entry.get("candle") == key:
            continue
        entry["candle"] = key
        state[name] = entry

        # Eskirgan shamni (masalan, dam olish kunidan keyin) o'tkazib yuboramiz
        age = now - to_utc(candle_time)
        if age > timedelta(minutes=step * 4):
            sb.log.info("%s: sham eskirgan, o'tkazildi", name)
            continue

        try:
            sig = sb.analyze(df)
        except Exception as e:
            sb.log.warning("Tahlil xatosi (%s): %s", name, e)
            continue

        if sig is None:
            sb.log.info("%s: signal yo'q", name)
            continue

        # Cooldown (vaqt bo'yicha)
        last = entry.get("last_signal")
        if last:
            since = to_utc(candle_time) - pd.Timestamp(last)
            if since < timedelta(minutes=step * sb.COOLDOWN_CANDLES):
                sb.log.info("%s: cooldown", name)
                continue

        decimals = 2 if "XAU" in name or "JPY" in name else 5
        if sb.send_telegram(sb.format_signal(name, sig, decimals)):
            entry["last_signal"] = key
            sb.log.info("%s: %s signal yuborildi", name, sig["direction"])

    save_state(state)


if __name__ == "__main__":
    main()
