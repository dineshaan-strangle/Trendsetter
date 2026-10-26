import os
import ccxt
import time
import pandas as pd
import ta
from datetime import datetime
import telebot
import threading
from flask import Flask

# ===== DELTA CONFIG - 10 POINTS =====
DEFAULT_LEVERAGE = 100  # FIXED - Delta la 100x
DEFAULT_QTY_USD = float(os.getenv("DEFAULT_QTY", "5"))
SYMBOL = os.getenv("SYMBOL", "BTCUSD")  # Delta la BTCUSD
TIMEFRAME = "15m"

SL_PERCENT = 0.8
TP1_PERCENT = 1.0
TP2_PERCENT = 3.0
TP3_PERCENT = 5.0
TRAILING_SL_PERCENT = 1.0

ADX_THRESHOLD_NORMAL = 25
ADX_THRESHOLD_BIG = 35
VOLUME_MULTIPLIER_BIG = 1.8

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_SECRET = os.getenv("DELTA_SECRET")

# ENV Check
if not TELEGRAM_BOT_TOKEN or not DELTA_API_KEY or not DELTA_SECRET:
    print("❌ ENV MISSING! Render la set pannunga!")
else:
    print(f"✅ ENV OK - SYMBOL {SYMBOL} QTY ${DEFAULT_QTY_USD} x {DEFAULT_LEVERAGE}x")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)

# DELTA EXCHANGE
exchange = ccxt.delta({
    'apiKey': DELTA_API_KEY,
    'secret': DELTA_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

app = Flask(__name__)

current_trade = None
daily_pnl = 0
trade_count = 0
qty_usd_global = DEFAULT_QTY_USD

def send_alert(msg):
    try:
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode='Markdown')
    except Exception as e:
        print(f"Telegram Error: {e}")

def fetch_data():
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=200)
    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
    return df

def get_support_resistance(df):
    last_day_high = df['high'].rolling(96).max().iloc[-2]
    last_day_low = df['low'].rolling(96).min().iloc[-2]
    ema9 = ta.trend.EMAIndicator(df['close'], 9).ema_indicator().iloc[-1]
    curr = df['close'].iloc[-1]
    return {
        'curr': curr,
        'strong_res': max(last_day_high, df['high'].tail(50).max()),
        'strong_sup': min(last_day_low, df['low'].tail(50).min()),
        'last_high': last_day_high,
        'last_low': last_day_low,
        'ema9': ema9,
    }

def check_trend(df):
    adx = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], 14).adx().iloc[-1]
    ema9 = ta.trend.EMAIndicator(df['close'], 9).ema_indicator()
    ema21 = ta.trend.EMAIndicator(df['close'], 21).ema_indicator()
    vol_avg = df['volume'].rolling(20).mean().iloc[-1]
    vol_curr = df['volume'].iloc[-1]
    trend = "NO TREND"
    trend_type = "NONE"
    if ema9.iloc[-1] > ema21.iloc[-1] and adx > ADX_THRESHOLD_NORMAL:
        trend = "UP"
        if adx > ADX_THRESHOLD_BIG and vol_curr > vol_avg * VOLUME_MULTIPLIER_BIG:
            trend_type = "BIG TREND"
        else:
            trend_type = "NORMAL TREND"
    elif ema9.iloc[-1] < ema21.iloc[-1] and adx > ADX_THRESHOLD_NORMAL:
        trend = "DOWN"
        if adx > ADX_THRESHOLD_BIG and vol_curr > vol_avg * VOLUME_MULTIPLIER_BIG:
            trend_type = "BIG TREND"
        else:
            trend_type = "NORMAL TREND"
    return trend, trend_type, adx, vol_curr/vol_avg if vol_avg>0 else 1

def place_order(side, qty_usd):
    global current_trade
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price = ticker['last']
        position_size = int((qty_usd * DEFAULT_LEVERAGE) / price * 1000)
        if position_size < 1:
            position_size = 1
        
        sl_price = price * (1 - SL_PERCENT/100) if side == 'buy' else price * (1 + SL_PERCENT/100)
        tp1 = price * (1 + TP1_PERCENT/100) if side == 'buy' else price * (1 - TP1_PERCENT/100)
        
        exchange.create_market_order(SYMBOL, side, position_size)
        
        current_trade = {
            'side': side, 
            'entry': price, 
            'qty_usd': qty_usd, 
            'position': position_size, 
            'sl': sl_price, 
            'tp1': tp1, 
            'time': datetime.now(), 
            'trend_type': 'NORMAL TREND'
        }
        sr = get_support_resistance(fetch_data())
        send_alert(f"🔥 *{side.upper()} {SYMBOL} (DELTA)*\nType: NORMAL TREND\nEntry: ${price}\nQty: ${qty_usd} x {DEFAULT_LEVERAGE}x\nContracts: {position_size}\n\n*Res:* ${sr['strong_res']:.2f}\n*Sup:* ${sr['strong_sup']:.2f}\nSL: ${sl_price:.2f}\nTP1: ${tp1:.2f}")
        return True
    except Exception as e:
        send_alert(f"❌ Delta Order Error: {e}")
        print(f"Order Error: {e}")
        return False

