# ✈️ Ucuz Uçak Bileti Avcısı (Flight Tracker Engine)

Hedef fiyatın altına düşen uçak biletlerini 7/24 arka planda takip eden, Telegram
üzerinden yönetilen **tam asenkron** bir Python sistemi. Fiyatlar Google Flights'tan
Playwright ile çekilir; rotalar dinamik olarak Telegram komutlarıyla yönetilir.

## Özellikler
- 🤖 **Telegram ile kontrol** — rotaları komutla ekle/sil/listele/duraklat, inline butonlarla yönet.
- ✈️ **Tek yön & gidiş-dönüş** — dönüş tarihi opsiyonel.
- 👥 **Çoklu yolcu & kabin** — `pax=2 cabin=business` bayraklarıyla yolcu sayısı ve kabin sınıfı.
- 📅 **Esnek tarih (±gün)** — `flex=3` ile pencere içindeki en ucuz günü bulur, bildirimde belirtir.
- 🗓️ **Tarih aralığı & sabit dönüş** — gidiş/dönüş `10-07-2026..15-07-2026` aralığı girilebilir;
  ayrıca **sabit dönüş tarihi** (gidiş aralık + tek dönüş günü) veya **sabit süre** (`nights=7` →
  dönüş = gidiş + 7 gece) ile aralıktaki en ucuz haftayı bulur.
- 🖼️ **Fiyat grafiği (PNG)** — `/grafik <id>` ile fiyat geçmişini görsel çizgi grafiği olarak gönderir.
- ⏱️ **10 dk tarama + pencere özeti** — varsayılan her 10 dakikada tarar; eşik altı bildirimde
  son **1 / 6 / 24 saatin en ucuz** fiyatını da gösterir (`CHECK_INTERVAL_MIN` ile ayarlanır).
- 🛫 **Çoklu havalimanı** — kalkış/varış virgülle birden çok olabilir (`IST,SAW,ADB,ESB BEG`);
  tüm kombinasyonlar taranıp en ucuz havalimanı seçilir. `near=1` ile bir havalimanı metro
  grubuna genişler (IST → IST+SAW). Bildirimde kazanan kalkış→varış belirtilir.
- 🌍 **Hedef grupları** — `BALKAN` anahtarı vizesiz Balkan ülkelerinin tümüne genişler
  (Belgrad, Saraybosna, Podgorica, Tivat, Tiran, Üsküp, Priştine); tek rota ile hepsi taranır.
- 🔁 **Aktarmalı + aktarmasız** — varsayılan tüm uçuşlar (aktarmalı dahil) değerlendirilir ve
  aktarma bilgisi bildirimde gösterilir; `direct=1` ile yalnızca aktarmasız seçilebilir.
- 🔮 **Al/Bekle sinyali** — fiyat geçmişine bakan sezgisel sinyal (`/gecmis` ve bildirimlerde):
  güncel fiyat dip bölgedeyse 🟢 AL, tepe/yükselişteyse 🟡 BEKLE. (Garanti değil; şeffaf kural.)
- 💱 **Satış noktası (POS) karşılaştırması** — `/karsilastir <id> [TR,GB,DE]` ile aynı uçuşu farklı
  ülke satış noktalarında sorgular; `curr=TRY` sabit tutularak hepsi doğrudan TL cinsinden
  karşılaştırılır (döviz çevrimi yok). `pos=GB,DE` bayrağıyla rotaya eklenirse, eşik bildirimine
  daha ucuz POS varsa otomatik satır eklenir. Not: çoğu rotada POS farkı yoktur; fark olan
  güzergahlarda tasarruf ortaya çıkar. Gerçek ödemede kart döviz komisyonu olabilir.
- 🗄️ **SQLite + analitik** — fiyat geçmişi, `/gecmis` ile min/ort/maks + mini grafik (sparkline).
- 📉 **Ani düşüş uyarısı** — fiyat hedefin üstünde olsa bile ani %X düşüşte haber verir (opsiyonel).
- 🚦 **Bildirim dedupe** — eşik ve düşüş bildirimleri ayrı; aynı/daha yüksek fiyatta tekrar atılmaz.
- 🕵️ **Anti-ban** — rastgele user-agent/viewport, `navigator.webdriver` gizleme, istek jitter'ı, proxy altyapısı.
- 🔁 **Tam asenkron** — bot + tarama + sağlık kontrolü tek event loop'ta; bildirimler fire-and-forget.
- 🛡️ **Dayanıklı** — retry/backoff, tarayıcı çökerse otomatik yeniden başlatma, rota bazında hata izolasyonu,
  üst üste başarısızlıkta sahibine uyarı; DOM değişirse `debug/` klasörüne ekran görüntüsü+HTML dökümü.

