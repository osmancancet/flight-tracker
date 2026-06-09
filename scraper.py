"""Google Flights fiyat kazıyıcı (Playwright, async) — sağlamlaştırılmış sürüm.

Projenin en kırılgan parçası: Google Flights istemci tarafında ağır JS ile
çizilir ve DOM yapısı sık değişebilir. Bu yüzden:
  * Fiyat/havayolu/saat ayrı, yedekli (fallback) seçicilerle parse edilir.
  * Bir seçici tutmazsa uyarı loglanır, çökme olmaz (None döner).
  * Tek tarayıcı örneği yeniden kullanılır (bellek stabilitesi); her arama
    için izole bir context açılıp kapatılır (temiz fingerprint + sızıntı yok).

Sağlamlık (deepening):
  * Üstel backoff ile tekrar deneme (max_retries).
  * Tarayıcı çökerse otomatik yeniden başlatma (`_ensure_browser` + `healthy`).
  * Parse başarısızlığında debug/ klasörüne ekran görüntüsü + HTML dökme.
  * Birden fazla uçuş çıkarma (`fetch_flights`); `fetch_cheapest` en ucuzu döner.

Anti-ban: rastgele user-agent + viewport, navigator.webdriver gizleme,
locale/timezone başlıkları, çağrı bazında jitter (tracker tarafında uygulanır).
Proxy desteği için altyapı bırakılmıştır (Settings.proxy).

Standalone test:
    python scraper.py IST LON 2026-08-15
    python scraper.py IST LON 2026-08-15 2026-08-22   # gidiş-dönüş
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import sys
import urllib.parse
from typing import List, Optional, Tuple

from playwright.async_api import (
    Browser,
    Playwright,
    TimeoutError as PWTimeoutError,
    async_playwright,
)

from logger import get_logger
from models import FlightResult

log = get_logger("scraper")

_DEBUG_DIR = "debug"

_USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

_VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1920, "height": 1080},
]

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['tr-TR', 'tr', 'en-US']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
"""

_PRICE_RE = re.compile(r"(?:₺|TL|TRY|\$|€|£)?\s*([\d][\d.,\s]*\d|\d)\s*(?:₺|TL|TRY|\$|€|£)?")

# Satır metninden FİYATI ayıklamak için para-birimi-çapalı kalıp. Bir uçuş satırında
# saat ("13:50"), süre, CO2 gibi başka sayılar da olduğundan, fiyatı yalnızca para
# birimi sembolü/kodu ile bitişik olan sayıdan alırız (yoksa "13:50" → 13 olurdu).
_CURRENCY_RE = re.compile(r"(?:₺|\$|€|£)\s?([\d.,]+)|([\d.,]+)\s?(?:TL|TRY)")


def _rotate(seq: List, index: int):
    return seq[index % len(seq)]


def _extract_currency_price(text: str) -> Optional[float]:
    """Metindeki para-birimi-çapalı en düşük fiyatı döndürür (yoksa None)."""
    best: Optional[float] = None
    for m in _CURRENCY_RE.finditer(text):
        val = _parse_price(m.group(1) or m.group(2) or "")
        if val and val > 0 and (best is None or val < best):
            best = val
    return best


def _parse_price(text: str) -> Optional[float]:
    """'₺2.450' / '2,450 TL' gibi metinden sayısal değeri çıkarır."""
    if not text:
        return None
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(" ", "")
    if "." in raw and "," in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    else:
        raw = raw.replace(",", "").replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


