import os, ccxt, time, pandas as pd, ta
from datetime import datetime
import telebot
import threading
from flask import Flask, request

DEFAULT_LEVERAGE = 100
DEFAULT_QTY_USD = float(os.getenv("DEFAULT_QTY", "5"))
SYMBOL_INPUT = os.getenv("SYMBOL", "BTCUSD")
TIMEFRAME = "15m"

SL_PERCENT = 0.8
TP1_PERCENT = 1.0
TP2_PERCENT = 3.0
TP3_PERCENT = 5.0
TRAILING_SL_PERCENT = 1.0
ADX_THRESHOLD_NORMAL = 25
ADX_THRESHOLD_BIG = 35
VOLUME_MULTIPLIER_BIG = 1.8
MAX_HOLD_HOURS = 72

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_SECRET = os.getenv("DELTA_SECRET")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://trendsetter-vkdr.onrender.com")

print(f"✅ V19 - INPUT SYMBOL {SYMBOL_INPUT} -> Will Auto-Find Correct Symbol")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)

# ===== FIXED DELTA INDIA - AUTO SYMBOL RESOLVE =====
exchange = ccxt.delta({
    'apiKey': DELTA_API_KEY,
    'secret': DELTA_SECRET,
    'enableRateLimit': True,
    'hostname': 'api.india.delta.exchange',
    'options': {'defaultType': 'future'}
})

# Auto find correct BTC symbol
def resolve_symbol():
    try:
        exchange.load_markets()
        print(f"Available symbols sample: {exchange.symbols[:10]}")
        # Try exact first
        if SYMBOL_INPUT in exchange.symbols:
            return SYMBOL_INPUT
        # Try common Delta formats
        for try_sym in [f"BTC/USD:BTC", f"BTC/USD", f"BTC/USDT", f"BTCUSD", f"BTC/USDT:USDT"]:
            if try_sym in exchange.symbols:
                return try_sym
        # Find any BTCUSD
        for s in exchange.symbols:
            if "BTC" in s and "USD" in s:
                print(f"Auto-picked: {s}")
                return s
        return "BTC/USD"
    except Exception as e:
        print(f"Resolve error: {e}")
        return "BTC/USD:BTC"

SYMBOL = resolve_symbol()
print(f"✅ FINAL SYMBOL USING: {SYMBOL}")

app = Flask(__name__)
current_trade = None
qty_usd_global = DEFAULT_QTY_USD
wins = 0
losses = 0
total_pnl_usd = 0

def send_alert(msg):
    try: bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode='Markdown')
    except: print(msg)

def fetch_data():
    # Use resolved symbol
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=200)
    return pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])

def get_support_resistance(df):
    last_96_high = df['high'].rolling(96).max().iloc[-2]
    last_96_low = df['low'].rolling(96).min().iloc[-2]
    recent_high = df['high'].tail(50).max()
    recent_low = df['low'].tail(50).min()
    curr = df['close'].iloc[-1]
    return {'curr': curr, 'strong_res': max(last_96_high, recent_high), 'strong_sup': min(last_96_low, recent_low)}

def check_trend(df):
    adx_ind = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], 14)
    adx = adx_ind.adx().iloc[-1]
    plus_di = adx_ind.adx_pos().iloc[-1]
    minus_di = adx_ind.adx_neg().iloc[-1]
    ema9 = ta.trend.EMAIndicator(df['close'], 9).ema_indicator()
    ema21 = ta.trend.EMAIndicator(df['close'], 21).ema_indicator()
    vol_avg = df['volume'].rolling(20).mean().iloc[-1]
    vol_ratio = df['volume'].iloc[-1]/vol_avg if vol_avg>0 else 1
    trend = "NO TREND"; trend_type = "NONE"
    if ema9.iloc[-1] > ema21.iloc[-1] and plus_di > minus_di and adx > ADX_THRESHOLD_NORMAL:
        trend = "UP"; trend_type = "BIG TREND" if adx > ADX_THRESHOLD_BIG and vol_ratio > VOLUME_MULTIPLIER_BIG else "NORMAL TREND"
    elif ema9.iloc[-1] < ema21.iloc[-1] and minus_di > plus_di and adx > ADX_THRESHOLD_NORMAL:
        trend = "DOWN"; trend_type = "BIG TREND" if adx > ADX_THRESHOLD_BIG and vol_ratio > VOLUME_MULTIPLIER_BIG else "NORMAL TREND"
    return trend, trend_type, adx, vol_ratio

def place_order(side, trend_type):
    global current_trade
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price = ticker['last']
        position_size = max(1, int((qty_usd_global * DEFAULT_LEVERAGE) / price * 1000))
        if side == 'buy':
            sl_price = price * (1 - SL_PERCENT/100)
            tp1 = price * (1 + TP1_PERCENT/100); tp2 = price * (1 + TP2_PERCENT/100); tp3 = price * (1 + TP3_PERCENT/100)
        else:
            sl_price = price * (1 + SL_PERCENT/100)
            tp1 = price * (1 - TP1_PERCENT/100); tp2 = price * (1 - TP2_PERCENT/100); tp3 = price * (1 - TP3_PERCENT/100)
        exchange.create_market_order(SYMBOL, side, position_size)
        current_trade = {'side': side, 'entry': price, 'qty_usd': qty_usd_global, 'position': position_size, 'sl': sl_price, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3, 'trend_type': trend_type, 'tp1_done': False, 'tp2_done': False, 'time': datetime.now()}
        send_alert(f"🔥 *{side.upper()} {SYMBOL}*\nType: {trend_type}\nEntry: ${price:.2f}\nQty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x\nSL: ${sl_price:.2f}\nTP: {tp1:.2f}/{tp2:.2f}/{tp3:.2f}")
        return True
    except Exception as e:
        send_alert(f"❌ Order Error: {e}"); print(e); return False

