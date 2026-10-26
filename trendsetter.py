import os, ccxt, time, pandas as pd, ta
from datetime import datetime
import telebot
import threading
from flask import Flask, request

# ===== 10 POINTS DELTA CONFIG =====
DEFAULT_LEVERAGE = 100  # FIXED
DEFAULT_QTY_USD = float(os.getenv("DEFAULT_QTY", "5"))
SYMBOL = os.getenv("SYMBOL", "BTCUSD")
TIMEFRAME = "15m"

# POINT 1,2,3 - TP SL %
SL_PERCENT = 0.8
TP1_PERCENT = 1.0
TP2_PERCENT = 3.0
TP3_PERCENT = 5.0
TRAILING_SL_PERCENT = 1.0
TRAILING_TRIGGER_PERCENT = 0.8

# POINT 4,5 - ADX & VOLUME
ADX_THRESHOLD_NORMAL = 25
ADX_THRESHOLD_BIG = 35
VOLUME_MULTIPLIER_BIG = 1.8

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_SECRET = os.getenv("DELTA_SECRET")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://trendsetter-vkdr.onrender.com")

print(f"✅ V17 FULL STRATEGY - {SYMBOL} QTY ${DEFAULT_QTY_USD} x {DEFAULT_LEVERAGE}x")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)
exchange = ccxt.delta({'apiKey': DELTA_API_KEY, 'secret': DELTA_SECRET, 'enableRateLimit': True, 'options': {'defaultType': 'future'}})
app = Flask(__name__)

current_trade = None
qty_usd_global = DEFAULT_QTY_USD
daily_pnl = 0

def send_alert(msg):
    try: bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode='Markdown')
    except Exception as e: print(e)

def fetch_data():
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=200)
    return pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])

# POINT 6,7 - SUPPORT RESISTANCE
def get_support_resistance(df):
    last_96_high = df['high'].rolling(96).max().iloc[-2] # last day
    last_96_low = df['low'].rolling(96).min().iloc[-2]
    recent_high = df['high'].tail(50).max()
    recent_low = df['low'].tail(50).min()
    curr = df['close'].iloc[-1]
    ema9 = ta.trend.EMAIndicator(df['close'], 9).ema_indicator().iloc[-1]
    return {
        'curr': curr,
        'strong_res': max(last_96_high, recent_high),
        'strong_sup': min(last_96_low, recent_low),
        'last_day_high': last_96_high,
        'last_day_low': last_96_low,
        'ema9': ema9
    }

# POINT 8,9 - TREND CHECK ADX + EMA
def check_trend(df):
    adx_ind = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], 14)
    adx = adx_ind.adx().iloc[-1]
    plus_di = adx_ind.adx_pos().iloc[-1]
    minus_di = adx_ind.adx_neg().iloc[-1]
    ema9 = ta.trend.EMAIndicator(df['close'], 9).ema_indicator()
    ema21 = ta.trend.EMAIndicator(df['close'], 21).ema_indicator()
    vol_avg = df['volume'].rolling(20).mean().iloc[-1]
    vol_curr = df['volume'].iloc[-1]
    vol_ratio = vol_curr/vol_avg if vol_avg>0 else 1

    trend = "NO TREND"
    trend_type = "NONE"
    
    if ema9.iloc[-1] > ema21.iloc[-1] and plus_di > minus_di and adx > ADX_THRESHOLD_NORMAL:
        trend = "UP"
        if adx > ADX_THRESHOLD_BIG and vol_ratio > VOLUME_MULTIPLIER_BIG:
            trend_type = "BIG TREND"
        else:
            trend_type = "NORMAL TREND"
    elif ema9.iloc[-1] < ema21.iloc[-1] and minus_di > plus_di and adx > ADX_THRESHOLD_NORMAL:
        trend = "DOWN"
        if adx > ADX_THRESHOLD_BIG and vol_ratio > VOLUME_MULTIPLIER_BIG:
            trend_type = "BIG TREND"
        else:
            trend_type = "NORMAL TREND"
    
    return trend, trend_type, adx, vol_ratio

