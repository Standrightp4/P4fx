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
# GLOBAL SETTINGS
# ======================
signal_history = []
MAX_SIGNALS_PER_HOUR = 3
MIN_CONFIDENCE = 70

SYMBOLS = [
    "R_10","R_25","R_50","R_75","R_100",
    "BOOM1000","BOOM500","CRASH1000","CRASH500",
    "JD10","JD25","JD50","JD75","JD100",
    "STEPINDEX"
]

# ======================
# SAFE DATA FETCH
# ======================
def get_market_data(symbol, granularity):
    try:
        ws = websocket.create_connection(
            "wss://ws.derivws.com/websockets/v3?app_id=1089"
        )

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
            print(f"No candles for {symbol}")
            return None

        df = pd.DataFrame(res['candles'])
        df[['open','close','high','low']] = df[['open','close','high','low']].astype(float)

        return df

    except Exception as e:
        print(f"Data error for {symbol}: {e}")
        return None

# ======================
# INDICATORS
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
# PRICE ACTION
# ======================
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

def retest_zone(df):
    rh = df['high'].iloc[-5:-1].max()
    rl = df['low'].iloc[-5:-1].min()
    lc = df['close'].iloc[-1]

    if abs(lc - rh) < 0.2: return "RETEST_HIGH"
    elif abs(lc - rl) < 0.2: return "RETEST_LOW"
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
# CONFIDENCE
# ======================
def get_confidence(direction, ema_slope, sma_slope, mbb_slope, breakout, retest, confirm, fake, htf):
    score = 0
    if direction != "RANGE": score += 20
    if (ema_slope > 0 and sma_slope > 0) or (ema_slope < 0 and sma_slope < 0):
        score += 15
    if "BREAK" in breakout: score += 15
    if "RETEST" in retest: score += 15
    if confirm in ["BUY","SELL"]: score += 15
    score += htf * 5
    if fake: score -= 20
    return max(0, min(score, 100))

def can_send():
    global signal_history
    now = time.time()
    signal_history = [t for t in signal_history if now - t < 3600]
    return len(signal_history) < MAX_SIGNALS_PER_HOUR

# ======================
# STRATEGY
# ======================
last_signal = {}

def strategy(symbol):
    global last_signal

    m1 = get_market_data(symbol, 60)
    m5 = get_market_data(symbol, 300)
    m15 = get_market_data(symbol, 900)

    h1 = get_market_data(symbol, 3600)
    h4 = get_market_data(symbol, 14400)
    d1 = get_market_data(symbol, 86400)

    # skip bad data
    if None in [m1, m5, m15, h1, h4, d1]:
        return

    t1, t5, t15 = get_structure(m1), get_structure(m5), get_structure(m15)
    htf = [get_structure(h1), get_structure(h4), get_structure(d1)]

    if t1 == t5 == t15 and t15 != "RANGE":
        direction = t15
        htf_confirm = htf.count(direction)

        if htf_confirm >= 1:
            df = calculate_indicators(m15)
            last = df.iloc[-1]

            ema_s, sma_s, mbb_s = get_slope(df['ema']), get_slope(df['sma']), get_slope(df['mbb'])

            breakout = trendline_break(df)
            retest = retest_zone(df)
            confirm = confirmation_candle(df)
            fake = is_fakeout(df)

            confidence = get_confidence(direction, ema_s, sma_s, mbb_s, breakout, retest, confirm, fake, htf_confirm)

            entry = last['close']
            sl = df['low'].iloc[-3] if direction=="UP" else df['high'].iloc[-3]
            tp = df['upper'].iloc[-1] if direction=="UP" else df['lower'].iloc[-1]
            rr = round(abs(tp-entry)/abs(entry-sl),2)

            if confidence >= MIN_CONFIDENCE and can_send():
                if last_signal.get(symbol) != direction:
                    signal_history.append(time.time())

                    send_signal(f"""{'BUY' if direction=='UP' else 'SELL'} ({symbol})

Entry: {entry}
SL: {sl}
TP: {tp}
RR: {rr}
Confidence: {confidence}%

LTF: {t1}/{t5}/{t15}
HTF confirm: {htf_confirm}
Breakout: {breakout}
Retest: {retest}
""")

                    last_signal[symbol] = direction

# ======================
# LOOP
# ======================
while True:
    try:
        for sym in SYMBOLS:
            strategy(sym)
            time.sleep(2)

        print("Scanning market...")
        time.sleep(60)

    except Exception as e:
        print("Error:", e)
        time.sleep(30)
