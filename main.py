import requests, pandas as pd, time, websocket, json, os, logging
from datetime import datetime, timezone
from sklearn.ensemble import RandomForestClassifier

# ================= CONFIG =================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DERIV_TOKEN = os.getenv("DERIV_TOKEN")

COOLDOWN = 900
MIN_DATA = 50

MAX_TRADES_PER_DAY = 15
MAX_CONSECUTIVE_LOSSES = 3
DAILY_LOSS_LIMIT = 15

trade_count = 0
wins = 0
losses = 0
consecutive_losses = 0
daily_loss = 0
last_reset_day = None

last_signal = {}
open_contracts = {}

model = RandomForestClassifier()

logging.basicConfig(level=logging.INFO)

# ================= TELEGRAM =================
def test_telegram():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("❌ Telegram TOKEN or CHAT_ID not set!")
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": "✅ Telegram test message"}
        )
        if res.status_code != 200:
            raise RuntimeError(f"❌ Telegram failed: {res.status_code} {res.text}")
        logging.info("✅ Telegram test message sent successfully")
    except Exception as e:
        raise RuntimeError(f"❌ Telegram test failed: {e}")

def send(msg):
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
        if res.status_code != 200:
            logging.error(f"Telegram send failed: {res.status_code} {res.text}")
        else:
            logging.info(f"Telegram message sent: {msg}")
    except Exception as e:
        logging.error(f"Exception sending Telegram message: {e}")

# ================= SESSION =================
def session_ok():
    h = datetime.now(timezone.utc).hour
    return 6 <= h <= 22

# ================= WS =================
def ws_connect():
    while True:
        try:
            ws = websocket.create_connection("wss://ws.derivws.com/websockets/v3?app_id=1089")
            ws.settimeout(10)
            ws.send(json.dumps({"authorize": DERIV_TOKEN}))
            ws.recv()
            logging.info("Connected to Deriv")
            send("🤖 Bot started and connected to Deriv")
            return ws
        except Exception as e:
            logging.error(f"WS connect failed: {e}")
            time.sleep(5)

# ================= SYMBOLS =================
def get_symbols(ws):
    try:
        ws.send(json.dumps({"active_symbols": "full"}))
        res = json.loads(ws.recv())
        return [
            s["symbol"] for s in res.get("active_symbols", [])
            if s.get("exchange_is_open", True) and s.get("is_trading_suspended", 0) == 0
        ]
    except Exception as e:
        logging.error(f"Failed to fetch symbols: {e}")
        return []

# ================= DATA =================
def get_data(ws, symbol):
    try:
        ws.send(json.dumps({
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "end": "latest",
            "count": 100,
            "granularity": 60,
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
    except Exception as e:
        logging.error(f"Failed to get data for {symbol}: {e}")
        return None

# ================= INDICATORS =================
def indicators(df):
    df['ema'] = df['close'].ewm(span=10).mean()
    df['sma'] = df['close'].rolling(10).mean()
    df['std'] = df['close'].rolling(20).std()
    df['atr'] = df['high'].rolling(14).max() - df['low'].rolling(14).min()
    df['range'] = df['high'] - df['low']
    return df

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

# ================= SPIKE =================
def spike(df):
    last = df['range'].iloc[-1]
    avg = df['range'].rolling(20).mean().iloc[-1]
    return last > avg * 2

# ================= AI =================
def features(df):
    return {
        "ema_slope": df['ema'].iloc[-1] - df['ema'].iloc[-2],
        "sma_slope": df['sma'].iloc[-1] - df['sma'].iloc[-2],
        "atr": df['atr'].iloc[-1],
        "vol": df['std'].iloc[-1]
    }

def ai_pass(df):
    if not hasattr(model, "classes_"):
        return True
    try:
        X = pd.DataFrame([features(df)])
        prob = model.predict_proba(X)[0][1]
        return prob > 0.6
    except Exception as e:
        logging.error(f"AI pass error: {e}")
        return True

# ================= RISK =================
def reset_daily():
    global trade_count, wins, losses, consecutive_losses, daily_loss, last_reset_day
    today = datetime.now(timezone.utc).date()
    if last_reset_day != today:
        trade_count = 0
        wins = 0
        losses = 0
        consecutive_losses = 0
        daily_loss = 0
        last_reset_day = today
        send("🔄 Daily reset completed")

def risk_ok():
    return (
        trade_count < MAX_TRADES_PER_DAY and
        consecutive_losses < MAX_CONSECUTIVE_LOSSES and
        daily_loss < DAILY_LOSS_LIMIT
    )

# ================= TRADE =================
def trade(ws, symbol, direction):
    try:
        contract = "CALL" if direction=="BUY" else "PUT"
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
        proposal_id = res["proposal"]["id"]
        ws.send(json.dumps({"buy":proposal_id,"price":1}))
        buy_res = json.loads(ws.recv())
        contract_id = buy_res["buy"]["contract_id"]
        open_contracts[contract_id] = symbol
        send(f"🟢 Trade executed: {direction} {symbol} (Contract {contract_id})")
        return contract_id
    except Exception as e:
        logging.error(f"Trade error: {e}")
        send(f"⚠️ Trade error for {symbol}: {e}")
        return None

# ================= RESULT TRACKING =================
def check_results(ws):
    global wins, losses, consecutive_losses, daily_loss
    for contract_id in list(open_contracts.keys()):
        try:
            ws.send(json.dumps({
                "proposal_open_contract": 1,
                "contract_id": contract_id
            }))
            res = json.loads(ws.recv())
            if "proposal_open_contract" not in res:
                continue
            poc = res["proposal_open_contract"]
            if poc.get("is_sold", False):
                profit = float(poc.get("profit", 0))
                if profit > 0:
                    wins += 1
                    consecutive_losses = 0
                else:
                    losses += 1
                    consecutive_losses += 1
                    daily_loss += abs(profit)
                del open_contracts[contract_id]
                total = wins + losses
                winrate = (wins / total * 100) if total > 0 else 0
                send(f"📊 WR: {winrate:.1f}% | W:{wins} L:{losses}")
        except Exception as e:
            logging.error(f"Check result error: {e}")

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
    if df['atr'].iloc[-1] < df['atr'].rolling(20).mean().iloc[-1]:
        return
    direction = bos(df)
    if not direction:
        return
    if not sweep(df):
        return
    if not spike(df):
        return
    if not ai_pass(df):
        return
    entry = df['close'].iloc[-1]
    send(f"📈 Signal: {direction} {symbol} @ {entry}")
    contract_id = trade(ws, symbol, direction)
    if contract_id:
        trade_count += 1
        last_signal[symbol] = now

# ================= MAIN =================
def main():
    try:
        test_telegram()  # Validate Telegram before starting
    except RuntimeError as e:
        logging.error(e)
        return

    ws = ws_connect()
    symbols = get_symbols(ws)
    send(f"Tracking {len(symbols)} symbols...")

    while True:
        try:
            reset_daily()
            for s in symbols:
                strategy(ws, s)
                check_results(ws)
                time.sleep(0.5)
        except websocket.WebSocketConnectionClosedException:
            logging.warning("WebSocket disconnected. Reconnecting...")
            ws = ws_connect()
            symbols = get_symbols(ws)
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            send(f"⚠️ ERROR: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
