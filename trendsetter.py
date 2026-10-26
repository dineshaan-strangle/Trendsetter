import os
import ccxt
import time
import pandas as pd
import ta
from datetime import datetime
import telebot
import threading
from flask import Flask

# ===== RENDER ENV CONFIG - 10 POINTS =====
DEFAULT_LEVERAGE = 100  # FIXED 100x
DEFAULT_QTY_USD = float(os.getenv("DEFAULT_QTY", "5"))
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
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
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_SECRET = os.getenv("BYBIT_SECRET")

# Check Env
if not TELEGRAM_BOT_TOKEN or not BYBIT_API_KEY:
    print("❌ ENV MISSING! Render la set pannunga!")

# Setup
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})
app = Flask(__name__)

# State
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
        'weak_res': curr * 1.005,
        'weak_sup': curr * 0.995
    }

def check_trend(df):
    adx = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], 14).adx().iloc[-1]
    ema9 = ta.trend.EMAIndicator(df['close'], 9).ema_indicator()
    ema21 = ta.trend.EMAIndicator(df['close'], 21).ema_indicator()
    vol_avg = df['volume'].rolling(20).mean().iloc[-1]
    vol_curr = df['volume'].iloc[-1]
    trend = "NO TREND"; trend_type = "NONE"
    if ema9.iloc[-1] > ema21.iloc[-1] and adx > ADX_THRESHOLD_NORMAL:
        trend = "UP"
        trend_type = "BIG TREND" if adx > ADX_THRESHOLD_BIG and vol_curr > vol_avg * VOLUME_MULTIPLIER_BIG else "NORMAL TREND"
    elif ema9.iloc[-1] < ema21.iloc[-1] and adx > ADX_THRESHOLD_NORMAL:
        trend = "DOWN"
        trend_type = "BIG TREND" if adx > ADX_THRESHOLD_BIG and vol_curr > vol_avg * VOLUME_MULTIPLIER_BIG else "NORMAL TREND"
    return trend, trend_type, adx, vol_curr/vol_avg if vol_avg>0 else 1

def place_order(side, qty_usd):
    global current_trade
    try:
        exchange.set_leverage(DEFAULT_LEVERAGE, SYMBOL)
        price = exchange.fetch_ticker(SYMBOL)['last']
        position_size = (qty_usd * DEFAULT_LEVERAGE) / price
        sl_price = price * (1 - SL_PERCENT/100) if side == 'buy' else price * (1 + SL_PERCENT/100)
        tp1 = price * (1 + TP1_PERCENT/100) if side == 'buy' else price * (1 - TP1_PERCENT/100)
        order = exchange.create_market_order(SYMBOL, side, position_size)
        current_trade = {'side': side, 'entry': price, 'qty_usd': qty_usd, 'position': position_size, 'sl': sl_price, 'tp1': tp1, 'time': datetime.now(), 'trend_type': 'NORMAL TREND'}
        sr = get_support_resistance(fetch_data())
        send_alert(f"🔥 *{side.upper()} {SYMBOL}*\nType: NORMAL TREND\nEntry: ${price}\nQty: ${qty_usd} x {DEFAULT_LEVERAGE}x = ${qty_usd*DEFAULT_LEVERAGE}\n\n*Resistance:* ${sr['strong_res']:.2f}\n*Support:* ${sr['strong_sup']:.2f}\nSL: ${sl_price:.2f} (-{SL_PERCENT}%)\nTP1: ${tp1:.2f} (+{TP1_PERCENT}%)")
        return True
    except Exception as e:
        send_alert(f"❌ Order Error: {e}"); return False

def close_trade(reason):
    global current_trade, daily_pnl, trade_count
    try:
        if not current_trade: return
        price = exchange.fetch_ticker(SYMBOL)['last']
        side = 'sell' if current_trade['side']=='buy' else 'buy'
        exchange.create_market_order(SYMBOL, side, current_trade['position'])
        pnl_usd = (price - current_trade['entry']) * current_trade['position'] if current_trade['side']=='buy' else (current_trade['entry'] - price) * current_trade['position']
        daily_pnl += pnl_usd; trade_count += 1
        send_alert(f"✅ *CLOSED: {reason}*\n{current_trade['side'].upper()} ${current_trade['entry']:.2f} -> ${price:.2f}\nP&L: ${pnl_usd:.2f} ({(pnl_usd/current_trade['qty_usd']*100):.1f}%)\nDaily: ${daily_pnl:.2f}")
        current_trade = None
    except Exception as e:
        send_alert(f"Close Error: {e}")