# POINT 10 - ENTRY WITH SL TP
def place_order(side, trend_type):
    global current_trade
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price = ticker['last']
        position_size = max(1, int((qty_usd_global * DEFAULT_LEVERAGE) / price * 1000))
        
        # SL TP Calculation
        if side == 'buy':
            sl_price = price * (1 - SL_PERCENT/100)
            tp1 = price * (1 + TP1_PERCENT/100)
            tp2 = price * (1 + TP2_PERCENT/100)
            tp3 = price * (1 + TP3_PERCENT/100)
        else:
            sl_price = price * (1 + SL_PERCENT/100)
            tp1 = price * (1 - TP1_PERCENT/100)
            tp2 = price * (1 - TP2_PERCENT/100)
            tp3 = price * (1 - TP3_PERCENT/100)

        exchange.create_market_order(SYMBOL, side, position_size)
        
        current_trade = {
            'side': side, 'entry': price, 'qty_usd': qty_usd_global,
            'position': position_size, 'sl': sl_price, 'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
            'trend_type': trend_type, 'tp1_done': False, 'tp2_done': False,
            'time': datetime.now()
        }
        sr = get_support_resistance(fetch_data())
        send_alert(f"🔥 *{side.upper()} {SYMBOL} (DELTA)*\nType: {trend_type}\nEntry: ${price:.2f}\nQty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x\nContracts: {position_size}\n\n*STRONG RES:* ${sr['strong_res']:.2f}\n*STRONG SUP:* ${sr['strong_sup']:.2f}\nLastDay H: ${sr['last_day_high']:.2f} L: ${sr['last_day_low']:.2f}\n\n*SL:* ${sl_price:.2f} (-{SL_PERCENT}%)\n*TP1:* ${tp1:.2f} (+{TP1_PERCENT}%)\n*TP2:* ${tp2:.2f} (+{TP2_PERCENT}%)\n*TP3:* ${tp3:.2f} (+{TP3_PERCENT}%)")
        return True
    except Exception as e:
        send_alert(f"❌ Order Error: {e}")
        print(e)
        return False

def close_trade(reason, price):
    global current_trade
    try:
        if not current_trade: return
        side = 'sell' if current_trade['side']=='buy' else 'buy'
        exchange.create_market_order(SYMBOL, side, current_trade['position'])
        send_alert(f"✅ *CLOSED: {reason}*\n{current_trade['side'].upper()} ${current_trade['entry']:.2f} -> ${price:.2f}\nQty: ${current_trade['qty_usd']}")
        current_trade = None
    except Exception as e:
        send_alert(f"Close Error: {e}")

def check_and_manage():
    global current_trade
    while True:
        try:
            df = fetch_data()
            trend, trend_type, adx, vol_ratio = check_trend(df)
            sr = get_support_resistance(df)
            curr_price = df['close'].iloc[-1]
            
            if current_trade:
                # TRAILING & TP LOGIC
                if current_trade['side']=='buy':
                    # TP1
                    if not current_trade['tp1_done'] and curr_price >= current_trade['tp1']:
                        current_trade['tp1_done'] = True
                        current_trade['sl'] = current_trade['entry'] # Move to breakeven
                        send_alert(f"🎯 *TP1 HIT DELTA!* ${curr_price:.2f}\nMoving SL to Breakeven: ${current_trade['entry']:.2f}")
                    # TP2
                    if not current_trade['tp2_done'] and curr_price >= current_trade['tp2']:
                        current_trade['tp2_done'] = True
                        # Trail to TP1
                        current_trade['sl'] = current_trade['tp1']
                        send_alert(f"🎯🎯 *TP2 HIT! TRAILING to TP1*\nSL Now: ${current_trade['sl']:.2f}")
                    # BIG TREND CONTINUE
                    if trend_type=="BIG TREND" and current_trade['trend_type']=="NORMAL TREND":
                        current_trade['trend_type']="BIG TREND"
                        current_trade['sl'] = curr_price * (1 - TRAILING_SL_PERCENT/100)
                        send_alert(f"🚀 *BIG TREND CONTINUE!*\nTrailing SL: ${current_trade['sl']:.2f}")
                    # SL Check
                    if curr_price <= current_trade['sl']:
                        close_trade(f"SL HIT {trend_type}", curr_price)
                else: # SELL
                    if not current_trade['tp1_done'] and curr_price <= current_trade['tp1']:
                        current_trade['tp1_done']=True
                        current_trade['sl']=current_trade['entry']
                        send_alert(f"🎯 *TP1 HIT DELTA SHORT!* ${curr_price:.2f}\nBreakeven SL")
                    if not current_trade['tp2_done'] and curr_price <= current_trade['tp2']:
                        current_trade['tp2_done']=True
                        current_trade['sl']=current_trade['tp1']
                        send_alert(f"🎯🎯 *TP2 HIT SHORT!* SL: ${current_trade['sl']:.2f}")
                    if trend_type=="BIG TREND" and current_trade['trend_type']=="NORMAL TREND":
                        current_trade['trend_type']="BIG TREND"
                        current_trade['sl']=curr_price * (1 + TRAILING_SL_PERCENT/100)
                        send_alert(f"🚀 *BIG TREND SHORT CONTINUE!* SL: ${current_trade['sl']:.2f}")
                    if curr_price >= current_trade['sl']:
                        close_trade(f"SL HIT {trend_type}", curr_price)
            else:
                if trend!="NO TREND":
                    # Support Resistance filter - Strong Res ku mela Buy koodathu, Strong Sup ku keezha Sell koodathu
                    if trend=="UP" and sr['curr'] < sr['strong_res']*0.998:
                        place_order('buy', trend_type)
                    elif trend=="DOWN" and sr['curr'] > sr['strong_sup']*1.002:
                        place_order('sell', trend_type)
            
            time.sleep(60)
        except Exception as e:
            print(f"Manage Error: {e}"); time.sleep(60)

