import os, ccxt, time, pandas as pd, ta
from datetime import datetime
import telebot
import threading
from flask import Flask, request

# ===== CONFIG =====
DEFAULT_LEVERAGE = 100
DEFAULT_QTY_USD = float(os.getenv("DEFAULT_QTY", "5"))
TIMEFRAME = "15m"
SL_PERCENT = 0.8
TP1_PERCENT = 1.0
MAX_HOLD_HOURS = 72

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DELTA_API_KEY = os.getenv("DELTA_API_KEY")
DELTA_SECRET = os.getenv("DELTA_SECRET")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://trendsetter-vkdr.onrender.com")

print(f"✅ V24 FINAL ORDER FIX!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=False)

exchange = ccxt.delta({
    'apiKey': DELTA_API_KEY,
    'secret': DELTA_SECRET,
    'enableRateLimit': True,
    'hostname': 'api.india.delta.exchange',
    'options': {'defaultType': 'future'}
})

try:
    exchange.load_markets()
    print(f"✅ Markets Loaded: {len(exchange.symbols)}")
except Exception as e:
    print(f"Market load fail: {e}")

SYMBOL = "BTC/USDT"
print(f"✅ SYMBOL: {SYMBOL}")

app = Flask(__name__)
current_trade = None
qty_usd_global = DEFAULT_QTY_USD
wins = 0; losses = 0; total_pnl_usd = 0
cached_df = None
last_price = 0

def send_alert(msg):
    try:
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode='Markdown')
    except Exception as e:
        print(f"Alert fail: {e} | {msg}")

def fetch_data():
    global cached_df, last_price
    try:
        for sym in ["BTC/USDT", "BTC/USDT:USDT", "BTC/USD", "BTCUSDT", "BTCUSD"]:
            try:
                ohlcv = exchange.fetch_ohlcv(sym, TIMEFRAME, limit=200)
                if ohlcv and len(ohlcv) >= 50:
                    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
                    df = df.dropna()
                    if len(df) >= 30:
                        cached_df = df
                        last_price = float(df['close'].iloc[-1])
                        print(f"✅ OHLCV SUCCESS {sym}: {len(df)} Price: {last_price}")
                        return df
            except Exception as e:
                print(f"OHLCV {sym} fail: {e}")
                continue
        
        try:
            for sym in ["BTC/USDT", "BTC/USDT:USDT", "BTCUSDT"]:
                try:
                    ticker = exchange.fetch_ticker(sym)
                    if ticker and ticker['last']:
                        last_price = float(ticker['last'])
                        print(f"✅ TICKER ONLY {sym}: {last_price}")
                        break
                except:
                    continue
            if cached_df is not None:
                return cached_df
            price = last_price if last_price > 0 else 80000
            df = pd.DataFrame({
                'timestamp': [int(time.time()*1000)-i*15*60*1000 for i in range(200)][::-1],
                'open': [price]*200, 'high': [price*1.005]*200,
                'low': [price*0.995]*200, 'close': [price]*200, 'volume': [100.0]*200
            })
            cached_df = df
            return df
        except Exception as e:
            print(f"Ticker fail: {e}")
        
        if cached_df is not None:
            return cached_df
        return None
    except Exception as e:
        print(f"Fetch main error: {e}")
        return cached_df

def get_support_resistance(df):
    if df is None or len(df) < 5:
        return None
    try:
        curr = float(df['close'].iloc[-1])
        res = float(df['high'].tail(96).max())
        sup = float(df['low'].tail(96).min())
        return {'curr': curr, 'strong_res': res, 'strong_sup': sup}
    except Exception as e:
        return {'curr': float(df['close'].iloc[-1]), 'strong_res': float(df['high'].max()), 'strong_sup': float(df['low'].min())}