def close_trade(reason):
    global current_trade, daily_pnl, trade_count
    try:
        if not current_trade:
            return
        price = exchange.fetch_ticker(SYMBOL)['last']
        side = 'sell' if current_trade['side']=='buy' else 'buy'
        exchange.create_market_order(SYMBOL, side, current_trade['position'])
        daily_pnl += 1
        trade_count += 1
        send_alert(f"✅ *DELTA CLOSED: {reason}*\n{current_trade['side'].upper()} ${current_trade['entry']:.2f} -> ${price:.2f}\nDaily: {trade_count} trades")
        current_trade = None
    except Exception as e:
        send_alert(f"Close Error: {e}")
        print(f"Close Error: {e}")

def check_and_manage():
    global current_trade
    while True:
        try:
            df = fetch_data()
            trend, trend_type, adx, vol = check_trend(df)
            sr = get_support_resistance(df)
            if current_trade:
                curr_price = df['close'].iloc[-1]
                if trend_type == "BIG TREND" and current_trade['trend_type'] == "NORMAL TREND":
                    current_trade['trend_type'] = "BIG TREND"
                    current_trade['sl'] = curr_price * (1 - TRAILING_SL_PERCENT/100) if current_trade['side']=='buy' else curr_price * (1 + TRAILING_SL_PERCENT/100)
                    send_alert(f"🚀 *BIG TREND CONTINUE! DELTA*\nTrade: {current_trade['side']} ${current_trade['entry']}\nNow: ${curr_price}\nTrailing SL: ${current_trade['sl']:.2f}")
                if (current_trade['side']=='buy' and curr_price <= current_trade['sl']) or (current_trade['side']=='sell' and curr_price >= current_trade['sl']):
                    close_trade(f"SL HIT")
            else:
                if trend != "NO TREND":
                    place_order('buy' if trend=='UP' else 'sell', qty_usd_global)
            time.sleep(60)
        except Exception as e:
            print(f"Manage Error: {e}")
            time.sleep(60)

@bot.message_handler(commands=['setqty','qty'])
def set_qty_cmd(message):
    global qty_usd_global
    try:
        qty = float(message.text.split()[1])
        qty_usd_global = qty
        bot.reply_to(message, f"✅ Delta Qty Updated: ${qty} x {DEFAULT_LEVERAGE}x")
    except:
        bot.reply_to(message, f"Usage: /setqty 5\nCurrent: ${qty_usd_global}")

@bot.message_handler(commands=['supportresistance','sr'])
def sr_cmd(message):
    try:
        df = fetch_data()
        sr = get_support_resistance(df)
        trend, trend_type, adx, vol = check_trend(df)
        bot.reply_to(message, f"📊 *DELTA S/R {SYMBOL}*\nCurr: ${sr['curr']:.2f}\nRes: ${sr['strong_res']:.2f}\nSup: ${sr['strong_sup']:.2f}\nTrend: {trend_type} {trend}\nADX: {adx:.1f}", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")

@bot.message_handler(commands=['close'])
def close_cmd(message):
    if current_trade:
        close_trade("Manual Close")
        bot.reply_to(message, "Closing trade...")
    else:
        bot.reply_to(message, "No trade open")

@bot.message_handler(commands=['pnl','status','start'])
def pnl_cmd(message):
    txt = f"📈 *DELTA STATUS*\nSYMBOL: {SYMBOL}\nQty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x\nTrade: {current_trade['side'] if current_trade else 'None'}\nDaily: {trade_count} trades"
    bot.reply_to(message, txt, parse_mode='Markdown')

@app.route('/')
def home():
    return f"V15 DELTA LIVE | {SYMBOL} | Qty ${qty_usd_global} x {DEFAULT_LEVERAGE}x | Trade: {current_trade['side'] if current_trade else 'None'} | {datetime.now()}"

def run_flask():
    port = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=port, use_reloader=False, debug=False)

if __name__ == "__main__":
    # FIX FOR 409 CONFLICT
    try:
        bot.delete_webhook(drop_pending_updates=True)
        print("Webhook deleted")
    except Exception as e:
        print(f"Webhook delete: {e}")
    
    time.sleep(2)
    
    threading.Thread(target=check_and_manage, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()
    
    print("✅ V15 DELTA STARTED - Single Polling Mode")
    send_alert(f"✅ *V15 DELTA STARTED ON RENDER*\nSYMBOL: {SYMBOL}\nLeverage: {DEFAULT_LEVERAGE}x (Fixed)\nQty: ${qty_usd_global}\n409 Fixed - Single Instance")

    while True:
        try:
            bot.polling(none_stop=True, interval=2, timeout=20, long_polling_timeout=20)
        except Exception as e:
            print(f"Polling error: {e}, retry in 5s")
            try:
                bot.stop_polling()
            except:
                pass
            time.sleep(5)
