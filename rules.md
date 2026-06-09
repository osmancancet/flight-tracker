# Proje: Otonom Uçak Bileti Avcısı ve Bildirim Sistemi (Flight Tracker Engine)

## Felsefe: Vibe Coding & Yüksek Performans
- Sen kıdemli bir yazılım mimarı ve geliştiricisisin.
- Hızlı iterasyon ("vibe coding") felsefesini benimsiyorsun. Uzun uzadıya planlama yapmak yerine, hızlıca çalışan, modüler ve teste hazır kodlar üret.
- Eski veya deprecate olmuş kütüphaneler yerine, en güncel, modern ve performanslı asenkron yapıları kullan.

## Teknoloji Yığını (Tech Stack)
- **Scraping ve Ağ İşlemleri:** Python (`asyncio`, `aiohttp`, gizlilik/anti-bot aşımı için `Playwright` veya `curl_cffi`).
- **Uygulama Mantığı:** Saf asenkron Python.
- **Veri Saklama & Konfigürasyon:** Parametreler ve rotalar için `config.json` veya SQLite.
- **Zamanlama:** Asenkron döngüler veya `APScheduler`.
- **Bildirim ve Kontrol Sistemi:** Telegram Bot API (`python-telegram-bot` kütüphanesi ile).

## Çekirdek Kurallar (Core Directives)

### 1. Mimari ve "Sürekli Çalışma" Prensibi
- Sistem 7/24 kesintisiz ve arka planda çalışacak şekilde tasarlanmalıdır.
- Bloklayan (synchronous) hiçbir ağ veya disk işlemi yazma. Her şey tam asenkron (`async/await`) olmalıdır.
- Memory leak oluşturabilecek yapılardan kaçın; bot günlerce açık kalsa bile bellek tüketimi stabil kalmalıdır.

### 2. Dinamik Rota ve Parametre Yönetimi (Yeni & Kritik)
- Kalkış ve varış noktaları (şehir/ülke IATA kodları), tarihler ve hedef fiyat sınırları (threshold) **kesinlikle koda gömülü (hardcoded) olmamalıdır**.
- Sistem, aranacak rotaları dışarıdan bir kaynaktan (`config.json`, veritabanı veya doğrudan Telegram komutları) okuyacak şekilde modüler tasarlanmalıdır.
- Kullanıcının Telegram üzerinden `/rota_ekle IST LON 15-08-2026 3000` gibi komutlarla arama havuzuna yeni görevler ekleyebileceği veya `/rotalar` ile silebileceği bir çift yönlü iletişim (Command Handler) yapısı kur.

### 3. Anti-Ban ve Gizlilik
- Hedef sitelere acımasızca DDoS atar gibi istek gönderme. İstekleri, aranan rotaların sayısına göre akıllıca sıraya koy.
- İstekler arasına mutlaka `jitter` (rastgele bekleme süreleri) ekle.
- Tarayıcı fingerprint'lerini gizlemek için gerekli header'ları dinamik olarak ayarla. Gerekirse proxy desteği eklenebilecek bir altyapı bırak.

### 4. Telegram Bildirim ve Kontrol Yönetimi
- Bildirim gönderme işlemi (`send_telegram_message`) ana scraping döngüsünü bekletmemelidir ("Fire-and-forget").
- Sadece fiyatı belirlenen hedefin altına düşen biletler için bildirim at. Biletin linkini, havayolunu, kalkış-varış saatini ve fiyatını net bir formatta gönder.
- Telegram API'sinin "Rate Limit" sınırlarına saygı duy.

### 5. Hata Yönetimi (Error Handling & Resilience)
- Hedef sitenin yapısı değiştiğinde, anlık bağlantı koptuğunda veya Telegram API yanıt vermediğinde program **asla çökmemelidir**.
- Kapsamlı `try-except` blokları kullan. Hata durumunda logla ve bir sonraki rotayı/döngüyü kontrol etmeye devam et.

### 6. İletişim ve Kod Çıktı Formatı
- Bana ne yapacağımı uzun uzun anlatma, doğrudan çözümü ve modüler kodu sun.
- Kod parçacıklarını eksik veya "kalanını sen tamamla" (placeholder) şeklinde verme. Kopyala-yapıştır yapıp çalıştırabileceğim tam bloklar halinde ver.
- Ortam değişkenleri için `.env` yapısını zorunlu tut (Telegram Token vb. gizli kalsın).