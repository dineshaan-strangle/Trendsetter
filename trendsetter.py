import os, ccxt, time, pandas as pd, ta, requests, threading, re
from datetime import datetime
from flask import Flask, request

DEFAULT_LEVERAGE = 20
DEFAULT_QTY_USD = float(os.getenv("DEFAULT_QTY", "10"))
TIMEFRAME = "15m"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID","").strip()
DELTA_API_KEY = os.getenv("DELTA_API_KEY","").strip().replace('"','').replace("'","").strip()
DELTA_SECRET = os.getenv("DELTA_SECRET","").strip().replace('"','').replace("'","").strip()
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL","https://trendsetter-vkdr.onrender.com")

BOT_TOKEN = TELEGRAM_BOT_TOKEN

print(f"V31 MANUAL ASK")

exchange = ccxt.delta({
    'apiKey': DELTA_API_KEY,
    'secret': DELTA_SECRET,
    'enableRateLimit': True,
    'hostname': 'api.india.delta.exchange',
    'options': {'defaultType': 'future','defaultSettle': 'USDT'}
})

try:
    exchange.load_markets()
    print(f"✅ Markets: {len(exchange.markets)}")
except Exception as e:
    print(f"Load markets error: {e}")

SYMBOL_CCXT = "BTC/USDT:USDT"
app = Flask(__name__)

current_trade = None
qty_usd_global = DEFAULT_QTY_USD
cached_df=None
last_price=80198.0
pending_signal = None  # NEW: Signal wait pannum

def send_reply(chat_id, msg):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e: print(f"Reply err {e}")

def send_alert(msg):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e: print(f"Alert err {e}")

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
        print(f"Placing {side} lots {lots} Lev {DEFAULT_LEVERAGE}x")
        order = exchange.create_order(SYMBOL_CCXT, 'market', side, lots)
        current_trade = {'side': side, 'entry': price, 'position': lots, 'time': datetime.now()}
        send_alert(f"🔥 *{side.upper()} EXECUTED {DEFAULT_LEVERAGE}x*\nEntry: ${price:.2f}\nLots: {lots}\nQty: ${qty_usd_global}")
        return True
    except Exception as e:
        send_alert(f"❌ Order Error: {e}")
        return False

def check_loop():
    global pending_signal
    while True:
        try:
            df = fetch_data()
            if df is None: time.sleep(30); continue
            sr = get_sr(df); trend, adx = check_trend(df)
            print(f"✅ LIVE ${sr['curr']:.2f} {trend} ADX {adx:.1f} Pending:{pending_signal}", flush=True)
            
            # NEW LOGIC: Auto trade panna maatan, unkitta ketkum!
            if not current_trade and not pending_signal:
                signal = None
                if trend=="DOWN" and sr['curr'] > sr['sup']*1.002:
                    signal = 'sell'
                elif trend=="UP" and sr['curr'] < sr['res']*0.998:
                    signal = 'buy'
                
                if signal:
                    pending_signal = signal
                    send_alert(f"⚠️ *SIGNAL FOUND!*\nTrend: {trend} ADX {adx:.1f}\nPrice: ${sr['curr']:.2f}\nSignal: *{signal.upper()} {DEFAULT_LEVERAGE}x*\nQty: ${qty_usd_global}\n\n👉 Trade panna /yes nu anuppu\n👉 Venda na /no")
            time.sleep(60)
        except Exception as e: print(f"Loop err {e}"); time.sleep(30)

@app.route('/')
def home(): return f"V31 MANUAL {SYMBOL_CCXT} ${last_price} Pending:{pending_signal} Lev:{DEFAULT_LEVERAGE}x"

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    global DEFAULT_LEVERAGE, qty_usd_global, current_trade, pending_signal
    try:
        data = request.get_json()
        print(f"MSG: {data}", flush=True)
        if 'message' in data:
            chat_id = data['message']['chat']['id']
            text = data['message'].get('text','').lower()
            raw_text = data['message'].get('text','')
            
            if '/lev' in text or 'leverage' in text:
                nums = re.findall(r'\d+', raw_text)
                if nums:
                    DEFAULT_LEVERAGE = int(nums[0])
                    send_reply(chat_id, f"✅ *Leverage {DEFAULT_LEVERAGE}x*\nQty: ${qty_usd_global}")
                else:
                    send_reply(chat_id, "Usage: /lev 20")
            
            elif '/qty' in text:
                nums = re.findall(r'\d+\.?\d*', raw_text)
                if nums:
                    qty_usd_global = float(nums[0])
                    send_reply(chat_id, f"✅ *Qty ${qty_usd_global} x {DEFAULT_LEVERAGE}x*")
                else:
                    send_reply(chat_id, "Usage /qty 10")
            
            elif '/yes' in text or text.strip() == 'yes':
                if pending_signal:
                    send_reply(chat_id, f"✅ Ok {pending_signal.upper()} order poduren...")
                    place_order(pending_signal)
                    pending_signal = None
                else:
                    send_reply(chat_id, "No pending signal da")
            
            elif '/no' in text:
                if pending_signal:
                    send_reply(chat_id, f"❌ {pending_signal.upper()} cancelled")
                    pending_signal = None
                else:
                    send_reply(chat_id, "No pending signal")
            
            elif '/buy' in text or '/sell' in text:
                side = 'buy' if '/buy' in text else 'sell'
                send_reply(chat_id, f"Manual {side.upper()} poduren {DEFAULT_LEVERAGE}x")
                place_order(side)
            
            elif '/close' in text:
                if current_trade:
                    side='sell' if current_trade['side']=='buy' else 'buy'
                    try: exchange.create_order(SYMBOL_CCXT,'market',side,current_trade['position'])
                    except: pass
                    current_trade=None; send_reply(chat_id, "✅ Closed")
                else: send_reply(chat_id, "No trade")
            
            elif '/sr' in text:
                df=fetch_data()
                if df is not None:
                    sr=get_sr(df); trend,adx=check_trend(df)
                    msg=f"📊 *{SYMBOL_CCXT}*\nPrice: ${sr['curr']:.2f}\nRes: ${sr['res']:.2f}\nSup: ${sr['sup']:.2f}\nTrend: {trend} ADX {adx:.1f}\nLev: {DEFAULT_LEVERAGE}x Qty: ${qty_usd_global}\nPending: {pending_signal}\nTrade: {current_trade['side'] if current_trade else 'None'}"
                else:
                    msg=f"📊 Price: ${last_price:.2f}\nLoading..."
                send_reply(chat_id, msg)
            
            else: # /start
                send_reply(chat_id, f"📈 *V31 MANUAL ASK*\nSym: {SYMBOL_CCXT}\nPrice: ${last_price:.2f}\nQty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x\nPending: {pending_signal}\nTrade: {current_trade['side'] if current_trade else 'None'}\n\n/sr - chart\n/lev 20 - leverage\n/qty 10 - qty\n/yes - confirm signal\n/no - reject\n/buy /sell - manual\n/close - close")
    except Exception as e:
        print(f"WEBHOOK ERROR: {e}", flush=True)
    return 'ok', 200

if __name__ == '__main__':
    threading.Thread(target=check_loop, daemon=True).start()
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={RENDER_URL}/{BOT_TOKEN}"
        requests.get(url, timeout=10)
    except: pass
    app.run(host='0.0.0.0', port=10000)