def close_trade(reason, price):
    global current_trade, wins, losses, total_pnl_usd
    try:
        if not current_trade: return
        side = 'sell' if current_trade['side']=='buy' else 'buy'
        exchange.create_market_order(SYMBOL, side, current_trade['position'])
        pnl_percent = ((price - current_trade['entry'])/current_trade['entry']*100) if current_trade['side']=='buy' else ((current_trade['entry']-price)/current_trade['entry']*100)
        pnl_usd = (pnl_percent/100 * qty_usd_global * DEFAULT_LEVERAGE)
        total_pnl_usd += pnl_usd
        if pnl_usd > 0: wins += 1
        else: losses += 1
        wr = (wins/(wins+losses)*100) if (wins+losses)>0 else 0
        send_alert(f"✅ *CLOSED: {reason}*\nPnL: {pnl_percent:.2f}% (${pnl_usd:.2f})\nWR: {wr:.1f}% ({wins}W/{losses}L)")
        current_trade = None
    except Exception as e:
        print(e)

def check_and_manage():
    global current_trade
    while True:
        try:
            df = fetch_data()
            trend, trend_type, adx, vol_ratio = check_trend(df)
            sr = get_support_resistance(df)
            curr_price = df['close'].iloc[-1]
            if current_trade:
                hold_hours = (datetime.now() - current_trade['time']).total_seconds() / 3600
                if hold_hours >= MAX_HOLD_HOURS:
                    close_trade(f"3 DAYS EXPIRY {hold_hours:.1f}h", curr_price)
                elif current_trade['side']=='buy':
                    if not current_trade['tp1_done'] and curr_price >= current_trade['tp1']:
                        current_trade['tp1_done']=True; current_trade['sl']=current_trade['entry']
                        send_alert(f"🎯 TP1 HIT BE")
                    if curr_price <= current_trade['sl']: close_trade("SL HIT", curr_price)
                else:
                    if not current_trade['tp1_done'] and curr_price <= current_trade['tp1']:
                        current_trade['tp1_done']=True; current_trade['sl']=current_trade['entry']
                    if curr_price >= current_trade['sl']: close_trade("SL HIT", curr_price)
            else:
                if trend!="NO TREND":
                    if trend=="UP" and sr['curr'] < sr['strong_res']*0.998: place_order('buy', trend_type)
                    elif trend=="DOWN" and sr['curr'] > sr['strong_sup']*1.002: place_order('sell', trend_type)
            time.sleep(60)
        except Exception as e:
            print(f"Manage Error: {e}"); time.sleep(30)

@bot.message_handler(commands=['start','pnl','status','sr','qty','close'])
def handle_all(message):
    try:
        txt = message.text.lower(); global qty_usd_global
        if '/qty' in txt:
            try: qty = float(message.text.split()[1]); qty_usd_global=qty; bot.reply_to(message, f"✅ Qty: ${qty} x {DEFAULT_LEVERAGE}x")
            except: bot.reply_to(message, f"Usage: /qty 5")
        elif '/close' in txt:
            if current_trade: close_trade("Manual", fetch_data()['close'].iloc[-1]); bot.reply_to(message, "Closing...")
            else: bot.reply_to(message, "No trade")
        elif '/sr' in txt:
            df=fetch_data(); sr=get_support_resistance(df); trend, ttype, adx, vol = check_trend(df)
            bot.reply_to(message, f"📊 *S/R {SYMBOL}*\nCurr: ${sr['curr']:.2f}\nRes: ${sr['strong_res']:.2f}\nSup: ${sr['strong_sup']:.2f}\nTrend: {ttype} {trend}\nADX: {adx:.1f}", parse_mode='Markdown')
        elif '/pnl' in txt:
            wr = (wins/(wins+losses)*100) if (wins+losses)>0 else 0
            bot.reply_to(message, f"💰 *PnL*\nTotal: ${total_pnl_usd:.2f}\nWR: {wr:.1f}% ({wins}W/{losses}L)\nSymbol: {SYMBOL}", parse_mode='Markdown')
        else:
            bot.reply_to(message, f"📈 *V19 LIVE*\nSymbol: {SYMBOL}\nQty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x\nTrade: {current_trade['side'] if current_trade else 'None'}", parse_mode='Markdown')
    except Exception as e: print(e)

@app.route('/')
def home(): return f"V19 LIVE | {SYMBOL} | Trade: {current_trade['side'] if current_trade else 'None'} | {datetime.now()}"

@app.route(f'/{TELEGRAM_BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    try: bot.process_new_updates([telebot.types.Update.de_json(request.stream.read().decode("utf-8"))])
    except: pass
    return "ok", 200

@app.route('/setwebhook')
def set_webhook_route():
    bot.remove_webhook(); time.sleep(1); bot.set_webhook(url=f"{RENDER_URL}/{TELEGRAM_BOT_TOKEN}"); return f"Webhook OK {RENDER_URL}"

threading.Thread(target=check_and_manage, daemon=True).start()
print("V19 Trading started")
try: bot.remove_webhook(); time.sleep(1); bot.set_webhook(url=f"{RENDER_URL}/{TELEGRAM_BOT_TOKEN}")
except Exception as e: print(e)
