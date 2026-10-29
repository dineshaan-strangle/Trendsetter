import os, ccxt, time, pandas as pd, ta, requests, threading
from datetime import datetime
from flask import Flask, request

DEFAULT_LEVERAGE = 100
DEFAULT_QTY_USD = float(os.getenv("DEFAULT_QTY", "5"))
TIMEFRAME = "15m"
SL_PERCENT = 0.8

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID","").strip()
DELTA_API_KEY = os.getenv("DELTA_API_KEY","").strip().replace('"','').replace("'","").strip()
DELTA_SECRET = os.getenv("DELTA_SECRET","").strip().replace('"','').replace("'","").strip()
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL","https://trendsetter-vkdr.onrender.com")

# FIX 1: BOT_TOKEN alias
BOT_TOKEN = TELEGRAM_BOT_TOKEN

print(f"V29 FIX - KeyLen {len(DELTA_API_KEY)} SecLen {len(DELTA_SECRET)} TokenLen {len(BOT_TOKEN)}")

# India host - Correct!
exchange = ccxt.delta({
    'apiKey': DELTA_API_KEY,
    'secret': DELTA_SECRET,
    'enableRateLimit': True,
    'hostname': 'api.india.delta.exchange',
    'options': {'defaultType': 'future','defaultSettle': 'USDT'}
})

try:
    exchange.load_markets()
    print(f"✅ Markets loaded: {len(exchange.markets)}")
except Exception as e:
    print(f"Load markets error: {e}")

SYMBOL_CCXT = "BTC/USDT:USDT"
SYMBOL_PRODUCT = "BTCUSDT"

app = Flask(__name__)
current_trade = None
qty_usd_global = DEFAULT_QTY_USD
cached_df=None
last_price=80000.0

def send_alert(msg):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e: print(f"Alert err {e}")

def send_reply(chat_id, msg):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=10)
        print(f"Reply sent to {chat_id}")
    except Exception as e: print(f"Reply err {e}")

def fetch_data():
    global cached_df, last_price
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL_CCXT, TIMEFRAME, limit=200)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        df = df.dropna()
        cached_df = df
        last_price = float(df['close'].iloc[-1])
        return df
    except Exception as e:
        print(f"Fetch error: {e}")
        return cached_df

def get_sr(df):
    return {'curr': float(df['close'].iloc[-1]), 'res': float(df['high'].tail(96).max()), 'sup': float(df['low'].tail(96).min())}

def check_trend(df):
    try:
        adx_ind = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], 14, fillna=True)
        adx = float(adx_ind.adx().iloc[-1])
        plus = float(adx_ind.adx_pos().iloc[-1]); minus = float(adx_ind.adx_neg().iloc[-1])
        ema9 = float(ta.trend.EMAIndicator(df['close'],9, fillna=True).ema_indicator().iloc[-1])
        ema21 = float(ta.trend.EMAIndicator(df['close'],21, fillna=True).ema_indicator().iloc[-1])
        if ema9>ema21 and plus>minus and adx>25: return "UP", adx
        if ema9<ema21 and minus>plus and adx>25: return "DOWN", adx
        return "NO TREND", adx
    except: return "NO TREND", 0

def get_price():
    try: return float(exchange.fetch_ticker(SYMBOL_CCXT)['last'])
    except: return float(last_price)

def place_order(side):
    global current_trade
    try:
        price = get_price()
        lots = max(1, int((qty_usd_global * DEFAULT_LEVERAGE) / price * 1000))
        print(f"Placing {side} {SYMBOL_CCXT} lots {lots} price {price}")
        order = exchange.create_order(SYMBOL_CCXT, 'market', side, lots)
        print(f"Order success: {order}")
        current_trade = {'side': side, 'entry': price, 'position': lots, 'sl': price*(0.992 if side=='buy' else 1.008), 'time': datetime.now()}
        send_alert(f"🔥 *{side.upper()} {SYMBOL_CCXT}*\nEntry: ${price:.2f}\nLots: {lots}")
        return True
    except Exception as e:
        print(f"❌ Order Error: {e}")
        send_alert(f"❌ Order Error: {e}")
        return False

def check_loop():
    global current_trade
    while True:
        try:
            df = fetch_data()
            if df is None: time.sleep(30); continue
            sr = get_sr(df); trend, adx = check_trend(df)
            print(f"✅ LIVE ${sr['curr']:.2f} {trend} ADX {adx:.1f}", flush=True)
            if not current_trade:
                if trend=="DOWN" and sr['curr'] > sr['sup']*1.002:
                    place_order('sell')
                elif trend=="UP" and sr['curr'] < sr['res']*0.998:
                    place_order('buy')
            time.sleep(60)
        except Exception as e: print(f"Loop err {e}"); time.sleep(30)

@app.route('/')
def home(): return f"V29 FIXED {SYMBOL_CCXT} ${last_price} Trade:{current_trade}"

# FIX 2: Use BOT_TOKEN which is now defined!
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        print(f"MSG RECEIVED: {data}", flush=True)
        if 'message' in data:
            chat_id = data['message']['chat']['id']
            text = data['message'].get('text','').lower()
            
            if '/lev' in text:
                    try:
                        global DEFAULT_LEVERAGE
                        DEFAULT_LEVERAGE = int(data['message']['text'].split()[1])
                        send_reply(chat_id, f"✅ Leverage set {DEFAULT_LEVERAGE}x\nQty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x")
                    except: send_reply(chat_id, "Usage: /lev 20")
                elif '/qty' in text:
                    try:
                        global qty_usd_global
                        qty_usd_global = float(data['message']['text'].split()[1])
                        send_reply(chat_id, f"Qty set ${qty_usd_global}")
                    except: send_reply(chat_id, "Usage /qty 5")
            elif '/close' in text:
                global current_trade
                if current_trade:
                    side='sell' if current_trade['side']=='buy' else 'buy'
                    try: exchange.create_order(SYMBOL_CCXT,'market',side,current_trade['position'])
                    except: pass
                    current_trade=None; send_reply(chat_id, "Closed ✅")
                else: send_reply(chat_id, "No trade")
            elif '/sr' in text:
                df=fetch_data()
                if df is not None:
                    sr=get_sr(df); trend,adx=check_trend(df)
                    msg=f"📊 *{SYMBOL_CCXT}*\nPrice: ${sr['curr']:.2f}\nRes: ${sr['res']:.2f}\nSup: ${sr['sup']:.2f}\nTrend: {trend} ADX {adx:.1f}\nTrade: {current_trade['side'] if current_trade else 'None'}"
                else:
                    msg=f"📊 *{SYMBOL_CCXT}*\nPrice: ${last_price:.2f}\nLoading..."
                send_reply(chat_id, msg)
            else: # /start and others
                send_reply(chat_id, f"📈 *V29 WORKING FIX*\nSym: {SYMBOL_CCXT}\nPrice: ${last_price:.2f}\nQty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x\nTrade: {current_trade['side'] if current_trade else 'None'}\n\n/sr /qty /close")
    except Exception as e:
        print(f"WEBHOOK ERROR: {e}", flush=True)
    return 'ok', 200

if __name__ == '__main__':
    threading.Thread(target=check_loop, daemon=True).start()
    # Set webhook automatically
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={RENDER_URL}/{BOT_TOKEN}"
        r = requests.get(url, timeout=10)
        print(f"Webhook set: {r.text}")
    except Exception as e: print(f"Webhook set err {e}")
    app.run(host='0.0.0.0', port=10000)
