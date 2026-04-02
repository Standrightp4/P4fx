import requests
import pandas as pd
import time
import websocket
import json
import os
from flask import Flask
from threading import Thread

# ======================
# 🔐 TELEGRAM CONFIG
# ======================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_signal(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": message})

# ======================
# 🌐 KEEP ALIVE (RAILWAY SAFE)
# ======================
app = Flask('')

@app.route('/')
def home():
    return "Bot running"

def run_web():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run_web).start()

# ======================
# 📊 DERIV DATA
# ======================
def get_market_data(symbol, granularity):
    ws = websocket.create_connection("wss://ws.derivws.com/websockets/v3")

    req = {
        "ticks_history": symbol,
        "adjust_start_time": 1,
        "count": 100,
        "end": "latest",
        "granularity": granularity,
        "style": "candles"
    }

    ws.send(json.dumps(req))
    res = json.loads(ws.recv())
    ws.close()

    df = pd.DataFrame(res['candles'])
    df['open'] = df['open'].astype(float)
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)

    return df

# ======================
# 📈 INDICATORS (BFA)
# ======================
def calculate_indicators(df):
    df['ema'] = df['close'].ewm(span=10).mean()
    df['sma'] = df['close'].rolling(10).mean()

    df['mbb'] = df['close'].rolling(20).mean()
    df['std'] = df['close'].rolling(20).std()
    df['upper'] = df['mbb'] + (2 * df['std'])
    df['lower'] = df['mbb'] - (2 * df['std'])

    return df

def get_slope(series):
    return series.iloc[-1] - series.iloc[-2]

# ======================
# 📉 PRICE ACTION
# ======================
def get_structure(df):
    highs = df['high']
    lows = df['low']

    if highs.iloc[-1] > highs.iloc[-2] and lows.iloc[-1] > lows.iloc[-2]:
        return "UP"
    elif highs.iloc[-1] < highs.iloc[-2] and lows.iloc[-1] < lows.iloc[-2]:
        return "DOWN"
    return "RANGE"

def trendline_break(df):
    recent_high = df['high'].iloc[-5:-1].max()
    recent_low = df['low'].iloc[-5:-1].min()

    last_close = df['close'].iloc[-1]

    if last_close > recent_high:
        return "BREAK_UP"
    elif last_close < recent_low:
        return "BREAK_DOWN"
    return "NONE"

def retest_zone(df):
    recent_high = df['high'].iloc[-5:-1].max()
    recent_low = df['low'].iloc[-5:-1].min()
    last = df.iloc[-1]

    if abs(last['close'] - recent_high) < 0.2:
        return "RETEST_HIGH"
    elif abs(last['close'] - recent_low) < 0.2:
        return "RETEST_LOW"
    return "NONE"

def confirmation_candle(df):
    last = df.iloc[-1]
    body = abs(last['close'] - last['open'])
    range_ = last['high'] - last['low']

    if body > range_ * 0.6:
        if last['close'] > last['open']:
            return "BUY"
        elif last['close'] < last['open']:
            return "SELL"
    return "NONE"

def is_fakeout(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    return (
        (prev['close'] > prev['open'] and last['close'] < last['open']) or
        (prev['close'] < prev['open'] and last['close'] > last['open'])
    )

def liquidity_sweep(df):
    prev_high = df['high'].iloc[-3]
    prev_low = df['low'].iloc[-3]
    last = df.iloc[-1]

    if last['high'] > prev_high and last['close'] < prev_high:
        return "SWEEP_HIGH"
    elif last['low'] < prev_low and last['close'] > prev_low:
        return "SWEEP_LOW"
    return "NONE"

# ======================
# 🎯 STRATEGY
# ======================
last_signal = None

def strategy(symbol):
    global last_signal

    # TIMEFRAMES
    m1 = get_market_data(symbol, 60)
    m5 = get_market_data(symbol, 300)
    m15 = get_market_data(symbol, 900)

    h1 = get_market_data(symbol, 3600)
    h4 = get_market_data(symbol, 14400)
    d1 = get_market_data(symbol, 86400)

    # STRUCTURE
    trend_m1 = get_structure(m1)
    trend_m5 = get_structure(m5)
    trend_m15 = get_structure(m15)

    trend_h1 = get_structure(h1)
    trend_h4 = get_structure(h4)
    trend_d1 = get_structure(d1)

    if trend_m1 == trend_m5 == trend_m15 and trend_m15 != "RANGE":

        direction = trend_m15
        htf_confirm = [trend_h1, trend_h4, trend_d1].count(direction)

        if htf_confirm >= 1:

            df = calculate_indicators(m15)

            last = df.iloc[-1]

            ema_slope = get_slope(df['ema'])
            sma_slope = get_slope(df['sma'])
            mbb_slope = get_slope(df['mbb'])

            breakout = trendline_break(df)
            retest = retest_zone(df)
            confirm = confirmation_candle(df)
            fake = is_fakeout(df)
            sweep = liquidity_sweep(df)

            # BUY
            if (
                direction == "UP" and
                last['ema'] > last['sma'] and
                last['ema'] > last['mbb'] and
                ema_slope > 0 and sma_slope > 0 and mbb_slope > 0 and
                breakout == "BREAK_UP" and
                retest == "RETEST_HIGH" and
                confirm == "BUY" and
                not fake
            ):
                entry = last['close']
                sl = df['low'].iloc[-3]
                tp = df['upper'].iloc[-1]

                if last_signal != "BUY":
                    send_signal(f"BUY\nEntry:{entry}\nSL:{sl}\nTP:{tp}")
                    last_signal = "BUY"

            # SELL
            elif (
                direction == "DOWN" and
                last['ema'] < last['sma'] and
                last['ema'] < last['mbb'] and
                ema_slope < 0 and sma_slope <