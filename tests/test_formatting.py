from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.services import localtime
from app.web.templates_env import (
    GROUP_SEPARATOR as NB,  # narrow no-break space between digit groups
)
from app.web.templates_env import format_amount, format_pct, format_plain, format_usd


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # This is the case the filter was made for: Decimal.normalize()
        # returned 1.5E+8, and in the portfolio table that looked like an
        # error.
        (Decimal("150000000"), f"150{NB}000{NB}000"),
        (Decimal("180.000000000000000000"), "180"),
        (Decimal("16800.00"), f"16{NB}800"),
        (Decimal("0.42"), "0.42"),
        (Decimal("0.000001"), "0.000001"),
        (Decimal("6.5"), "6.5"),
        (Decimal("-12.5"), "-12.5"),
        (Decimal("0"), "0"),
        (None, "—"),
    ],
)
def test_format_amount(value, expected):
    assert format_amount(value) == expected


def test_amount_trims_excess_precision():
    """Eighteen decimal places from the database aren't needed in the UI."""
    assert format_amount(Decimal("0.123456789012345678")) == "0.12345679"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("85054.59"), f"$85{NB}054.59"),
        (Decimal("1234.5"), f"$1{NB}234.50"),
        (Decimal("0"), "$0.00"),
        (Decimal("-500"), "-$500.00"),
        (None, "—"),
    ],
)
def test_format_usd(value, expected):
    assert format_usd(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("120.5"), "+$120.50"),
        (Decimal("-120.5"), "-$120.50"),
        (Decimal("0"), "+$0.00"),
    ],
)
def test_format_usd_signed(value, expected):
    """For PnL a plus is as informative as a minus."""
    assert format_usd(value, signed=True) == expected


@pytest.mark.parametrize(
    ("value", "signed", "expected"),
    [
        (Decimal("5.256"), True, "+5.26%"),
        (Decimal("-1.64"), True, "-1.64%"),
        (Decimal("39.3"), False, "39.30%"),
        (None, True, "—"),
    ],
)
def test_format_pct(value, signed, expected):
    assert format_pct(value, signed=signed) == expected


def test_pct_respects_decimals():
    assert format_pct(Decimal("39.34"), 1) == "39.3%"


# --- Time zone ---


class FakeUser:
    def __init__(self, tz):
        self.timezone = tz


def test_moment_shifts_into_user_zone():
    """Time is stored in UTC and shown in the user's time zone."""
    utc = datetime(2026, 9, 6, 0, 27, tzinfo=timezone.utc)

    assert localtime.moment(utc, FakeUser("UTC")) == "06.09 00:27"
    assert localtime.moment(utc, FakeUser("Europe/Kyiv")) == "06.09 03:27"


def test_moment_treats_naive_time_as_utc():
    """Silently shifting naive time to local time is the worst option."""
    naive = datetime(2026, 9, 6, 0, 27)

    assert localtime.moment(naive, FakeUser("Europe/Kyiv")) == "06.09 03:27"


def test_unknown_zone_falls_back_to_utc_instead_of_crashing():
    utc = datetime(2026, 9, 6, 0, 27, tzinfo=timezone.utc)

    assert localtime.moment(utc, FakeUser("Средиземье/Шир")) == "06.09 00:27"


def test_moment_without_user_is_utc():
    utc = datetime(2026, 9, 6, 0, 27, tzinfo=timezone.utc)

    assert localtime.moment(utc, None) == "06.09 00:27"
    assert localtime.moment(None, FakeUser("UTC")) == "—"


# --- Values for input fields ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("-1.0000"), "-1"),
        (Decimal("90.00"), "90"),
        (Decimal("1000"), "1000"),
        (Decimal("0.0200"), "0.02"),
        (None, ""),
    ],
)
def test_plain_is_safe_to_put_into_a_form(value, expected):
    """Display filters don't fit a form's value.

    The server won't parse "-1.00%" or "1 000" with a non-breaking space, and the rule
    would stop saving after a single click on "Save".
    """
    assert format_plain(value) == expected


def test_plain_output_survives_a_round_trip():
    from app.services import tools_service

    for value in (Decimal("-1.5"), Decimal("0.02"), Decimal(1000)):
        assert tools_service.parse_decimal(format_plain(value), "поле") == value
