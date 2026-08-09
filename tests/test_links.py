from app.links import bet_url, sport_slug


def test_kalshi_series_url_from_ticker():
    assert (
        bet_url("kalshi", "KXNFLGAME-25SEP04DALPHI-PHI")
        == "https://kalshi.com/markets/kxnflgame"
    )
    assert bet_url("kalshi", "FED-25SEP-CUT") == "https://kalshi.com/markets/fed"


def test_kalshi_mock_and_empty_keys_are_safe():
    assert bet_url("kalshi", "mkt-fed") == "https://kalshi.com/markets/mkt"
    assert bet_url("kalshi", "") is None
    assert bet_url("kalshi", "   ") is None


def test_polymarket_event_slug_sources():
    assert (
        bet_url("polymarket", "0xcond", fill_raw={"eventSlug": "mlb-nyy-bos"})
        == "https://polymarket.com/event/mlb-nyy-bos"
    )
    # Falls back to the cached Gamma event slug.
    assert (
        bet_url("polymarket", "0xcond", fill_raw={"mock": True}, meta_raw={"slug": "us-open"})
        == "https://polymarket.com/event/us-open"
    )
    assert bet_url("polymarket", "0xcond") is None
    # Malformed payloads must not raise.
    assert bet_url("polymarket", "0xcond", fill_raw={"eventSlug": None}, meta_raw={"slug": 7}) is None


def test_unknown_platform():
    assert bet_url("robinhood", "XYZ") is None


def test_sport_slug_mapping():
    assert sport_slug("Tennis") == "tennis"
    assert sport_slug("Baseball") == "baseball"
    assert sport_slug("Golf") == "golf"
    assert sport_slug("Basketball") == "basketball"
    assert sport_slug("Hockey") == "hockey"
    assert sport_slug("UFC") == "mma"
    assert sport_slug("Pro Football") == "football"
    # Precedence: combined labels stay soccer, not gridiron.
    assert sport_slug("Soccer/Football") == "soccer"
    assert sport_slug("Soccer") == "soccer"
    for junk in ("Economics", "Politics", "Other", "", None):
        assert sport_slug(junk) == "generic"
