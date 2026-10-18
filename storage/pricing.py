"""Versioned official time bands; a call keeps its start-time rate snapshot."""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal

from core.configuration import PRICING

BEIJING = timezone(timedelta(hours=8))


def utc_now():
    return datetime.now(UTC)


def pricing_band(settings, at=None):
    at = at or utc_now()
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Pricing requires a timezone-aware timestamp")
    if settings.pricing_mode != "auto":
        return settings.pricing_mode
    local = at.astimezone(BEIJING)
    holiday = any(start <= local.date() <= end for start, end in PRICING.holiday_ranges)
    # Provider specifies Mon-Fri, so make-up Saturdays remain off-peak.
    peak = (
        not holiday
        and local.weekday() in PRICING.peak_weekdays
        and any(
            time.fromisoformat(start) <= local.time() < time.fromisoformat(end)
            for start, end in PRICING.peak_windows
        )
    )
    return "peak" if peak else "off_peak"


@dataclass(frozen=True)
class PriceSnapshot:
    band: str
    rates: tuple[Decimal, Decimal, Decimal]
    basis: str


def price_snapshot(settings, model, at=None):
    at = at or utc_now()
    rates = settings.price_rates_cny.get(model)
    if rates is None:
        return None
    band = pricing_band(settings, at)
    multiplier = Decimal(1) if band == "peak" else Decimal(str(PRICING.off_peak_multiplier))
    selected = tuple(Decimal(str(rate)) * multiplier for rate in rates)
    timestamp = at.astimezone(UTC).isoformat(timespec="milliseconds")
    basis = (
        f"estimate:{PRICING.version}:{band}:CNY/1M;request_start={timestamp};"
        f"rates={','.join(str(rate) for rate in selected)};mode={settings.pricing_mode}"
    )
    return PriceSnapshot(band, selected, basis)


def pricing_info(settings):
    at = utc_now()
    return {
        "mode": settings.pricing_mode,
        "current_band": pricing_band(settings, at),
        "peak_weekdays": PRICING.peak_weekdays,
        "peak_windows": PRICING.peak_windows,
        "off_peak_multiplier": str(PRICING.off_peak_multiplier),
        "holiday_calendar_years": PRICING.holiday_calendar_years,
        "holiday_calendar_complete": at.astimezone(BEIJING).year in PRICING.holiday_calendar_years,
        "version": PRICING.version,
    }
