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
def send(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg}
        )
    except:
        pass

# ================= SESSION =================
def session_ok():
    h = datetime.now(timezone.utc).hour
    return 6 <= h <= 22

# ================= WS =================
def ws_connect():
    while True:
        try:
            ws = websocket.create_connection("wss://ws.derivws.com/websockets/v3?app_id=1089")
            ws.settimeout(5)
            ws.send(json.dumps({"authorize": DERIV_TOKEN}))
            ws.recv()
            logging.info("Connected to Deriv")
            return ws
        except:
            time.sleep(5)

# ================= SYMBOLS =================
def get_symbols(ws):
    try:
        ws.send(json.dumps({"active_symbols": "full"}))
        res = json.loads(ws.recv())
        return [
            s["symbol"] for s in res["active_symbols"]
            if s["exchange_is_open"] and s["is_trading_suspended"] == 0
        ]
    except:
        return []

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
    try:
        X = pd.DataFrame([features(df)])
        prob = model.predict_proba(X)[0][1]
        return prob > 0.6
    except:
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

def risk_ok():
    return (
        trade_count < MAX_TRADES_PER_DAY and
        consecutive_losses < MAX_CONSECUTIVE_LOSSES and
        daily_loss < DAILY_LOSS_LIMIT
    )

# ================= TRADE =================
def trade(ws, symbol, direction):
    try:
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

        proposal_id = res["proposal"]["id"]

        ws.send(json.dumps({"buy":proposal_id,"price":1}))
        buy_res = json.loads(ws.recv())

        contract_id = buy_res["buy"]["contract_id"]
        open_contracts[contract_id] = symbol

        return contract_id

    except Exception as e:
        logging.error(f"Trade error: {e}")
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

            if poc["is_sold"]:
                profit = poc["profit"]

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

        except:
            continue

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

    send(f"{direction} {symbol} @ {entry}")

    contract_id = trade(ws, symbol, direction)

    if contract_id:
        trade_count += 1
        last_signal[symbol] = now

# ================= MAIN =================
def main():
    while True:
        try:
            logging.info("Bot running...")

            reset_daily()
            ws = ws_connect()

            symbols = get_symbols(ws)

            for s in symbols:
                strategy(ws, s)
                check_results(ws)
                time.sleep(0.5)

            ws.close()
            time.sleep(60)

        except Exception as e:
            logging.error(e)
            send(f"⚠️ ERROR: {e}")
            time.sleep(10)

# ================= RUN =================
if __name__ == "__main__":
    main()
