import requests, pandas as pd, time, websocket, json, os, logging
from datetime import datetime, timezone
from sklearn.ensemble import RandomForestClassifier

# ================= CONFIG =================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DERIV_TOKEN = os.getenv("DERIV_TOKEN")

COOLDOWN = 900
MIN_DATA = 50

# Risk controls
MAX_TRADES_PER_DAY = 15
MAX_CONSECUTIVE_LOSSES = 3
DAILY_LOSS_LIMIT = 15

trade_count = 0
consecutive_losses = 0
daily_loss = 0
last_reset_day = None

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
    return 6 <= h <= 22

# ================= WEBSOCKET =================
def ws_connect():
    ws = websocket.create_connection("wss://ws.derivws.com/websockets/v3?app_id=1089")
    ws.settimeout(5)
    ws.send(json.dumps({"authorize": DERIV_TOKEN}))
    ws.recv()
    return ws

# ================= GET SYMBOLS =================
def get_symbols(ws):
    ws.send(json.dumps({"active_symbols": "full"}))
    res = json.loads(ws.recv())

    symbols = []

    for s in res["active_symbols"]:
        if s["exchange_is_open"] and s["is_trading_suspended"] == 0:
            symbols.append(s["symbol"])

    return symbols

# ================= DATA =================
def get_data(ws, symbol):
    try:
        ws.send(json.dumps({
            "ticks_history": symbol,
            "count": 100,
            "granularity": 900,
            "style": "candles"
        }))
        res = json.loads(ws.recv())

        if "candles" not in res:
            return None

        df = pd.DataFrame(res['candles'])
        df[['open','close','high','low']] = df[['open','close','high','low']].astype(float)

        if len(df) < MIN_DATA:
            return None

        return df
    except:
        return None

# ================= INDICATORS =================
def indicators(df):
    df['ema'] = df['close'].ewm(span=10).mean()
    df['sma'] = df['close'].rolling(10).mean()
    df['std'] = df['close'].rolling(20).std()
    df['atr'] = df['high'].rolling(14).max() - df['low'].rolling(14).min()
    return df

# ================= MARKET TYPE =================
def market_type(symbol):
    if "BOOM" in symbol or "CRASH" in symbol:
        return "BOOMCRASH"
    elif symbol.startswith("R_"):
        return "SYNTH"
    else:
        return "FOREX"

# ================= SMART MONEY =================
def bos(df):
    if df['close'].iloc[-1] > df['high'].iloc[-10:-2].max():
        return "BUY"
    if df['close'].iloc[-1] < df['low'].iloc[-10:-2].min():
        return "SELL"
    return None

def sweep(df):
    return (
        df['high'].iloc[-1] > df['high'].iloc[-5:-1].max() or
        df['low'].iloc[-1] < df['low'].iloc[-5:-1].min()
    )

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
        return prob > 0.6
    except:
        return True

# ================= RISK =================
def reset_daily():
    global trade_count, consecutive_losses, daily_loss, last_reset_day

    today = datetime.now(timezone.utc).date()

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
        "proposal":1,
        "amount":1,
        "basis":"stake",
        "contract_type":contract,
        "currency":"USD",
        "duration":5,
        "duration_unit":"m",
        "symbol":symbol
    }))
    res = json.loads(ws.recv())

    ws.send(json.dumps({"buy":res["proposal"]["id"],"price":1}))
    return json.loads(ws.recv())

# ================= STRATEGY =================
def strategy(ws, symbol):
    global trade_count

    if not session_ok() or not risk_ok():
        return

    now = time.time()
    if now - last_signal.get(symbol, 0) < COOLDOWN:
        return

    df = get_data(ws, symbol)
    if df is None:
        return

    df = indicators(df)

    # volatility filter (adaptive)
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

    send(f"{direction} {symbol} @ {entry}")

    trade(ws, symbol, direction)

    trade_count += 1
    last_signal[symbol] = now

# ================= MAIN =================
def main():
    while True:
        try:
            reset_daily()
            ws = ws_connect()

            symbols = get_symbols(ws)
            logging.info(f"Scanning {len(symbols)} symbols...")

            for s in symbols:
                strategy(ws, s)
                time.sleep(0.5)

            ws.close()
            time.sleep(60)

        except Exception as e:
            send(f"⚠️ ERROR: {e}")
            time.sleep(10)

# ================= RUN =================
if __name__ == "__main__":
    main()
