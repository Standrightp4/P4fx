import requests, pandas as pd, time, websocket, json, os, logging
from datetime import datetime, timezone
from sklearn.ensemble import RandomForestClassifier

# ================= CONFIG =================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DERIV_TOKEN = os.getenv("DERIV_TOKEN")

SYMBOLS = ["R_10","R_25","R_50","R_75","R_100","BOOM1000","CRASH1000"]

MIN_CONFIDENCE = 75
COOLDOWN = 900

# Risk controls
MAX_TRADES_PER_DAY = 10
MAX_CONSECUTIVE_LOSSES = 3
DAILY_LOSS_LIMIT = 10

trade_count = 0
consecutive_losses = 0
daily_loss = 0
last_reset_day = None

signal_history = []
last_signal = {}
trade_history = []

model = RandomForestClassifier()

logging.basicConfig(level=logging.INFO)

# ================= TELEGRAM =================
def send(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception as e:
        logging.error(f"Telegram error: {e}")

# ================= SESSION =================
def session_ok():
    h = datetime.now(timezone.utc).hour
    return 7 <= h <= 21

# ================= WEBSOCKET =================
def ws_connect():
    ws = websocket.create_connection("wss://ws.derivws.com/websockets/v3?app_id=1089")
    ws.settimeout(5)
    ws.send(json.dumps({"authorize": DERIV_TOKEN}))
    ws.recv()
    return ws

# ================= DATA =================
def get_data(ws, symbol, g):
    try:
        ws.send(json.dumps({
            "ticks_history": symbol,
            "count": 100,
            "granularity": g,
            "style": "candles"
        }))
        res = json.loads(ws.recv())

        if "candles" not in res:
            return None

        df = pd.DataFrame(res['candles'])
        df[['open','close','high','low']] = df[['open','close','high','low']].astype(float)

        return df
    except Exception as e:
        logging.error(f"Data error: {e}")
        return None

# ================= INDICATORS =================
def indicators(df):
    df['ema'] = df['close'].ewm(span=10).mean()
    df['sma'] = df['close'].rolling(10).mean()
    df['std'] = df['close'].rolling(20).std()
    df['atr'] = df['high'].rolling(14).max() - df['low'].rolling(14).min()
    return df

# ================= SMART MONEY =================
def bos(df):
    if df['close'].iloc[-1] > df['high'].iloc[-10:-2].max():
        return "UP"
    if df['close'].iloc[-1] < df['low'].iloc[-10:-2].min():
        return "DOWN"
    return None

def sweep(df):
    if df['high'].iloc[-1] > df['high'].iloc[-5:-1].max():
        return True
    if df['low'].iloc[-1] < df['low'].iloc[-5:-1].min():
        return True
    return False

# ================= AI =================
def features(df):
    return {
        "ema_slope": df['ema'].iloc[-1] - df['ema'].iloc[-2],
        "sma_slope": df['sma'].iloc[-1] - df['sma'].iloc[-2],
        "atr": df['atr'].iloc[-1],
        "vol": df['std'].iloc[-1]
    }

def ai_pass(df):
    if len(trade_history) < 30:
        return True

    try:
        X = pd.DataFrame([features(df)])
        prob = model.predict_proba(X)[0][1]
        return prob > 0.65
    except Exception:
        return True  # fallback if model not trained

# ================= RISK =================
def reset_daily():
    global trade_count, consecutive_losses, daily_loss, last_reset_day

    today = datetime.now(timezone.utc).date()  # ✅ FIXED

    if last_reset_day != today:
        trade_count = 0
        consecutive_losses = 0
        daily_loss = 0
        last_reset_day = today

def risk_ok():
    return (
        trade_count < MAX_TRADES_PER_DAY and
        consecutive_losses < MAX_CONSECUTIVE_LOSSES and
        daily_loss < DAILY_LOSS_LIMIT
    )

# ================= TRADE =================
def trade(ws, symbol, direction):
    contract = "CALL" if direction == "BUY" else "PUT"

    ws.send(json.dumps({
        "proposal": 1,
        "amount": 1,
        "basis": "stake",
        "contract_type": contract,
        "currency": "USD",
        "duration": 5,
        "duration_unit": "m",
        "symbol": symbol
    }))
    res = json.loads(ws.recv())

    ws.send(json.dumps({"buy": res["proposal"]["id"], "price": 1}))
    return json.loads(ws.recv())

# ================= STRATEGY =================
def strategy(ws, symbol):
    global trade_count

    if not session_ok():
        return

    if not risk_ok():
        return

    now = time.time()

    if now - last_signal.get(symbol, 0) < COOLDOWN:
        return

    df = get_data(ws, symbol, 900)
    if df is None or df.empty:
        return

    df = indicators(df)

    if df['atr'].iloc[-1] < df['atr'].rolling(20).mean().iloc[-1]:
        return

    direction = bos(df)
    if not direction:
        return

    if not sweep(df):
        return

    if not ai_pass(df):
        return

    entry = df['close'].iloc[-1]
    sl = df['low'].iloc[-3] if direction == "UP" else df['high'].iloc[-3]

    if abs(entry - sl) == 0:
        return

    send(f"{direction} {symbol} @ {entry}")

    trade(ws, symbol, "BUY" if direction == "UP" else "SELL")

    trade_count += 1
    last_signal[symbol] = now

# ================= MAIN =================
def main():
    while True:
        try:
            reset_daily()
            ws = ws_connect()

            for s in SYMBOLS:
                strategy(ws, s)
                time.sleep(1)

            ws.close()
            time.sleep(60)

        except Exception as e:
            send(f"⚠️ ERROR: {e}")
            time.sleep(10)

# ================= RUN =================
if __name__ == "__main__":
    main()
