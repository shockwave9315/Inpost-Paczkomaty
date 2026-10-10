"""Unit tests for error mapping, parsing helpers and log redaction."""

import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.util import dt as dt_util

from custom_components.inpost_paczkomaty.api import (
    InPostApiClient,
    _parse_parcel_lockers,
)
from custom_components.inpost_paczkomaty.exceptions import (
    ApiAuthError,
    ApiClientError,
    ApiResponseError,
    InPostApiError,
    RateLimitedError,
)
from custom_components.inpost_paczkomaty.http_client import HttpClient
from custom_components.inpost_paczkomaty.inpost_auth_flow import InpostAuth
from custom_components.inpost_paczkomaty.models import (
    ApiCarbonFootprint,
    ApiParcel,
    HttpResponse,
)
from custom_components.inpost_paczkomaty.utils import (
    REDACTED,
    drop_nulls,
    get_header,
    parse_retry_after,
    redact_headers,
)

from .common import make_jwt, make_locker


@pytest.fixture
def client():
    hass = MagicMock()
    hass.config.language = "pl"
    return InPostApiClient(hass, access_token=make_jwt(), refresh_token="refresh-1")


# =============================================================================
# Helpers
# =============================================================================


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("120", 120.0),
        (" 7.5 ", 7.5),
        ("-3", 0.0),
        ("soon", None),
    ],
)
def test_parse_retry_after_seconds(value, expected):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date():
    when = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert 590 <= parse_retry_after(format_datetime(when, usegmt=True)) <= 600
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert parse_retry_after(format_datetime(past, usegmt=True)) == 0.0


def test_get_header_is_case_insensitive():
    assert get_header({"retry-after": "5"}, "Retry-After") == "5"
    assert get_header({}, "Retry-After") is None
    assert get_header(None, "Retry-After") is None


def test_redact_headers():
    headers = {
        "Authorization": "Bearer secret",
        "cookie": "SESSION=secret",
        "Accept": "application/json",
    }
    redacted = redact_headers(headers)
    assert redacted == {
        "Authorization": REDACTED,
        "cookie": REDACTED,
        "Accept": "application/json",
    }
    assert headers["Authorization"] == "Bearer secret"  # input untouched


def test_drop_nulls():
    assert drop_nulls({"a": None, "b": {"c": None, "d": 0}, "e": [{"f": None}, 1]}) == {
        "b": {"d": 0},
        "e": [{}, 1],
    }


# =============================================================================
# Log redaction at the transport level
# =============================================================================


async def test_http_client_does_not_log_credentials(caplog):
    """The real request path logs headers with credentials masked."""
    caplog.set_level(logging.DEBUG)
    http = HttpClient(auth_type="Bearer", auth_value="super-secret-access-token")

    response = MagicMock()
    response.status = 200
    response.json = AsyncMock(return_value={"access_token": "body-secret"})
    response.cookies = {}
    response.headers = {}
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.request = MagicMock(return_value=context)

    with patch.object(http, "_ensure_session", AsyncMock(return_value=session)):
        await http.post(
            "https://example.invalid/token",
            data={"refresh_token": "super-secret-refresh-token"},
            custom_headers={"Cookie": "SESSION=super-secret-cookie"},
        )

    sent = session.request.call_args.kwargs["headers"]
    assert sent["Authorization"] == "Bearer super-secret-access-token"
    assert "Headers:" in caplog.text and REDACTED in caplog.text
    assert "User-Agent" in caplog.text  # still useful for diagnostics
    for secret in ("super-secret", "body-secret"):
        assert secret not in caplog.text


async def test_token_exchange_failure_does_not_expose_response_body(caplog):
    """A partial token response is neither logged nor put in the exception."""
    caplog.set_level(logging.DEBUG)
    auth = InpostAuth()
    with patch.object(auth._http_client, "post", new_callable=AsyncMock) as post:
        post.return_value = HttpResponse(
            body={"access_token": "leaky-access-token", "id_token": "leaky-id"},
            status=200,
        )
        with pytest.raises(ValueError, match="Token exchange failed") as err:
            await auth.exchange_code_for_tokens("code")
    assert "leaky" not in str(err.value)
    assert "leaky" not in caplog.text


# =============================================================================
# Error mapping
# =============================================================================


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (401, {}, ApiAuthError),
        (429, {"Retry-After": "60"}, RateLimitedError),
        (403, {}, ApiClientError),
        (500, {}, ApiClientError),
    ],
)
async def test_parcels_error_status_mapping(client, status, headers, expected):
    client._refresh_token = None  # isolate the status mapping from token renewal
    with patch.object(client._http_client, "get", new_callable=AsyncMock) as get:
        get.return_value = HttpResponse(body={}, status=status, headers=headers)
        with pytest.raises(expected) as err:
            await client.get_parcels()
    assert type(err.value) is expected
    assert f"Status: {status}" in str(err.value)
    if expected is RateLimitedError:
        assert err.value.retry_after == 60.0