@bot.message_handler(commands=['start','pnl','status','sr','qty','close'])
def handle_all(message):
    try:
        txt = message.text.lower()
        global qty_usd_global
        if '/qty' in txt:
            try:
                qty = float(message.text.split()[1]); qty_usd_global=qty
                bot.reply_to(message, f"✅ Qty: ${qty} x {DEFAULT_LEVERAGE}x")
            except: bot.reply_to(message, f"Usage: /qty 5\nCurrent: ${qty_usd_global}")
        elif '/close' in txt:
            if current_trade: close_trade("Manual Close", fetch_data()['close'].iloc[-1]); bot.reply_to(message, "Closing...")
            else: bot.reply_to(message, "No trade")
        elif '/sr' in txt:
            df=fetch_data(); sr=get_support_resistance(df); trend, ttype, adx, vol = check_trend(df)
            bot.reply_to(message, f"📊 *DELTA S/R {SYMBOL}*\nCurr: ${sr['curr']:.2f}\nRes: ${sr['strong_res']:.2f}\nSup: ${sr['strong_sup']:.2f}\nLast H: ${sr['last_day_high']:.2f} L: ${sr['last_day_low']:.2f}\nTrend: {ttype} {trend}\nADX: {adx:.1f} Vol: {vol:.2f}x", parse_mode='Markdown')
        else:
            bot.reply_to(message, f"📈 *DELTA FULL STRATEGY LIVE*\nSYMBOL: {SYMBOL}\nQty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x\nTrade: {current_trade['side'] if current_trade else 'None'}\nSL: {SL_PERCENT}% TP1: {TP1_PERCENT}% TP2: {TP2_PERCENT}% TP3: {TP3_PERCENT}%\nMode: WEBHOOK No 409!", parse_mode='Markdown')
    except Exception as e: print(e)

@app.route('/')
def home(): return f"V17 DELTA FULL 10 POINTS LIVE | {SYMBOL} | Trade: {current_trade['side'] if current_trade else 'None'} | {datetime.now()}"

@app.route(f'/{TELEGRAM_BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    try:
        bot.process_new_updates([telebot.types.Update.de_json(request.stream.read().decode("utf-8"))])
    except Exception as e: print(e)
    return "ok", 200

@app.route('/setwebhook')
def set_webhook_route():
    bot.remove_webhook(); time.sleep(1)
    bot.set_webhook(url=f"{RENDER_URL}/{TELEGRAM_BOT_TOKEN}")
    return f"Webhook set to {RENDER_URL} - OK! Now /pnl"

threading.Thread(target=check_and_manage, daemon=True).start()
print("V17 Trading thread started")

try:
    bot.remove_webhook(); time.sleep(1)
    bot.set_webhook(url=f"{RENDER_URL}/{TELEGRAM_BOT_TOKEN}")
    print(f"Webhook set {RENDER_URL}")
except Exception as e: print(e)
