import requests
import pandas as pd
import time
import websocket
import json
import os
from flask import Flask
from threading import Thread

# ======================
# TELEGRAM
# ======================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_signal(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})

# ======================
# KEEP ALIVE
# ======================
app = Flask('')

@app.route('/')
def home():
    return "Bot running"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    Thread(target=run_web).start()

keep_alive()

# ======================
# SETTINGS
# ======================
signal_history = []
MAX_SIGNALS_PER_HOUR = 3
MIN_CONFIDENCE = 70

BOOM_CRASH = ["BOOM1000","BOOM500","CRASH1000","CRASH500"]
VOLATILITY = ["R_10","R_25","R_50","R_75","R_100","JD10","JD25","JD50","JD75","JD100","STEPINDEX"]

# ======================
# DATA FETCH
# ======================
def get_market_data(symbol, granularity):
    try:
        ws = websocket.create_connection("wss://ws.derivws.com/websockets/v3?app_id=1089")
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
        if 'candles' not in res:
            return None
        df = pd.DataFrame(res['candles'])
        df[['open','close','high','low']] = df[['open','close','high','low']].astype(float)
        return df
    except Exception as e:
        print(f"Data error {symbol}: {e}")
        return None

# ======================
# INDICATORS & STRUCTURE
# ======================
def calculate_indicators(df):
    df['ema'] = df['close'].ewm(span=10).mean()
    df['sma'] = df['close'].rolling(10).mean()
    return df

def get_slope(series):
    return series.iloc[-1] - series.iloc[-2]

def get_structure(df):
    if df['high'].iloc[-1] > df['high'].iloc[-2] and df['low'].iloc[-1] > df['low'].iloc[-2]:
        return "UP"
    elif df['high'].iloc[-1] < df['high'].iloc[-2] and df['low'].iloc[-1] < df['low'].iloc[-2]:
        return "DOWN"
    return "RANGE"

def trendline_break(df):
    rh = df['high'].iloc[-5:-1].max()
    rl = df['low'].iloc[-5:-1].min()
    lc = df['close'].iloc[-1]
    if lc > rh: return "BREAK_UP"
    elif lc < rl: return "BREAK_DOWN"
    return "NONE"

def confirmation_candle(df):
    last = df.iloc[-1]
    body = abs(last['close'] - last['open'])
    rng = last['high'] - last['low']
    if body > rng * 0.6:
        return "BUY" if last['close'] > last['open'] else "SELL"
    return "NONE"

def is_fakeout(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    return ((prev['close'] > prev['open'] and last['close'] < last['open']) or
            (prev['close'] < prev['open'] and last['close'] > last['open']))

# ======================
# SIGNAL MANAGEMENT
# ======================
def can_send():
    global signal_history
    now = time.time()
    signal_history = [t for t in signal_history if now - t < 3600]
    return len(signal_history) < MAX_SIGNALS_PER_HOUR

last_signal = {}

# ======================
# BFA SCALPING FOR BOOM/CRASH
# ======================
def bfa_strategy(symbol):
    global last_signal
    m1 = get_market_data(symbol, 60)
    m5 = get_market_data(symbol, 300)
    if any(x is None or x.empty for x in [m1, m5]):
        return
    df = calculate_indicators(m1)
    last = df.iloc[-1]
    ema_slope = get_slope(df['ema'])
    confirm = confirmation_candle(df)
    breakout = trendline_break(df)
    fake = is_fakeout(df)
    direction = "UP" if ema_slope > 0 else "DOWN"
    confidence = 0
    confidence += 20 if (ema_slope>0 and direction=="UP") or (ema_slope<0 and direction=="DOWN") else 0
    confidence += 15 if "BREAK" in breakout else 0
    confidence += 15 if confirm in ["BUY","SELL"] else 0
    confidence -= 20 if fake else 0
    confidence = max(0, min(confidence, 100))
    if confidence >= MIN_CONFIDENCE and can_send():
        if last_signal.get(symbol) != direction:
            signal_history.append(time.time())
            send_signal(f"{'BUY' if direction=='UP' else 'SELL'} ({symbol})\nEntry: {last['close']}\nConfidence: {confidence}%")
            last_signal[symbol] = direction

# ======================
# MULTI-TIMEFRAME STRATEGY FOR OTHER INDICES
# ======================
def strategy(symbol):
    global last_signal
    m1 = get_market_data(symbol, 60)
    m5 = get_market_data(symbol, 300)
    m15 = get_market_data(symbol, 900)
    if any(x is None or x.empty for x in [m1,m5,m15]):
        return
    t1, t5, t15 = get_structure(m1), get_structure(m5), get_structure(m15)
    if t1 == t5 == t15 and t15 != "RANGE":
        direction = t15
        df = calculate_indicators(m15)
        last = df.iloc[-1]
        ema_s = get_slope(df['ema'])
        breakout = trendline_break(df)
        confirm = confirmation_candle(df)
        fake = is_fakeout(df)
        confidence = 0
        confidence += 20
        confidence += 15 if (ema_s>0 and direction=="UP") or (ema_s<0 and direction=="DOWN") else 0
        confidence += 15 if "BREAK" in breakout else 0
        confidence += 15 if confirm in ["BUY","SELL"] else 0
        confidence -= 20 if fake else 0
        confidence = max(0, min(confidence,100))
        if confidence >= MIN_CONFIDENCE and can_send():
            if last_signal.get(symbol) != direction:
                signal_history.append(time.time())
                send_signal(f"{'BUY' if direction=='UP' else 'SELL'} ({symbol})\nEntry: {last['close']}\nConfidence: {confidence}%")
                last_signal[symbol] = direction

# ======================
# MAIN LOOP
# ======================
while True:
    try:
        for sym in BOOM_CRASH:
            bfa_strategy(sym)
            time.sleep(1)

        for sym in VOLATILITY:
            strategy(sym)
            time.sleep(2)

        print("Market scan complete...")
        time.sleep(15)

    except Exception as e:
        print("Error:", e)
        time.sleep(5)