@pytest.mark.parametrize(
    "error",
    [
        InPostApiError("Request timed out"),
        aiohttp.ClientConnectionError("refused"),
        TimeoutError(),
        ConnectionResetError(),
    ],
)
async def test_transport_errors_become_api_client_error(client, error):
    with patch.object(client._http_client, "get", new_callable=AsyncMock) as get:
        get.side_effect = error
        with pytest.raises(ApiClientError) as err:
            await client.get_parcels()
    assert not isinstance(err.value, (ApiAuthError, RateLimitedError))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ApiAuthError),
        (401, ApiAuthError),
        (429, RateLimitedError),
        (503, ApiClientError),
    ],
)
async def test_refresh_error_status_mapping(client, status, expected):
    with patch.object(
        client._public_http_client, "post", new_callable=AsyncMock
    ) as post:
        post.return_value = HttpResponse(body={"error": "x"}, status=status)
        with pytest.raises(expected) as err:
            await client.refresh_access_token()
    assert type(err.value) is expected
    assert client._refresh_token == "refresh-1"  # a failed refresh changes nothing


@pytest.mark.parametrize("body", ["<html>", {}, {"access_token": ""}, None])
async def test_refresh_with_unusable_body_keeps_tokens(client, body):
    before = client._access_token
    with patch.object(
        client._public_http_client, "post", new_callable=AsyncMock
    ) as post:
        post.return_value = HttpResponse(body=body, status=200)
        with pytest.raises(ApiResponseError):
            await client.refresh_access_token()
    assert client._access_token == before
    assert client._refresh_token == "refresh-1"


async def test_programming_errors_are_not_swallowed(client):
    """Only transport errors are translated; bugs must surface."""
    with patch.object(client._http_client, "get", new_callable=AsyncMock) as get:
        get.side_effect = KeyError("bug")
        with pytest.raises(KeyError):
            await client.get_parcels()


# =============================================================================
# Summary building
# =============================================================================


def test_all_count_respects_show_only_own_parcels():
    hass = MagicMock()
    hass.config.language = "pl"
    parcels = [
        ApiParcel(shipment_number="1", status="DELIVERED", ownership_status="OWN"),
        ApiParcel(shipment_number="2", status="DELIVERED", ownership_status="FRIEND"),
    ]
    assert InPostApiClient(hass)._build_parcels_summary(parcels).all_count == 2
    own_only = InPostApiClient(hass, show_only_own_parcels=True)
    assert own_only._build_parcels_summary(parcels).all_count == 1


async def test_carbon_footprint_is_bucketed_by_local_day(hass):
    """A pickup at 23:30 UTC belongs to the next day in Warsaw."""
    await hass.config.async_set_time_zone("Europe/Warsaw")
    parcel = ApiParcel(
        shipment_number="1",
        status="DELIVERED",
        pick_up_date="2026-01-10T23:30:00.000Z",
        carbon_footprint=ApiCarbonFootprint(address_delivery="0.5"),
    )
    stats = (
        InPostApiClient(hass)._build_parcels_summary([parcel]).carbon_footprint_stats
    )
    assert [d.date for d in stats.daily_data] == ["2026-01-11"]
    assert dt_util.as_local(parcel.pick_up_date_parsed).day == 11


# =============================================================================
# Parcel lockers list
# =============================================================================


def test_parse_parcel_lockers_skips_bad_entries(caplog):
    body = {
        "items": [
            make_locker("GDA117M"),
            {"n": "BROKEN"},
            "garbage",
            {**make_locker("INT01M"), "l": {"a": 54, "o": 18}, "d": None, "q": 5},
        ]
    }
    lockers = _parse_parcel_lockers(body)
    assert [locker.n for locker in lockers] == ["GDA117M", "INT01M"]
    assert lockers[1].l.a == 54.0 and lockers[1].d == ""
    assert "Skipped 2 unreadable entries" in caplog.text


@pytest.mark.parametrize(
    "body", ["<html>", None, [], {"items": "x"}, {"items": [{"n": "A"}, {"x": 1}]}]
)
def test_parse_parcel_lockers_rejects_unusable_payload(body):
    with pytest.raises(ApiResponseError):
        _parse_parcel_lockers(body)


def test_parse_parcel_lockers_accepts_empty_list():
    assert _parse_parcel_lockers({"items": []}) == []
