import os, ccxt, time, pandas as pd, ta
from datetime import datetime
import telebot, threading
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

print(f"V28 FIX - KeyLen {len(DELTA_API_KEY)} SecLen {len(DELTA_SECRET)}")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)

# ===== SAME AS YOUR WORKING BOTS =====
exchange = ccxt.delta({
    'apiKey': DELTA_API_KEY,
    'secret': DELTA_SECRET,
    'enableRateLimit': True,
    'hostname': 'api.india.delta.exchange',  # <-- ITHU THAN FIX! India host!
    'options': {
        'defaultType': 'future',
        'defaultSettle': 'USDT'
    }
})
try:
    exchange.load_markets()
    print(f"✅ Markets loaded: {len(exchange.markets)}")
except Exception as e:
    print(f"Load markets error: {e}")

SYMBOL_CCXT = "BTC/USDT:USDT"  # Correct for Delta Perps
SYMBOL_PRODUCT = "BTCUSDT"
SYMBOL = SYMBOL_CCXT

app = Flask(__name__)
current_trade = None
qty_usd_global = DEFAULT_QTY_USD
wins=0; losses=0; total_pnl_usd=0
cached_df=None
last_price=80000.0

def send_alert(msg):
    try: bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode='Markdown')
    except: print(msg)

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
        # Delta lot size = qty * leverage / price. For BTCUSDT 1 lot = 0.001 BTC approx
        # Use 1 lot minimum for $5 x 100x
        lots = max(1, int((qty_usd_global * DEFAULT_LEVERAGE) / price * 1000))
        print(f"Placing {side} {SYMBOL_CCXT} lots {lots} price {price}")
        # This is same as your working bots
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
            print(f"✅ LIVE ${sr['curr']:.2f} {trend} ADX {adx:.1f}")
            if not current_trade:
                if trend=="DOWN" and sr['curr'] > sr['sup']*1.002:
                    place_order('sell')
                elif trend=="UP" and sr['curr'] < sr['res']*0.998:
                    place_order('buy')
            time.sleep(60)
        except Exception as e: print(f"Loop err {e}"); time.sleep(30)

@bot.message_handler(commands=['start','sr','pnl','qty','close'])
def handle(m):
    global qty_usd_global
    try:
        t=m.text.lower()
        if '/qty' in t:
            try: qty_usd_global=float(m.text.split()[1]); bot.reply_to(m,f"Qty ${qty_usd_global}")
            except: bot.reply_to(m,f"Usage /qty 5")
        elif '/close' in t:
            if current_trade:
                side='sell' if current_trade['side']=='buy' else 'buy'
                exchange.create_order(SYMBOL_CCXT,'market',side,current_trade['position'])
                current_trade=None; bot.reply_to(m,"Closed")
            else: bot.reply_to(m,"No trade")
        elif '/sr' in t:
            df=fetch_data(); sr=get_sr(df) if df is not None else {'curr':last_price,'res':0,'sup':0}; trend,adx=check_trend(df) if df is not None else ("LOADING",0)
            bot.reply_to(m,f"📊 *{SYMBOL_CCXT}*\nPrice: ${sr['curr']:.2f}\nRes: ${sr['res']:.2f}\nSup: ${sr['sup']:.2f}\nTrend: {trend} ADX {adx:.1f}\nTrade: {current_trade['side'] if current_trade else 'None'}",parse_mode='Markdown')
        else:
            bot.reply_to(m,f"📈 *V28 WORKING FIX*\nSym: {SYMBOL_CCXT}\nPrice: ${last_price:.2f}\nQty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x\nTrade: {current_trade['side'] if current_trade else 'None'}\n\n/sr /pnl /qty /close",parse_mode='Markdown')
    except Exception as e: print(e)

@app.route('/')
def home(): return f"V28 {SYMBOL_CCXT} ${last_price} Trade:{current_trade}"

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        print(f"MSG: {data}", flush=True)
        if 'message' in data and 'text' in data['message']:
            chat_id = data['message']['chat']['id']
            text = data['message']['text']
            # TEST REPLY - etha adichalum reply varum
            reply = f"✅ Dinesh Bot LIVE!\nNee anupinathu: {text}\nPrice: {get_price()}"
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": reply}, timeout=15)
    except Exception as e:
        print(f"WEBHOOK ERROR: {e}", flush=True)
        try:
            # Error vanthalum ithu pakka anupum
            if 'message' in data:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": data['message']['chat']['id'], "text": f"BOT LIVE but error: {e}"}, timeout=10)
        except: pass
    return 'ok', 200
