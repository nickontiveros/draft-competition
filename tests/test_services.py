import pytest
from fastapi import HTTPException

from app.services import normalize_polymarket_address

FULL = "0xAbCdef0123456789abcdef0123456789ABCDEF01"


def test_plain_address_normalized_lowercase():
    assert normalize_polymarket_address(FULL) == FULL.lower()


def test_address_extracted_from_profile_url():
    url = f"https://polymarket.com/profile/{FULL}?tab=positions"
    assert normalize_polymarket_address(url) == FULL.lower()


def test_address_extracted_from_padded_text():
    assert normalize_polymarket_address(f"  {FULL}\n") == FULL.lower()


@pytest.mark.parametrize(
    "bad",
    [
        "0xAbc…123",  # truncated display text copied from the app
        "0x1234",  # too short
        "myusername",
        "",
    ],
)
def test_bad_addresses_rejected_with_explanation(bad):
    with pytest.raises(HTTPException) as exc:
        normalize_polymarket_address(bad)
    assert exc.value.status_code == 400
    assert "full Polymarket wallet address" in exc.value.detail
