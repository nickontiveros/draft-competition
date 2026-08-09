"""Outbound links to betting platforms and sport-icon mapping.

Kalshi's web app reliably serves series-level pages at
https://kalshi.com/markets/{series_ticker}; deeper per-market URL formats are
undocumented, so we link to the series (the exact market is one click away).
Polymarket event pages are https://polymarket.com/event/{eventSlug}, with the
slug taken from the stored fill payload (Data API) or the cached Gamma event.
"""

from __future__ import annotations


def bet_url(
    platform: str,
    market_key: str,
    fill_raw: dict | None = None,
    meta_raw: dict | None = None,
) -> str | None:
    """Best-effort URL to the market's page on its platform; None if unknown.

    Must never raise: it runs on every feed/ledger row, including mock data
    and rows whose raw payloads predate the current schema.
    """
    try:
        if platform == "kalshi":
            series = (market_key or "").split("-")[0].strip().lower()
            if series:
                return f"https://kalshi.com/markets/{series}"
            return None
        if platform == "polymarket":
            slug = ""
            if isinstance(fill_raw, dict):
                slug = fill_raw.get("eventSlug") or ""
            if not slug and isinstance(meta_raw, dict):
                slug = meta_raw.get("slug") or ""
            if slug and isinstance(slug, str):
                return f"https://polymarket.com/event/{slug}"
            return None
    except Exception:  # noqa: BLE001 - links are decorative, never fatal
        return None
    return None


# Ordered: first match wins. "soccer" must precede "football" so European
# "Soccer/Football" style labels don't render as gridiron.
_SPORT_SLUGS = [
    ("soccer", "soccer"),
    ("tennis", "tennis"),
    ("baseball", "baseball"),
    ("softball", "baseball"),
    ("golf", "golf"),
    ("basketball", "basketball"),
    ("hockey", "hockey"),
    ("mma", "mma"),
    ("ufc", "mma"),
    ("boxing", "mma"),
    ("football", "football"),
    ("nfl", "football"),
]


def sport_slug(category: str) -> str:
    """Map a category label ("Tennis", "Pro Football", "Economics") to one of
    the icon slugs defined in templates/_icons.html; "generic" when unknown."""
    label = (category or "").lower()
    for needle, slug in _SPORT_SLUGS:
        if needle in label:
            return slug
    return "generic"
