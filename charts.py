"""Fiyat geçmişini PNG çizgi grafiğine render eder (matplotlib, Agg backend).

Render CPU-bağımlı ve bloklayıcıdır; çağıran taraf `asyncio.to_thread` ile
arka planda çalıştırarak event loop'u bekletmemelidir (bkz. bot.cmd_chart).
"""
from __future__ import annotations

import io
from typing import List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")  # başsız (headless) ortam — ekran gerektirmez
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402
from datetime import datetime  # noqa: E402

from logger import get_logger  # noqa: E402

log = get_logger("charts")


def _parse_ts(s: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def render_price_chart(label: str, points: List[Tuple[str, float]],
                       threshold: Optional[float] = None,
                       currency: str = "TRY") -> bytes:
    """(fetched_at, price) noktalarından PNG byte'ları üretir."""
    times = [_parse_ts(t) for t, _ in points]
    prices = [p for _, p in points]
    use_dates = all(t is not None for t in times) and len(times) > 1
    xs = times if use_dates else list(range(len(prices)))

    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=110)
    ax.plot(xs, prices, marker="o", markersize=3, linewidth=1.6, color="#1f77b4")
    ax.fill_between(xs, prices, min(prices), alpha=0.08, color="#1f77b4")

    if threshold is not None:
        ax.axhline(threshold, color="#d62728", linestyle="--", linewidth=1.2,
                   label=f"Hedef {threshold:.0f}")

    # En düşük noktayı işaretle.
    lo_i = min(range(len(prices)), key=lambda i: prices[i])
    ax.annotate(f"{prices[lo_i]:.0f}", (xs[lo_i], prices[lo_i]),
                textcoords="offset points", xytext=(0, 8), ha="center",
                fontsize=9, color="#2ca02c", fontweight="bold")

    ax.set_title(label, fontsize=11, fontweight="bold")
    ax.set_ylabel(currency)
    ax.grid(True, alpha=0.25)
    if threshold is not None:
        ax.legend(fontsize=8, loc="best")

    if use_dates:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
        fig.autofmt_xdate(rotation=30)
    else:
        ax.set_xlabel("ölçüm")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