## Mimari
| Dosya | Görev |
|-------|-------|
| `config.py` | `.env` yükleme, ayarlar |
| `logger.py` | konsol + döner dosya logu |
| `models.py` | `Route`, `FlightResult` |
| `database.py` | aiosqlite CRUD + fiyat geçmişi + dedupe |
| `scraper.py` | Playwright Google Flights kazıma + anti-ban |
| `notifier.py` | Telegram fire-and-forget bildirim |
| `predictor.py` | al/bekle fiyat sinyali (sezgisel) |
| `charts.py` | fiyat geçmişi PNG grafiği (matplotlib) |
| `bot.py` | Telegram komut handler'ları |
| `tracker.py` | periyodik tarama döngüsü |
| `main.py` | her şeyi birleştiren giriş noktası |

## Kurulum
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Yapılandırma
```bash
cp .env.example .env
```
`.env` içinde en az `TELEGRAM_BOT_TOKEN` doldurulmalı:
1. Telegram'da **@BotFather**'a `/newbot` yaz, bir bot oluştur, verilen token'ı kopyala.
2. `.env` → `TELEGRAM_BOT_TOKEN=...`

Diğer ayarlar (opsiyonel, hepsi `.env.example`'da açıklamalı):
`CHECK_INTERVAL_MIN`, `REQUEST_JITTER_MIN/MAX`, `HEADLESS`, `PROXY`, `DB_PATH`,
`DROP_ALERT_PCT` (ani düşüş %), `SCRAPER_MAX_RETRIES`, `DEBUG_DUMP`, `HEALTH_CHECK_MIN`.

## Çalıştırma
```bash
python main.py
```
Bot açıldıktan sonra Telegram'da botuna yaz:

| Komut | Açıklama |
|-------|----------|
| `/start` veya `/yardim` | tanıtım + komut listesi |
| `/rota_ekle IST LON 15-08-2026 3000` | tek yön: kalkış varış tarih hedef_fiyat |
| `/rota_ekle IST LON 15-08-2026 22-08-2026 5000` | gidiş-dönüş (dönüş tarihi eklenir) |
| `/rota_ekle IST,SAW,ADB,ESB BEG 15-08-2026 3000` | çoklu kalkış havalimanı |
| `/rota_ekle IST BEG 10-07-2026..15-07-2026 25-07-2026 5000` | gidiş aralık + sabit dönüş |
| `/rota_ekle IST BEG 01-07-2026..31-07-2026 5000 nights=7` | sabit süre (7 gece) |
| `/rota_ekle IST BALKAN 01-07-2026..28-07-2026 4000 nights=7` | tüm vizesiz Balkan ülkeleri |
| `/rota_ekle IST LON 15-08-2026 3000 pax=2 cabin=business flex=3` | bayraklarla |
| `/rotalar` | rotaların (Sil / Duraklat-Devam butonlarıyla) |
| `/sil <id>` | bir rotayı sil |
| `/duraklat <id>` · `/devam <id>` | rotayı geçici durdur / tekrar aktif et |
| `/esik <id> <fiyat>` | hedef fiyatı güncelle |
| `/gecmis <id>` | fiyat geçmişi: min/ort/maks + mini grafik |
| `/grafik <id>` | fiyat geçmişi PNG çizgi grafiği |
| `/durum` | sistem durumu / son tarama |

**İsteğe bağlı bayraklar** (`/rota_ekle` sonuna, herhangi bir sırada):
`pax=2` (yolcu 1-9) · `cabin=economy|premium|business|first` · `flex=3` (±0-7 gün) ·
`nights=7` (sabit süre) · `pos=GB,DE` (satış noktası) · `near=1` (metro havalimanları) ·
`direct=1` (yalnızca aktarmasız). Varış için `BALKAN` grubu kullanılabilir.
Türkçe karşılıklar da geçerli: `yolcu=2 kabin=business esnek=3`.

Fiyat hedefin altına düştüğünde bot sana otomatik bildirim gönderir. `DROP_ALERT_PCT`
ayarlanırsa, hedefin üstünde olsa bile ani fiyat düşüşlerinde de uyarır.

## Tek rota için scraper testi (Telegram'sız)
```bash
python scraper.py IST LON 2026-08-15              # tek yön
python scraper.py IST LON 2026-08-15 2026-08-22   # gidiş-dönüş
```
Fiyat bulunamazsa ve `DEBUG_DUMP=true` ise `debug/` klasörüne ekran görüntüsü + HTML düşer
(selector onarımı için).

## Testler
```bash
pip install -r requirements-dev.txt
pytest
```
Parser, veritabanı (CRUD/migration/istatistik/dedupe) ve tracker döngüsü (eşik, düşüş,
hata raporu) gerçek Playwright/Telegram olmadan, sahte bileşenlerle test edilir.

## Notlar
- **Google Flights kırılganlığı:** Sayfa yapısı değişirse parse bozulabilir. Seçiciler
  `scraper.py` içinde izole ve yedeklidir; `_wait_for_results` / `_extract_cheapest`
  güncellenerek kolayca onarılır. Anti-bot tetiklenirse `HEADLESS=false` ile gözlemle,
  gerekirse `PROXY` tanımla.
- **Hukuki/etik:** Yalnızca makul aralıklarla ve jitter ile tarama yapar; sitelere aşırı
  yük bindirmez. Kullanım koşullarına ve yerel mevzuata uygun kullanım kullanıcının sorumluluğundadır.