def check_trend(df):
    if df is None or len(df) < 30:
        return "NO TREND", "NONE", 0, 1
    try:
        adx_ind = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], 14, fillna=True)
        adx = float(adx_ind.adx().iloc[-1])
        plus_di = float(adx_ind.adx_pos().iloc[-1])
        minus_di = float(adx_ind.adx_neg().iloc[-1])
        ema9 = float(ta.trend.EMAIndicator(df['close'], 9, fillna=True).ema_indicator().iloc[-1])
        ema21 = float(ta.trend.EMAIndicator(df['close'], 21, fillna=True).ema_indicator().iloc[-1])
        trend = "NO TREND"; ttype = "NONE"
        if ema9 > ema21 and plus_di > minus_di and adx > 25:
            trend = "UP"; ttype = "NORMAL"
        elif ema9 < ema21 and minus_di > plus_di and adx > 25:
            trend = "DOWN"; ttype = "NORMAL"
        return trend, ttype, adx, 1
    except Exception as e:
        return "NO TREND", "NONE", 0, 1

def get_live_price():
    global last_price
    try:
        for sym in ["BTC/USDT", "BTC/USDT:USDT", "BTCUSDT", "BTC/USD"]:
            try:
                ticker = exchange.fetch_ticker(sym)
                if ticker and ticker['last'] is not None:
                    price = float(ticker['last'])
                    if price > 0:
                        last_price = price
                        return price
            except:
                continue
        if last_price and last_price > 0:
            return float(last_price)
        if cached_df is not None:
            return float(cached_df['close'].iloc[-1])
        return 80000.0
    except:
        return float(last_price) if last_price else 80000.0

def place_order(side, trend_type):
    global current_trade, last_price
    try:
        price = get_live_price()
        if price is None or price == 0:
            print("No price for order!")
            return False

        last_price = price
        position_size = max(1, int((qty_usd_global * DEFAULT_LEVERAGE) / price * 1000))
        sl = price * (1 - SL_PERCENT/100) if side == 'buy' else price * (1 + SL_PERCENT/100)
        
        print(f"Placing {side} order Price: {price} Size: {position_size}")
        exchange.create_market_order(SYMBOL, side, position_size)
        
        current_trade = {'side': side, 'entry': price, 'position': position_size, 'sl': sl, 'time': datetime.now()}
        send_alert(f"🔥 *{side.upper()} {SYMBOL}*\nEntry: ${price:.2f}\nSL: ${sl:.2f}\nQty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x")
        return True
    except Exception as e:
        send_alert(f"❌ Order Error: {e}")
        print(f"Order exception: {e}")
        return False

def close_trade(reason, price):
    global current_trade, wins, losses, total_pnl_usd, last_price
    try:
        if not current_trade:
            return
        if price is None or price == 0:
            price = get_live_price()
        
        side = 'sell' if current_trade['side'] == 'buy' else 'buy'
        exchange.create_market_order(SYMBOL, side, current_trade['position'])
        pnl_percent = ((price - current_trade['entry'])/current_trade['entry']*100) if current_trade['side']=='buy' else ((current_trade['entry']-price)/current_trade['entry']*100)
        pnl_usd = (pnl_percent/100 * qty_usd_global * DEFAULT_LEVERAGE)
        total_pnl_usd += pnl_usd
        if pnl_usd > 0: wins += 1
        else: losses += 1
        send_alert(f"✅ *CLOSED {reason}*\nPrice: ${price:.2f}\nPnL: {pnl_percent:.2f}%")
        current_trade = None
    except Exception as e:
        print(f"Close error: {e}")

def check_and_manage():
    global current_trade
    while True:
        try:
            df = fetch_data()
            if df is None:
                time.sleep(30)
                continue
            sr = get_support_resistance(df)
            if sr is None:
                time.sleep(30)
                continue
            trend, ttype, adx, vol = check_trend(df)
            curr = sr['curr']
            print(f"✅ LIVE Check: ${curr:.2f} Trend:{trend} ADX:{adx:.1f}")
            if current_trade:
                hold = (datetime.now() - current_trade['time']).total_seconds()/3600
                if hold >= MAX_HOLD_HOURS:
                    close_trade("3 DAYS", curr)
                elif (current_trade['side']=='buy' and curr <= current_trade['sl']) or (current_trade['side']=='sell' and curr >= current_trade['sl']):
                    close_trade("SL HIT", curr)
            else:
                if trend == "UP" and curr < sr['strong_res']*0.998:
                    place_order('buy', ttype)
                elif trend == "DOWN" and curr > sr['strong_sup']*1.002:
                    place_order('sell', ttype)
            time.sleep(60)
        except Exception as e:
            print(f"Manage Error: {e}")
            time.sleep(30)