class FlightScraper:
    """Tek tarayıcı örneğini yöneten, yeniden kullanılabilir, kendini onaran kazıyıcı."""

    def __init__(self, headless: bool = True, proxy: Optional[str] = None,
                 nav_timeout_ms: int = 45000, max_retries: int = 2,
                 debug_dump: bool = False):
        self._headless = headless
        self._proxy = proxy
        self._nav_timeout = nav_timeout_ms
        self._max_retries = max_retries
        self._debug_dump = debug_dump
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._req_count = 0
        self._lock = asyncio.Lock()  # yeniden başlatmayı serileştir

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> None:
        await self._ensure_browser()

    async def _ensure_browser(self) -> None:
        """Tarayıcı yoksa veya bağlantısı kopmuşsa (çökme) başlat/yeniden başlat."""
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return
            # Eski örneği temizle.
            if self._browser is not None:
                log.warning("Tarayıcı bağlantısı kopmuş, yeniden başlatılıyor.")
                try:
                    await self._browser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._browser = None
            if self._pw is None:
                self._pw = await async_playwright().start()
            launch_kwargs: dict = {
                "headless": self._headless,
                "args": [
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            }
            if self._proxy:
                launch_kwargs["proxy"] = {"server": self._proxy}
            self._browser = await self._pw.chromium.launch(**launch_kwargs)
            log.info("Tarayıcı başlatıldı (headless=%s, proxy=%s)", self._headless, bool(self._proxy))

    def healthy(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def health_check(self) -> bool:
        """Tarayıcı canlı mı? Değilse yeniden başlat. Sağlıklıysa True."""
        if self.healthy():
            return True
        log.warning("Sağlık kontrolü: tarayıcı sağlıksız, onarılıyor.")
        try:
            await self._ensure_browser()
            return self.healthy()
        except Exception as exc:  # noqa: BLE001
            log.warning("Tarayıcı onarılamadı: %s", exc)
            return False

    async def close(self) -> None:
        async with self._lock:
            try:
                if self._browser:
                    await self._browser.close()
            finally:
                if self._pw:
                    await self._pw.stop()
                self._browser = None
                self._pw = None
                log.info("Tarayıcı kapatıldı")

    # --------------------------------------------------------------- URL
    @staticmethod
    def build_url(origin: str, dest: str, date: str, return_date: Optional[str] = None,
                  passengers: int = 1, cabin: str = "economy",
                  gl: Optional[str] = None, curr: str = "TRY", hl: str = "tr") -> str:
        """Google Flights arama URL'i. date / return_date = 'YYYY-MM-DD'.

        Yolcu sayısı ve kabin sınıfı doğal dil sorgusuna gömülür. `gl` satış noktası
        (ülke) kodudur; `curr` görüntü para birimi. POS karşılaştırmasında gl değişir,
        curr=TRY sabit kalır → tüm fiyatlar doğrudan TL cinsinden karşılaştırılır."""
        from models import CABINS  # döngüsel importu önlemek için yerel
        parts = [f"Flights from {origin} to {dest} on {date}"]
        if return_date:
            parts.append(f"returning {return_date}")
        else:
            parts.append("one way")
        if passengers and passengers > 1:
            parts.append(f"for {passengers} adults")
        cabin_phrase = CABINS.get(cabin)
        if cabin_phrase and cabin != "economy":
            parts.append(f"in {cabin_phrase}")
        query = " ".join(parts)
        params = {"q": query, "hl": hl, "curr": curr}
        if gl:
            params["gl"] = gl
        return "https://www.google.com/travel/flights?" + urllib.parse.urlencode(params)

    # ------------------------------------------------------------- public
    async def fetch_cheapest(self, origin: str, dest: str, date: str,
                             return_date: Optional[str] = None,
                             passengers: int = 1, cabin: str = "economy",
                             gl: Optional[str] = None, curr: str = "TRY",
                             nonstop_only: bool = False) -> Optional[FlightResult]:
        """En ucuz uçuşu döndürür (yoksa None). Asla exception sızdırmaz.

        Varsayılan: aktarmalı + aktarmasız tüm uçuşlar dikkate alınır.
        nonstop_only=True ise yalnızca aktarmasız uçuşlar arasından seçer."""
        flights = await self.fetch_flights(origin, dest, date, return_date, passengers, cabin, gl, curr)
        if nonstop_only:
            flights = [f for f in flights if f.stops == "Aktarmasız"]
        if not flights:
            return None
        return min(flights, key=lambda f: f.price)

    async def fetch_flights(self, origin: str, dest: str, date: str,
                            return_date: Optional[str] = None,
                            passengers: int = 1, cabin: str = "economy",
                            gl: Optional[str] = None, curr: str = "TRY") -> List[FlightResult]:
        """Bulunan uçuşların listesi (en fazla ~20). Tekrar denemeli, kendini onaran.

        Hata durumunda boş liste döner — tracker döngüsünün kesilmemesi kritik."""
        label = f"{origin}→{dest} {date}" + (f"/{return_date}" if return_date else "") + (f" [{gl}]" if gl else "")
        for attempt in range(self._max_retries + 1):
            try:
                await self._ensure_browser()
                flights = await self._scrape_once(origin, dest, date, return_date, passengers, cabin, gl, curr)
                if flights:
                    return flights
                # Sonuç boş: yeniden dene (geçici yüklenme sorunu olabilir).
                log.info("%s: sonuç boş (deneme %d/%d)", label, attempt + 1, self._max_retries + 1)
            except PWTimeoutError:
                log.warning("%s: zaman aşımı (deneme %d/%d)", label, attempt + 1, self._max_retries + 1)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: kazıma hatası (deneme %d/%d): %s", label, attempt + 1, self._max_retries + 1, exc)
                # Tarayıcı çökmüş olabilir; bir sonraki denemede _ensure_browser onaracak.
            if attempt < self._max_retries:
                backoff = 2.0 * (attempt + 1)
                await asyncio.sleep(backoff)
        log.warning("%s: tüm denemeler başarısız.", label)
        return []

    async def compare_pos(self, route, codes: List[str],
                          jitter: Tuple[float, float] = (1.5, 4.0)) -> List[Tuple[str, Optional[float]]]:
        """Aynı uçuşu birden çok satış noktasında (ülke) curr=TRY ile sorgular ve
        (ülke_kodu, TL_fiyat | None) listesini fiyata göre artan döndürür.

        Anti-ban: POS'lar arasında rastgele jitter. Asla exception sızdırmaz."""
        results: List[Tuple[str, Optional[float]]] = []
        o, d = route.primary_origin(), route.primary_dest()
        for j, code in enumerate(codes):
            r = await self.fetch_cheapest(
                o, d, route.date, route.return_date,
                route.passengers, route.cabin, gl=code, curr="TRY",
            )
            results.append((code, r.price if r else None))
            if j < len(codes) - 1:
                await asyncio.sleep(random.uniform(*jitter))
        results.sort(key=lambda t: (t[1] is None, t[1] if t[1] is not None else 0))
        return results

    # ------------------------------------------------------------ internals
    async def _scrape_once(self, origin, dest, date, return_date,
                           passengers=1, cabin="economy",
                           gl=None, curr="TRY") -> List[FlightResult]:
        url = self.build_url(origin, dest, date, return_date, passengers, cabin, gl, curr)
        context = None
        try:
            self._req_count += 1
            context = await self._browser.new_context(
                user_agent=_rotate(_USER_AGENTS, self._req_count),
                viewport=_rotate(_VIEWPORTS, self._req_count),
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
            )
            await context.add_init_script(_STEALTH_JS)
            page = await context.new_page()
            page.set_default_timeout(self._nav_timeout)

            await page.goto(url, wait_until="domcontentloaded", timeout=self._nav_timeout)
            await self._maybe_accept_consent(page)
            await self._wait_for_results(page)

            flights = await self._extract_flights(page, url)
            if not flights and self._debug_dump:
                await self._dump_debug(page, origin, dest, date)
            return flights
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _maybe_accept_consent(self, page) -> None:
        selectors = [
            "button[aria-label*='Accept']",
            "button[aria-label*='Kabul']",
            "button:has-text('Accept all')",
            "button:has-text('Tümünü kabul et')",
            "form[action*='consent'] button",
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=3000)
                    await page.wait_for_timeout(1000)
                    return
            except Exception:  # noqa: BLE001
                continue

    async def _wait_for_results(self, page) -> None:
        candidates = [
            "li.pIav2d",
            "ul.Rk10dc li",
            "[role='list'] [role='listitem']",
            "li[data-test-id]",
        ]
        for sel in candidates:
            try:
                await page.wait_for_selector(sel, timeout=12000, state="attached")
                return
            except PWTimeoutError:
                continue
        await page.wait_for_timeout(4000)

    async def _extract_flights(self, page, url: str) -> List[FlightResult]:
        """Sayfadan uçuşları çıkarır. Yapı değişimine karşı çok katmanlı.

        Satır seçicileri canlı Google Flights DOM'una göre ayarlanmıştır
        (li.pIav2d). Fiyat, satır içindeki para-birimi-çapalı sayıdan alınır;
        böylece kalkış saati ("13:50") yanlışlıkla fiyat sanılmaz."""
        row_selectors = [
            "li.pIav2d",
            "ul.Rk10dc li",
            "[role='list'] [role='listitem']",
            "li[data-test-id]",
        ]
        results: List[FlightResult] = []
        for sel in row_selectors:
            try:
                rows = page.locator(sel)
                n = min(await rows.count(), 25)
            except Exception:  # noqa: BLE001
                continue
            for i in range(n):
                try:
                    text = (await rows.nth(i).inner_text(timeout=2000)).strip()
                except Exception:  # noqa: BLE001
                    continue
                price = _extract_currency_price(text)
                if price is None or price <= 0:
                    continue
                results.append(FlightResult(
                    price=price,
                    currency="TRY",
                    airline=self._guess_airline(text),
                    departure_time=self._guess_time(text),
                    stops=self._guess_stops(text),
                    link=url,
                ))
            if results:
                cheapest = min(results, key=lambda f: f.price)
                log.info("%d uçuş bulundu; en ucuz %.0f TRY (seçici: %s)",
                         len(results), cheapest.price, sel)
                return results

        # Yedek: sayfadaki tüm fiyat metinlerini tara.
        try:
            body = await page.inner_text("body", timeout=3000)
            prices = [_parse_price(t) for t in
                      re.findall(r"[₺$€£]\s?[\d.,]+|[\d.,]+\s?(?:TL|TRY)", body)]
            prices = [p for p in prices if p and p > 100]
            if prices:
                log.info("Yedek tarama: %d fiyat, en ucuz %.0f TRY", len(prices), min(prices))
                return [FlightResult(price=p, currency="TRY", link=url) for p in prices]
        except Exception as exc:  # noqa: BLE001
            log.warning("Yedek fiyat taraması başarısız: %s", exc)

        log.warning("Fiyat bulunamadı — DOM yapısı değişmiş olabilir (%s)", url)
        return []

    async def _dump_debug(self, page, origin, dest, date) -> None:
        """Selector onarımı için ekran görüntüsü + HTML kaydet."""
        try:
            os.makedirs(_DEBUG_DIR, exist_ok=True)
            stamp = f"{origin}_{dest}_{date}_{self._req_count}"
            png = os.path.join(_DEBUG_DIR, f"{stamp}.png")
            html = os.path.join(_DEBUG_DIR, f"{stamp}.html")
            await page.screenshot(path=png, full_page=True)
            content = await page.content()
            with open(html, "w", encoding="utf-8") as f:
                f.write(content)
            log.info("Debug dökümü: %s , %s", png, html)
        except Exception as exc:  # noqa: BLE001
            log.warning("Debug dökümü alınamadı: %s", exc)

    @staticmethod
    def _guess_airline(text: str) -> Optional[str]:
        known = [
            "Turkish Airlines", "THY", "Pegasus", "AnadoluJet", "AJet", "SunExpress",
            "Lufthansa", "Wizz Air", "Ryanair", "easyJet", "British Airways",
            "Qatar Airways", "Emirates", "KLM", "Air France",
        ]
        low = text.lower()
        for name in known:
            if name.lower() in low:
                return name
        return None

    @staticmethod
    def _guess_time(text: str) -> Optional[str]:
        m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", text)
        return f"{m.group(1)}:{m.group(2)}" if m else None

    @staticmethod
    def _guess_stops(text: str) -> Optional[str]:
        """Aktarma bilgisi: 'Aktarmasız' / 'N aktarma' (yoksa None)."""
        low = text.lower()
        if "aktarmasız" in low or "aktarmasiz" in low or "nonstop" in low or "direct" in low:
            return "Aktarmasız"
        m = re.search(r"(\d+)\s*(durak|aktarma|stop)", low)
        if m:
            n = m.group(1)
            return "Aktarmasız" if n == "0" else f"{n} aktarma"
        return None

    @staticmethod
    def is_nonstop(result) -> bool:
        return getattr(result, "stops", None) == "Aktarmasız"


async def _standalone(origin: str, dest: str, date: str, return_date: Optional[str]) -> None:
    scraper = FlightScraper(headless=True, debug_dump=True)
    await scraper.start()
    try:
        flights = await scraper.fetch_flights(origin, dest, date, return_date)
        if flights:
            cheapest = min(flights, key=lambda f: f.price)
            trip = f" ↩ {return_date}" if return_date else ""
            print(f"\n✅ {origin}→{dest} {date}{trip}  ({len(flights)} uçuş)")
            print(f"   En ucuz : {cheapest.price:.0f} {cheapest.currency}")
            print(f"   Havayolu: {cheapest.airline or '-'}")
            print(f"   Link    : {cheapest.link}")
        else:
            print(f"\n⚠️  {origin}→{dest} {date}: fiyat bulunamadı (debug/ klasörüne bak).")
    finally:
        await scraper.close()


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        print("Kullanım: python scraper.py <ORIGIN> <DEST> <YYYY-MM-DD> [DÖNÜŞ YYYY-MM-DD]")
        print("Örnek   : python scraper.py IST LON 2026-08-15")
        print("Örnek   : python scraper.py IST LON 2026-08-15 2026-08-22")
        sys.exit(1)
    ret = sys.argv[4] if len(sys.argv) == 5 else None
    asyncio.run(_standalone(sys.argv[1].upper(), sys.argv[2].upper(), sys.argv[3], ret))
