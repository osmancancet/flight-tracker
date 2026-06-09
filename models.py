"""Sistem genelinde paylaşılan veri yapıları (dataclass)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def _human(iso: Optional[str]) -> Optional[str]:
    """YYYY-MM-DD -> DD-MM-YYYY. None/biçimsiz girdide olduğu gibi döner."""
    if not iso:
        return None
    try:
        y, m, d = iso.split("-")
        return f"{d}-{m}-{y}"
    except ValueError:
        return iso


# Kabul edilen kabin sınıfları ve doğal dil karşılıkları (Google Flights sorgusu için)
CABINS = {
    "economy": "economy",
    "premium": "premium economy",
    "business": "business class",
    "first": "first class",
}
CABIN_LABEL_TR = {
    "economy": "ekonomi",
    "premium": "premium ekonomi",
    "business": "business",
    "first": "first",
}

# Satış noktası (point-of-sale) kataloğu: Google Flights `gl` ülke kodu -> TR etiket.
# Aynı uçuş ülkeye göre farklı fiyatlanabildiğinden, fiyatı curr=TRY sabit tutarak
# her POS'ta sorgulayıp doğrudan TL cinsinden karşılaştırırız (döviz çevrimi yok).
# Sadece fiyat ayrıştırıcının tanıdığı para birimi sembollerini kullanan ülkeler tutulur
# (TRY ₺ sabit görüntü olduğundan hepsi uyumlu).
POS_CATALOG = {
    "TR": "Türkiye",
    "GB": "İngiltere",
    "DE": "Almanya",
    "FR": "Fransa",
    "NL": "Hollanda",
    "ES": "İspanya",
    "US": "ABD",
    "AE": "BAE",
}
DEFAULT_POS = ["TR", "GB", "DE", "US"]

# Şehir/metro havalimanı grupları: near=1 ile bir havalimanı tüm metro grubuna genişler.
# Örn. İstanbul = IST + SAW (Sabiha Gökçen) — Pegasus SAW'dan sık sık daha ucuzdur.
METRO_GROUPS = {
    "IST": ["IST", "SAW"],
    "SAW": ["IST", "SAW"],
    "LON": ["LHR", "LGW", "STN", "LTN"],
    "LHR": ["LHR", "LGW", "STN", "LTN"],
    "PAR": ["CDG", "ORY"],
    "MOW": ["SVO", "DME", "VKO"],
    "NYC": ["JFK", "EWR", "LGA"],
    "MIL": ["MXP", "LIN", "BGY"],
}
MAX_AIRPORT_COMBOS = 12  # origins × dests üst sınırı (anti-ban: istek patlamasını önler)
MAX_DATE_PAIRS = 20      # tarih aralığında üretilecek (gidiş,dönüş) çifti üst sınırı
MAX_SCAN_TASKS = 36      # bir rotada tek taramada toplam istek (kombo × tarih) üst sınırı

# Hedef grupları: bir anahtar kelime birden çok varış havalimanına genişler.
# BALKAN = Türk pasaportuyla VİZESİZ gidilebilen Balkan ülkelerinin ana havalimanları.
# (Sırbistan, Bosna-Hersek, Karadağ, Arnavutluk, K. Makedonya, Kosova)
DEST_GROUPS = {
    "BALKAN": ["BEG", "SJJ", "TGD", "TIV", "TIA", "SKP", "PRN"],
}
# İnsan-okunur etiketler (bildirim/yardım için)
AIRPORT_NAMES = {
    "BEG": "Belgrad", "SJJ": "Saraybosna", "TGD": "Podgorica", "TIV": "Tivat",
    "TIA": "Tiran", "SKP": "Üsküp", "PRN": "Priştine",
    "IST": "İstanbul", "SAW": "İstanbul-SAW", "ADB": "İzmir", "ESB": "Ankara",
}


def expand_airport(code: str) -> list:
    """near=1 için: havalimanını metro grubuna genişletir (yoksa kendisi)."""
    return list(METRO_GROUPS.get(code, [code]))


@dataclass
class Route:
    """Takip edilen tek bir uçuş araması (tek yön veya gidiş-dönüş)."""
    id: Optional[int]
    chat_id: int
    origin: str            # IATA veya virgüllü liste, örn. "IST" / "IST,SAW"
    dest: str              # IATA veya virgüllü liste, örn. "BEG"
    date: str              # gidiş, ISO biçim "YYYY-MM-DD"
    threshold: float       # bu fiyatın altına düşünce bildirim at
    currency: str = "TRY"
    active: bool = True
    return_date: Optional[str] = None  # dolu ise gidiş-dönüş (dönüş aralığının başı), ISO
    date_end: Optional[str] = None     # dolu ise gidiş bir ARALIK: date..date_end
    return_date_end: Optional[str] = None  # dolu ise dönüş bir ARALIK: return_date..return_date_end
    passengers: int = 1                # yetişkin yolcu sayısı
    cabin: str = "economy"             # CABINS anahtarlarından biri
    flex_days: int = 0                 # ±gün esnek tarih penceresi (0 = kapalı)
    nights: int = 0                    # sabit süre: dönüş = gidiş + nights gece (0 = kapalı)
    direct_only: bool = False          # True ise yalnızca aktarmasız uçuşlar dikkate alınır
    pos: Optional[str] = None          # virgüllü POS kodları, örn "GB,DE" (bildirimde karşılaştır)

    def pos_codes(self) -> list:
        return [c for c in (self.pos or "").split(",") if c] if self.pos else []

    def origin_list(self) -> list:
        return [c for c in self.origin.split(",") if c]

    def dest_list(self) -> list:
        return [c for c in self.dest.split(",") if c]

    def primary_origin(self) -> str:
        return self.origin_list()[0]

    def primary_dest(self) -> str:
        return self.dest_list()[0]

    @property
    def is_round_trip(self) -> bool:
        return bool(self.return_date) or self.nights > 0

    @property
    def has_date_range(self) -> bool:
        return bool(self.date_end or self.return_date_end)

    def human_date(self) -> str:
        return _human(self.date) or self.date

    @staticmethod
    def _range_disp(start: Optional[str], end: Optional[str]) -> str:
        if not start:
            return ""
        if end and end != start:
            return f"{_human(start)}…{_human(end)}"
        return _human(start)

    def _airports_label(self) -> str:
        o = "/".join(self.origin_list())
        d = "/".join(self.dest_list())
        return o, d

    def label(self) -> str:
        o, d = self._airports_label()
        dep = self._range_disp(self.date, self.date_end)
        if self.nights > 0:
            base = f"{o}⇄{d} {dep} ({self.nights} gece)"
        elif self.is_round_trip:
            ret = self._range_disp(self.return_date, self.return_date_end)
            base = f"{o}⇄{d} {dep} → {ret}"
        else:
            base = f"{o}→{d} {dep}"
        extras = []
        if self.flex_days:
            extras.append(f"±{self.flex_days}g")
        if self.passengers != 1:
            extras.append(f"{self.passengers} yolcu")
        if self.cabin != "economy":
            extras.append(CABIN_LABEL_TR.get(self.cabin, self.cabin))
        return base + (("  · " + " · ".join(extras)) if extras else "")


@dataclass
class FlightResult:
    """Scraper'ın bulduğu en ucuz uçuşun özeti."""
    price: float
    currency: str = "TRY"
    airline: Optional[str] = None
    departure_time: Optional[str] = None
    arrival_time: Optional[str] = None
    duration: Optional[str] = None
    stops: Optional[str] = None
    link: Optional[str] = None
    date: Optional[str] = None          # esnek/aralık tarama: en ucuzun gidiş tarihi (ISO)
    return_date: Optional[str] = None   # aralık tarama: en ucuzun dönüş tarihi (ISO)
    origin: Optional[str] = None        # çoklu havalimanı: en ucuzun kalkış IATA'sı
    dest: Optional[str] = None          # çoklu havalimanı: en ucuzun varış IATA'sı
