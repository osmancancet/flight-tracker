"""Basit, sezgisel "al / bekle" fiyat sinyali.

Bu bir tahmin GARANTİSİ değildir; yalnızca kaydedilmiş fiyat geçmişine bakan
şeffaf bir kuraldır. Az veriyle (ör. <5 ölçüm) "belirsiz" döner. Amaç, kullanıcıya
"şu anki fiyat geçmişe göre ucuz mu, pahalı mı, yükseliyor mu" konusunda hızlı bir
sezgi vermektir.
"""
from __future__ import annotations

from typing import List, Tuple

# Sinyal kodları
BUY = "AL"
WAIT = "BEKLE"
NEUTRAL = "NÖTR"
UNKNOWN = "BELİRSİZ"

_EMOJI = {BUY: "🟢", WAIT: "🟡", NEUTRAL: "⚪", UNKNOWN: "⚪"}


def predict(prices: List[float]) -> Tuple[str, str]:
    """(sinyal, gerekçe) döndürür. prices kronolojik (eskiden yeniye)."""
    n = len(prices)
    if n < 5:
        return (UNKNOWN, f"yeterli geçmiş yok (en az 5 ölçüm gerekir, şu an {n})")

    cur = prices[-1]
    mn, mx = min(prices), max(prices)
    avg = sum(prices) / n
    span = mx - mn
    pos = (cur - mn) / span if span > 0 else 0.0  # 0 = en dip, 1 = en tepe

    # Trend: son üçte birin ortalaması vs ilk üçte birin ortalaması
    k = max(1, n // 3)
    early = sum(prices[:k]) / k
    late = sum(prices[-k:]) / k
    rising = late > early * 1.03
    falling = late < early * 0.97

    if cur <= mn * 1.02:
        return (BUY, "kayıtlı en düşük fiyata çok yakın")
    if pos <= 0.25 and cur < avg:
        return (BUY, "geçmişe göre dip bölgede ve ortalamanın altında")
    if pos >= 0.75:
        return (WAIT, "geçmişe göre yüksek bölgede")
    if rising:
        return (WAIT, "fiyat yükseliş eğiliminde")
    if falling:
        return (NEUTRAL, "fiyat düşüş eğiliminde, biraz daha izlenebilir")
    return (NEUTRAL, "ortalama civarında, belirgin sinyal yok")


def signal_line(prices: List[float]) -> str:
    """Bildirim/gecmis için tek satırlık biçimli sinyal."""
    sig, reason = predict(prices)
    return f"{_EMOJI[sig]} <b>{sig}</b> sinyali: {reason}"