@bot.message_handler(commands=['start','pnl','sr','qty','close','leverage'])
def handle_all(message):
    try:
        txt = message.text.lower()
        global qty_usd_global, DEFAULT_LEVERAGE
        if '/qty' in txt:
            try:
                qty = float(message.text.split()[1])
                qty_usd_global = qty
                bot.reply_to(message, f"✅ Qty Updated: ${qty} x {DEFAULT_LEVERAGE}x = ${qty*DEFAULT_LEVERAGE} position")
            except:
                bot.reply_to(message, f"Usage: /qty 5\nCurrent: ${qty_usd_global}")
        elif '/leverage' in txt:
            try:
                lev = int(message.text.split()[1])
                if 10 <= lev <= 200:
                    DEFAULT_LEVERAGE = lev
                    bot.reply_to(message, f"✅ Leverage: {lev}x\nQty: ${qty_usd_global} x {lev}x")
                else:
                    bot.reply_to(message, "Leverage 10-200 only")
            except:
                bot.reply_to(message, f"Current Leverage: {DEFAULT_LEVERAGE}x\nUsage: /leverage 100")
        elif '/close' in txt:
            if current_trade:
                df = fetch_data()
                price = get_support_resistance(df)['curr'] if df is not None else get_live_price()
                close_trade("Manual", price)
                bot.reply_to(message, "Closing trade...")
            else:
                bot.reply_to(message, "No open trade")
        elif '/sr' in txt:
            df = fetch_data()
            if df is None:
                bot.reply_to(message, f"⏳ Data Loading...\nLast Price: ${last_price}\nTry again 10s\nSymbol: {SYMBOL}")
                return
            sr = get_support_resistance(df)
            trend, ttype, adx, vol = check_trend(df)
            bot.reply_to(message, f"📊 *{SYMBOL} LIVE*\n💰 Price: ${sr['curr']:.2f}\n📈 Res: ${sr['strong_res']:.2f}\n📉 Sup: ${sr['strong_sup']:.2f}\nTrend: {ttype} {trend}\nADX: {adx:.1f}\nCandles: {len(df)}", parse_mode='Markdown')
        elif '/pnl' in txt:
            wr = (wins/(wins+losses)*100) if (wins+losses)>0 else 0
            bot.reply_to(message, f"💰 *PnL Report*\nPnL: ${total_pnl_usd:.2f}\nWR: {wr:.1f}% ({wins}W/{losses}L)\nQty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x\nTrade: {current_trade['side'] if current_trade else 'None'}", parse_mode='Markdown')
        else:
            df = fetch_data()
            price_str = f"${get_support_resistance(df)['curr']:.2f}" if df is not None else f"${last_price:.2f}" if last_price else "Loading..."
            bot.reply_to(message, f"📈 *V24 LIVE FIXED!*\nSymbol: {SYMBOL}\nPrice: {price_str}\nQty: ${qty_usd_global} x {DEFAULT_LEVERAGE}x\nTrade: {current_trade['side'] if current_trade else 'None'}\n\nCommands:\n/sr - Market\n/pnl - Profit\n/qty 5 - Set Qty\n/leverage 100 - Set Lev\n/close - Close Trade", parse_mode='Markdown')
    except Exception as e:
        print(f"Handler error: {e}")
        try:
            bot.reply_to(message, f"Error: {e}")
        except:
            pass

@app.route('/')
def home():
    return f"V24 LIVE FIXED | {SYMBOL} ${last_price} | {datetime.now()} | Trade: {current_trade['side'] if current_trade else 'None'}"

@app.route(f'/{TELEGRAM_BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    try:
        bot.process_new_updates([telebot.types.Update.de_json(request.stream.read().decode("utf-8"))])
    except Exception as e:
        print(f"Webhook error: {e}")
    return "ok", 200

@app.route('/setwebhook')
def set_webhook_route():
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=f"{RENDER_URL}/{TELEGRAM_BOT_TOKEN}")
    return f"Webhook set"

if __name__ == '__main__':
    threading.Thread(target=check_and_manage, daemon=True).start()
    print("V24 Trading Thread Started")
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=f"{RENDER_URL}/{TELEGRAM_BOT_TOKEN}")
        print(f"Webhook OK")
    except Exception as e:
        print(f"Webhook fail: {e}")
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
