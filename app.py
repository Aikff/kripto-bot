import os
from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
import ccxt
import pandas as pd
from datetime import datetime
import time

# --- AYARLAR ---
app = Flask(__name__)

# Veritabanı
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///signals.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- VERİTABANI MODELİ (GÜNCELLENDİ) ---
class Signal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False)
    timeframe = db.Column(db.String(5), nullable=False) # YENİ SÜTUN: 2h, 4h, 12h, 1d
    side = db.Column(db.String(10), nullable=False)
    price = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.now)
    ema_val = db.Column(db.Float, nullable=True)
    sma_val = db.Column(db.Float, nullable=True)

# DB Oluşturma
with app.app_context():
    db.create_all()

# --- BORSA BAĞLANTISI ---
def get_exchange():
    return ccxt.binance({
        'enableRateLimit': True, # Çoklu timeframe tararken ban yememek için şart
        'options': {
            'defaultType': 'future',
        }
    })

# --- ANALİZ MOTORU (MTF GÜNCELLEMESİ) ---
def analyze_market():
    # Taranacak zaman dilimleri
    TIMEFRAMES = ['2h', '4h', '12h', '1d']
    
    print(f"[{datetime.now()}] 🚀 MTF Tarama Başlıyor: {TIMEFRAMES}")
    
    exchange = get_exchange()
    
    try:
        markets = exchange.load_markets()
        # Sadece USDT Futures ve Aktif olanlar
        target_symbols = [
            symbol for symbol in markets 
            if '/USDT' in symbol and markets[symbol]['active']
        ]
        
        print(f"İşlenecek Coin: {len(target_symbols)} | Zaman Dilimleri: {len(TIMEFRAMES)}")
        
        for symbol in target_symbols:
            # Temiz sembol adı (DB için)
            clean_symbol = symbol.split(':')[0]
            
            for tf in TIMEFRAMES:
                try:
                    # Her timeframe için mumları çek
                    # Günlük (1d) için daha az mum yetse de standart 60 tutalım
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=60)
                    
                    if not ohlcv or len(ohlcv) < 55:
                        continue

                    df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
                    
                    # İndikatörler
                    df['ema25'] = df['close'].ewm(span=25, adjust=False).mean()
                    df['sma50'] = df['close'].rolling(window=50).mean()
                    
                    # Son KAPANMIŞ mum (-2)
                    last = df.iloc[-2]
                    
                    c_open = last['open']
                    c_close = last['close']
                    ema = last['ema25']
                    sma = last['sma50']
                    
                    signal_side = None
                    
                    # STRATEJİ
                    # LONG
                    if (c_open < ema and c_open < sma) and (c_close > ema and c_close > sma):
                        signal_side = "LONG 🟢"
                    # SHORT
                    elif (c_open > ema and c_open > sma) and (c_close < ema and c_close < sma):
                        signal_side = "SHORT 🔴"
                        
                    if signal_side:
                        with app.app_context():
                            # Tekrar Kontrolü: Aynı Coin + Aynı Timeframe + Son 2 mum süresi
                            last_sig = Signal.query.filter_by(symbol=clean_symbol, timeframe=tf).order_by(Signal.timestamp.desc()).first()
                            
                            should_save = False
                            if not last_sig:
                                should_save = True
                            else:
                                # Timeframe'e göre bekleme süresi ayarla (Saniye cinsinden)
                                # Basit mantık: En azından o timeframe'in yarısı kadar zaman geçmiş olmalı
                                diff = (datetime.now() - last_sig.timestamp).total_seconds()
                                
                                # 2h=7200s, 4h=14400s vb. (Basit koruma için 1 saat diyelim hepsine)
                                if diff > 3600: 
                                    should_save = True
                            
                            if should_save:
                                new_signal = Signal(
                                    symbol=clean_symbol, 
                                    timeframe=tf, # Hangi periyotta geldiğini kaydet
                                    side=signal_side, 
                                    price=c_close,
                                    ema_val=ema,
                                    sma_val=sma
                                )
                                db.session.add(new_signal)
                                db.session.commit()
                                print(f"✅ [{tf}] {clean_symbol} -> {signal_side}")

                except Exception as inner_e:
                    continue
                    
    except Exception as e:
        print(f"Genel Hata: {e}")
    
    print(f"[{datetime.now()}] Tarama Bitti.")

# --- ZAMANLAYICI ---
scheduler = BackgroundScheduler()
# Tarama süresi uzayacağı için aralığı 20 dakikaya çıkaralım, işlemci rahatlasın
scheduler.add_job(func=analyze_market, trigger="interval", minutes=20)
scheduler.start()

# --- WEB ROTA ---
@app.route('/')
def index():
    # Verileri çekerken artık timeframe'e göre gruplamak HTML tarafında yapılacak
    # En son 200 sinyali çekelim
    all_signals = Signal.query.order_by(Signal.timestamp.desc()).limit(200).all()
    
    # Son tarama zamanını bul (UX için)
    last_scan = "Bekleniyor..."
    if all_signals:
        last_scan = all_signals[0].timestamp.strftime('%H:%M')

    return render_template('index.html', signals=all_signals, last_scan=last_scan)

if __name__ == '__main__':
    app.run(debug=True)
