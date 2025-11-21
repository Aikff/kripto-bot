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

# Veritabanı Ayarı
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///signals.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- VERİTABANI MODELİ ---
class Signal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(30), nullable=False)
    side = db.Column(db.String(10), nullable=False)
    price = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.now)
    ema_val = db.Column(db.Float, nullable=True)
    sma_val = db.Column(db.Float, nullable=True)

# İlk çalışmada DB oluştur
with app.app_context():
    db.create_all()

# --- BORSA BAĞLANTISI VE AYARLAR ---
def get_exchange():
    return ccxt.binance({
        'enableRateLimit': True,  # Çok önemli: Ban yememek için hızı otomatik ayarlar
        'options': {
            'defaultType': 'future',  # Spot değil, VADELİ (Futures) piyasası
        }
    })

# --- ANALİZ MOTORU ---
def analyze_market():
    print(f"[{datetime.now()}] 🚀 TÜM FUTURES PİYASASI TARANIYOR...")
    
    exchange = get_exchange()
    
    try:
        # 1. Adım: Piyasadaki tüm sembolleri yükle
        markets = exchange.load_markets()
        
        # 2. Adım: Sadece USDT paritelerini ve Aktif olanları filtrele
        # Örnek: 'BTC/USDT:USDT' formatında gelir futures sembolleri
        target_symbols = [
            symbol for symbol in markets 
            if '/USDT' in symbol and markets[symbol]['active']
        ]
        
        print(f"İşlenecek Coin Sayısı: {len(target_symbols)}")
        
        # 3. Adım: Döngüye gir (Rate Limit yüzünden bu işlem zaman alır)
        for symbol in target_symbols:
            try:
                # 2H mumlar, son 60 mum yeterli
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe='2h', limit=60)
                
                if not ohlcv or len(ohlcv) < 50:
                    continue

                df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
                
                # İndikatörler
                df['ema25'] = df['close'].ewm(span=25, adjust=False).mean()
                df['sma50'] = df['close'].rolling(window=50).mean()
                
                # Son KAPANMIŞ mumu al (-2)
                last = df.iloc[-2]
                
                c_open = last['open']
                c_close = last['close']
                ema = last['ema25']
                sma = last['sma50']
                
                signal_side = None
                
                # --- STRATEJİ: Dual Cross ---
                # LONG
                if (c_open < ema and c_open < sma) and (c_close > ema and c_close > sma):
                    signal_side = "LONG 🟢"
                
                # SHORT
                elif (c_open > ema and c_open > sma) and (c_close < ema and c_close < sma):
                    signal_side = "SHORT 🔴"
                    
                # Sinyal varsa kaydet
                if signal_side:
                    with app.app_context():
                        # Sinyal tekrarını önle (2 saat kuralı)
                        # Veritabanında sembol adını temizleyerek arayalım (BTC/USDT:USDT -> BTC/USDT)
                        clean_symbol = symbol.split(':')[0] 
                        
                        last_sig = Signal.query.filter_by(symbol=clean_symbol).order_by(Signal.timestamp.desc()).first()
                        
                        should_save = False
                        if not last_sig:
                            should_save = True
                        else:
                            diff = (datetime.now() - last_sig.timestamp).total_seconds()
                            if diff > 7200: 
                                should_save = True
                        
                        if should_save:
                            new_signal = Signal(
                                symbol=clean_symbol, 
                                side=signal_side, 
                                price=c_close,
                                ema_val=ema,
                                sma_val=sma
                            )
                            db.session.add(new_signal)
                            db.session.commit()
                            print(f"✅ BULUNDU: {clean_symbol} -> {signal_side}")

            except Exception as inner_e:
                # Tek bir coinde hata olursa diğerine geç, döngüyü kırma
                # print(f"Atlanan coin {symbol}: {inner_e}") 
                continue
                
    except Exception as e:
        print(f"Genel Tarama Hatası: {e}")
    
    print(f"[{datetime.now()}] Tarama tamamlandı.")

# --- ZAMANLAYICI ---
scheduler = BackgroundScheduler()
# Tüm marketi taramak uzun sürer, aralığı 30 dakikaya çıkarmak mantıklı olabilir
# Ama Render performansı iyiyse 15 dk kalabilir.
scheduler.add_job(func=analyze_market, trigger="interval", minutes=15)
scheduler.start()

# --- WEB ROTA ---
@app.route('/')
def index():
    all_signals = Signal.query.order_by(Signal.timestamp.desc()).limit(100).all()
    return render_template('index.html', signals=all_signals)

if __name__ == '__main__':
    app.run(debug=True)