def check_and_manage():
    global current_trade, daily_pnl
    while True:
        try:
            df = fetch_data()
            trend, trend_type, adx, vol = check_trend(df)
            sr = get_support_resistance(df)
            if current_trade:
                curr_price = df['close'].iloc[-1]
                pnl_usd = (curr_price - current_trade['entry']) * current_trade['position'] if current_trade['side']=='buy' else (current_trade['entry'] - curr_price) * current_trade['position']
                if trend_type == "BIG TREND" and current_trade['trend_type'] == "NORMAL TREND":
                    current_trade['trend_type'] = "BIG TREND"
                    current_trade['sl'] = curr_price * (1 - TRAILING_SL_PERCENT/100) if current_trade['side']=='buy' else curr_price * (1 + TRAILING_SL_PERCENT/100)
                    send_alert(f"🚀 *BIG TREND CONTINUE!*\nTrade: {current_trade['side']} ${current_trade['entry']}\nNow: ${curr_price} P&L +${pnl_usd:.2f}\nTrailing SL: ${current_trade['sl']:.2f}\nRes: ${sr['strong_res']:.2f} Sup: ${sr['strong_sup']:.2f}")
                if (current_trade['side']=='buy' and curr_price <= current_trade['sl']) or (current_trade['side']=='sell' and curr_price >= current_trade['sl']):
                    close_trade(f"SL HIT")
                if datetime.now().hour == 23 and datetime.now().minute > 50 and current_trade['trend_type']=="NORMAL TREND":
                    close_trade("Daily Close Normal")
            else:
                if trend != "NO TREND":
                    place_order('buy' if trend=='UP' else 'sell', qty_usd_global)
            time.sleep(60)
        except Exception as e:
            send_alert(f"❌ *BOT ERROR/OFFLINE:* {e}"); time.sleep(60)

# TELEGRAM COMMANDS
@bot.message_handler(commands=['setqty','qty'])
def set_qty_cmd(message):
    global qty_usd_global
    try:
        qty = float(message.text.split()[1]); qty_usd_global = qty
        bot.reply_to(message, f"✅ Qty: ${qty} x {DEFAULT_LEVERAGE}x = ${qty*DEFAULT_LEVERAGE}")
    except:
        bot.reply_to(message, f"Usage: /setqty 5\nCurrent: ${qty_usd_global} x {DEFAULT_LEVERAGE}x")

@bot.message_handler(commands=['supportresistance','sr'])
def sr_cmd(message):
    try:
        df = fetch_data(); sr = get_support_resistance(df); trend, trend_type, adx, vol = check_trend(df)
        trade_info = f"Trade: {current_trade['side']} ${current_trade['entry']:.2f} P&L calc" if current_trade else "No active trade"
        bot.reply_to(message, f"📊 *S/R LEVELS*\n{trade_info}\n\nStrong Res: ${sr['strong_res']:.2f}\nStrong Sup: ${sr['strong_sup']:.2f}\nEMA9: ${sr['ema9']:.2f}\nTrend: {trend_type} {trend} ADX {adx:.1f}\nNow: ${sr['curr']:.2f}", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")

@bot.message_handler(commands=['close'])
def close_cmd(message):
    if current_trade:
        bot.reply_to(message, f"⚠️ Close? {current_trade['side']} ${current_trade['entry']} Reply YES")
        bot.register_next_step_handler(message, lambda m: close_trade("Manual Close") if m.text.upper()=="YES" else bot.reply_to(m, "Cancelled"))
    else:
        bot.reply_to(message, "No trade")

@bot.message_handler(commands=['pnl','status','dailypnl'])
def pnl_cmd(message):
    if current_trade:
        df = fetch_data(); curr = df['close'].iloc[-1]
        pnl = (curr - current_trade['entry']) * current_trade['position'] if current_trade['side']=='buy' else (current_trade['entry'] - curr) * current_trade['position']
        bot.reply_to(message, f"📈 LIVE P&L: ${pnl:.2f} ({pnl/current_trade['qty_usd']*100:.1f}%) {'✅ PROFIT' if pnl>0 else '❌ LOSS'}\nDaily: ${daily_pnl:.2f}", parse_mode='Markdown')
    else:
        bot.reply_to(message, f"Daily P&L: ${daily_pnl:.2f} ({trade_count} trades) Qty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x")

@app.route('/')
def home():
    return f"V15 LIVE | Daily ${daily_pnl:.2f} | Trade: {current_trade['side'] if current_trade else 'None'} | Qty ${qty_usd_global} x {DEFAULT_LEVERAGE}x"

def daily_summary_scheduler():
    while True:
        now = datetime.now()
        if now.hour == 23 and now.minute == 55:
            send_alert(f"📊 *DAILY SUMMARY*\nP&L: ${daily_pnl:.2f}\nTrades: {trade_count}\nQty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x"); time.sleep(70)
        time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=check_and_manage, daemon=True).start()
    threading.Thread(target=daily_summary_scheduler, daemon=True).start()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.getenv("PORT", 10000))), daemon=True).start()
    send_alert(f"✅ *V15 STARTED ON RENDER*\nLeverage {DEFAULT_LEVERAGE}x FIXED\nQty ${qty_usd_global} | {SYMBOL}\n10 Points Active!")
    bot.infinity_polling()
