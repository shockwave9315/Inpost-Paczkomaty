"""Integration tests: setup/unload lifecycle, tokens, errors and entities."""

import logging
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.inpost_paczkomaty.const import DOMAIN
from custom_components.inpost_paczkomaty.exceptions import InPostApiError
from custom_components.inpost_paczkomaty.http_client import HttpClient
from custom_components.inpost_paczkomaty.models import HttpResponse

from .common import (
    LOCKER,
    PARCELS_PATH,
    PHONE,
    TOKEN_PATH,
    make_entry,
    make_jwt,
    make_parcel,
)

PARCELS_LIST = f"sensor.inpost_{PHONE}_parcels_list"
READY_COUNT = f"sensor.inpost_{PHONE}_ready_for_pickup_parcels_count"


async def setup_entry(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def reauth_flows(hass: HomeAssistant) -> list:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"]["source"] == SOURCE_REAUTH
    ]


def entity_ids(hass: HomeAssistant, entry) -> set[str]:
    registry = er.async_get(hass)
    return {
        e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


# =============================================================================
# Setup, entities and identity
# =============================================================================


async def test_setup_fetches_parcels_once_and_creates_entities(hass, fake_inpost):
    """Setup issues a single parcels request and registers documented IDs."""
    entry = make_entry()
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert fake_inpost.count(PARCELS_PATH) == 1
    assert fake_inpost.count(TOKEN_PATH) == 0

    locker = LOCKER.lower()
    assert entity_ids(hass, entry) == {
        f"sensor.inpost_{PHONE}_all_parcels_count",
        f"sensor.inpost_{PHONE}_en_route_parcels_count",
        READY_COUNT,
        PARCELS_LIST,
        f"sensor.inpost_{PHONE}_total_carbon_footprint",
        f"sensor.inpost_{PHONE}_today_carbon_footprint",
        f"sensor.inpost_{PHONE}_carbon_footprint_statistics",
        f"sensor.inpost_{PHONE}_{locker}_en_route_count",
        f"sensor.inpost_{PHONE}_{locker}_ready_for_pickup_count",
        f"sensor.inpost_{PHONE}_{locker}_locker_id",
        f"sensor.inpost_{PHONE}_{locker}_description",
        f"sensor.inpost_{PHONE}_{locker}_address",
        f"binary_sensor.inpost_{PHONE}_{locker}_parcels_en_route",
        f"binary_sensor.inpost_{PHONE}_{locker}_ready_for_pickup",
    }
    assert hass.states.get(READY_COUNT).state == "1"
    assert (
        hass.states.get(f"binary_sensor.inpost_{PHONE}_{locker}_ready_for_pickup").state
        == "on"
    )
    assert hass.states.get(f"sensor.inpost_{PHONE}_{locker}_locker_id").state == LOCKER
    attrs = hass.states.get(PARCELS_LIST).attributes
    assert attrs["ready_for_pickup"][0]["open_code"] == "680001"
    assert attrs["invalid_parcels_count"] == 0
    assert attrs["has_more"] is False

    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        assert entity.unique_id.startswith(f"{entry.entry_id}_")


async def test_two_accounts_tracking_the_same_locker(hass, fake_inpost):
    """Entities and devices are scoped to the entry, so nothing collides."""
    first = make_entry()
    second = make_entry(phone="987654321")
    await setup_entry(hass, first)
    await setup_entry(hass, second)

    assert first.state is second.state is ConfigEntryState.LOADED
    assert len(entity_ids(hass, first)) == 14
    assert len(entity_ids(hass, second)) == 14
    assert f"sensor.inpost_987654321_{LOCKER.lower()}_en_route_count" in entity_ids(
        hass, second
    )
    devices = dr.async_get(hass)
    assert len(dr.async_entries_for_config_entry(devices, first.entry_id)) == 2
    assert len(dr.async_entries_for_config_entry(devices, second.entry_id)) == 2


async def test_entry_without_unique_id_adopts_phone_number(hass, fake_inpost):
    """Entries created before unique IDs existed are identified on setup."""
    entry = make_entry(unique_id=None)
    await setup_entry(hass, entry)
    assert entry.unique_id == PHONE


async def test_stale_registry_entries_are_removed(hass, fake_inpost):
    """Entities from the pre-0.5.0 identity scheme and dropped lockers go away."""
    entry = make_entry(lockers=(LOCKER, "GDA145M"))
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{PHONE}_total_count",
        config_entry=entry,
        suggested_object_id="legacy_total",
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(legacy.entity_id) is None
    assert len(entity_ids(hass, entry)) == 7 + 2 * 7
    devices = dr.async_get(hass)
    assert len(dr.async_entries_for_config_entry(devices, entry.entry_id)) == 3

    # Drop one locker the way the options flow does
    hass.config_entries.async_update_entry(
        entry, options={"lockers": [entry.options["lockers"][0]]}
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert len(entity_ids(hass, entry)) == 14
    assert not any("gda145m" in entity_id for entity_id in entity_ids(hass, entry))
    assert len(dr.async_entries_for_config_entry(devices, entry.entry_id)) == 2


async def test_large_parcel_lists_are_not_recorded(hass, fake_inpost, caplog):
    """Unbounded list attributes stay live but out of the recorder."""
    fake_inpost.parcels = [make_parcel(i) for i in range(60)]
    entry = make_entry()
    await setup_entry(hass, entry)

    assert len(hass.states.get(PARCELS_LIST).attributes["ready_for_pickup"]) == 60
    await hass.async_add_executor_job(get_instance(hass).block_till_done)
    assert "exceed maximum size" not in caplog.text


# =============================================================================
# HTTP session lifecycle
# =============================================================================


@pytest.fixture
def tracked_sessions():
    """Let real aiohttp sessions be created and record them."""
    sessions = []
    original = HttpClient._ensure_session

    async def _ensure_session(self):
        session = await original(self)
        if session not in sessions:
            sessions.append(session)
        return session

    with patch.object(HttpClient, "_ensure_session", _ensure_session):
        yield sessions


def _session_backed_request(fake_inpost):
    async def _request(self, method, url, **kwargs):
        await self._ensure_session()
        return await fake_inpost.request(self, method, url, **kwargs)

    return _request


async def test_unload_and_reload_close_http_sessions(
    hass, fake_inpost, tracked_sessions
):
    """No aiohttp session survives unload or reload."""
    entry = make_entry(access_token=make_jwt(60))  # forces a token refresh too
    with patch.object(HttpClient, "_request", _session_backed_request(fake_inpost)):
        await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.LOADED
        assert len(tracked_sessions) == 2  # authenticated + public client

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert all(session.closed for session in tracked_sessions[:2])

        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.NOT_LOADED

    assert tracked_sessions
    assert all(session.closed for session in tracked_sessions)


async def test_failed_setup_closes_http_sessions(hass, fake_inpost, tracked_sessions):
    """A setup that never completes does not leave a session behind."""
    fake_inpost.parcels_queue = [HttpResponse(body={}, status=500)]
    entry = make_entry()
    with patch.object(HttpClient, "_request", _session_backed_request(fake_inpost)):
        await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert len(tracked_sessions) == 1
    assert all(session.closed for session in tracked_sessions)


# =============================================================================
# Token refresh and persistence
# =============================================================================


async def test_rotated_tokens_are_persisted_without_reload(hass, fake_inpost):
    """Refreshed tokens are stored in the entry and survive a restart."""
    entry = make_entry(access_token=make_jwt(60))
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    new_access = entry.data["access_token"]
    assert new_access.endswith("sig-new")
    assert entry.data["refresh_token"] == "refresh-rotated"
    assert fake_inpost.last(TOKEN_PATH)["data"]["refresh_token"] == "refresh-original"
    assert fake_inpost.last(PARCELS_PATH)["headers"]["Authorization"] == (
        f"Bearer {new_access}"
    )

    # Persisting tokens must not reload the entry (no reload loop)
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator
    assert fake_inpost.count(PARCELS_PATH) == 1

    # "Restart": a fresh client is built from the stored entry data
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert fake_inpost.count(TOKEN_PATH) == 1  # the stored token is still valid
    assert fake_inpost.last(PARCELS_PATH)["headers"]["Authorization"] == (
        f"Bearer {new_access}"
    )
    assert entry.runtime_data.api_client._refresh_token == "refresh-rotated"


async def test_refresh_without_new_refresh_token_keeps_the_old_one(hass, fake_inpost):
    """A provider that does not rotate must not wipe the refresh token."""
    fake_inpost.token_body = {"access_token": make_jwt(60, marker="short")}
    entry = make_entry(access_token=make_jwt(60))
    await setup_entry(hass, entry)

    client = entry.runtime_data.api_client
    assert client._refresh_token == "refresh-original"
    assert entry.data["refresh_token"] == "refresh-original"
    assert entry.data["access_token"].endswith("sig-short")

    # The next refresh is still possible with the preserved token
    await entry.runtime_data.async_refresh()
    assert fake_inpost.count(TOKEN_PATH) == 2
    assert fake_inpost.last(TOKEN_PATH)["data"]["refresh_token"] == "refresh-original"
    assert entry.runtime_data.last_update_success


async def test_rejected_access_token_is_renewed_once(hass, fake_inpost):
    """HTTP 401 with a not-yet-expired token triggers one refresh and retry."""
    fake_inpost.parcels_queue = [HttpResponse(body={}, status=401)]
    entry = make_entry()
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert fake_inpost.count(TOKEN_PATH) == 1
    assert fake_inpost.count(PARCELS_PATH) == 2
    assert entry.data["refresh_token"] == "refresh-rotated"
    assert not reauth_flows(hass)


# =============================================================================
# Authentication failures
# =============================================================================


async def test_invalid_grant_during_setup_starts_reauth(hass, fake_inpost):
    """A rejected refresh token is an auth failure, not a transient error."""
    fake_inpost.token_queue = [
        HttpResponse(body={"error": "invalid_grant"}, status=400)
    ]
    entry = make_entry(access_token=make_jwt(-10))
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = reauth_flows(hass)
    assert len(flows) == 1
    assert flows[0]["context"]["entry_id"] == entry.entry_id
    assert fake_inpost.count(PARCELS_PATH) == 0


async def test_persistent_401_starts_reauth_and_backs_off(hass, fake_inpost):
    """401 that survives a token refresh starts reauth and slows polling."""
    entry = make_entry()
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    base = coordinator.update_interval

    fake_inpost.parcels_queue = [HttpResponse(body={}, status=401)] * 2
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert len(reauth_flows(hass)) == 1
    assert coordinator.update_interval == base * 2
    assert hass.states.get(READY_COUNT).state == "unavailable"


async def test_network_error_is_not_an_auth_failure(hass, fake_inpost):
    """Timeouts and 5xx are retried and never start reauth."""
    fake_inpost.parcels_queue = [InPostApiError("Request timed out")]
    entry = make_entry()
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not reauth_flows(hass)


# =============================================================================
# API unavailability, rate limiting and backoff
# =============================================================================


@pytest.mark.parametrize(
    "failure",
    [
        HttpResponse(body={"status": 500}, status=500),
        HttpResponse(body="<html>Bad Gateway</html>", status=502),
        HttpResponse(body="not json at all", status=200),
        HttpResponse(body=["unexpected"], status=200),
        HttpResponse(body={"parcels": "nope"}, status=200),
        InPostApiError("Request timed out"),
        ConnectionResetError("reset"),
    ],
    ids=[
        "500",
        "502-html",
        "invalid-json",
        "json-array",
        "bad-list",
        "timeout",
        "reset",
    ],
)
async def test_api_failure_marks_entities_unavailable_then_recovers(
    hass, fake_inpost, failure
):
    """Any failed update makes entities unavailable; the next success recovers."""
    entry = make_entry()
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    base = coordinator.update_interval

    fake_inpost.parcels_queue = [failure]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert hass.states.get(READY_COUNT).state == "unavailable"
    assert coordinator.update_interval == base * 2
    assert not reauth_flows(hass)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert hass.states.get(READY_COUNT).state == "1"
    assert coordinator.update_interval == base


async def test_backoff_grows_and_is_capped(hass, fake_inpost, caplog):
    """Consecutive failures double the interval up to one hour, logging once."""
    entry = make_entry()
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    assert coordinator.update_interval == timedelta(seconds=30)

    fake_inpost.parcels_queue = [HttpResponse(body={}, status=503)] * 12
    caplog.clear()
    seen = []
    for _ in range(12):
        await coordinator.async_refresh()
        seen.append(coordinator.update_interval.total_seconds())

    assert seen[:4] == [60, 120, 240, 480]
    assert seen[-1] == 3600
    assert max(seen) == 3600
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1  # the coordinator reports the outage once


async def test_rate_limit_honours_retry_after(hass, fake_inpost):
    """HTTP 429 postpones the next poll by at least Retry-After."""
    entry = make_entry()
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data

    fake_inpost.parcels_queue = [
        HttpResponse(body={}, status=429, headers={"Retry-After": "900"})
    ]
    await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert coordinator.update_interval == timedelta(seconds=900)
    assert fake_inpost.count(TOKEN_PATH) == 0

    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.update_interval == timedelta(seconds=30)


async def test_rate_limit_without_retry_after_uses_backoff(hass, fake_inpost):
    """HTTP 429 without Retry-After still backs off."""
    entry = make_entry()
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data

    fake_inpost.parcels_queue = [HttpResponse(body={}, status=429)]
    await coordinator.async_refresh()
    assert coordinator.update_interval == timedelta(seconds=60)


# =============================================================================
# Payload resilience
# =============================================================================


@pytest.mark.parametrize(
    "broken",
    [
        {"shipmentNumber": None},
        {"status": None},
        {"pickUpPoint": "GDA117M"},
        {"receiver": {"phoneNumber": {"prefix": "+48"}}},
        {"sender": ["x"]},
    ],
    ids=["no-number", "no-status", "point-not-object", "partial-phone", "sender-list"],
)
async def test_one_malformed_parcel_does_not_hide_the_others(
    hass, fake_inpost, caplog, broken
):
    """A bad record is skipped, counted and reported once - nothing else."""
    fake_inpost.parcels = [make_parcel(1), make_parcel(2, **broken), make_parcel(3)]
    entry = make_entry()
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    attrs = hass.states.get(PARCELS_LIST).attributes
    assert attrs["invalid_parcels_count"] == 1
    assert [p["open_code"] for p in attrs["ready_for_pickup"]] == ["680001", "680003"]

    await entry.runtime_data.async_refresh()
    warnings = [r for r in caplog.records if "Skipping parcel" in r.getMessage()]
    assert len(warnings) == 1
    assert "680002" not in caplog.text  # pickup code of the skipped parcel


@pytest.mark.parametrize(
    "tolerated",
    [
        {"pickUpPoint": {"name": None, "easyAccessZone": None}},
        {
            "pickUpPoint": {
                "name": LOCKER,
                "location": {"latitude": 54, "longitude": 18},
            }
        },
        {"carbonFootprint": {"boxMachineDelivery": 0.012, "addressDelivery": None}},
        {"openCode": 680002, "qrCode": None, "storedDate": None},
        {
            "sender": None,
            "receiver": None,
            "parcelSize": None,
            "unknownField": {"a": 1},
        },
    ],
    ids=[
        "null-point-fields",
        "int-coordinates",
        "numeric-co2",
        "numeric-code",
        "nulls",
    ],
)
async def test_harmless_payload_variations_are_accepted(hass, fake_inpost, tolerated):
    """Nulls, unknown fields and numeric scalars do not drop a parcel."""
    fake_inpost.parcels = [make_parcel(1), make_parcel(2, **tolerated)]
    entry = make_entry()
    await setup_entry(hass, entry)

    attrs = hass.states.get(PARCELS_LIST).attributes
    assert attrs["invalid_parcels_count"] == 0
    assert len(attrs["ready_for_pickup"]) == 2
    assert attrs["ready_for_pickup"][1]["open_code"] == "680002"


async def test_fully_unparseable_list_is_a_failure_not_an_empty_list(hass, fake_inpost):
    """If no record can be read the update fails instead of reporting zero."""
    fake_inpost.parcels = [{"foo": 1}, {"bar": 2}]
    entry = make_entry()
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_empty_and_missing_parcels_list(hass, fake_inpost):
    """An account without parcels is a valid, empty result."""
    fake_inpost.parcels_queue = [HttpResponse(body={"updatedUntil": "x"}, status=200)]
    entry = make_entry()
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(READY_COUNT).state == "0"


async def test_truncated_response_is_reported(hass, fake_inpost, caplog):
    """`more: true` is surfaced instead of silently showing a partial list."""
    fake_inpost.parcels_extra = {"more": True}
    entry = make_entry()
    await setup_entry(hass, entry)
    await entry.runtime_data.async_refresh()

    assert hass.states.get(PARCELS_LIST).attributes["has_more"] is True
    warnings = [r for r in caplog.records if "more tracked parcels" in r.getMessage()]
    assert len(warnings) == 1


# =============================================================================
# Secrets in logs
# =============================================================================


async def test_no_secrets_in_debug_logs(hass, fake_inpost, caplog):
    """The integration never logs tokens or pickup codes, even at DEBUG.

    Only the integration's own loggers are checked: Home Assistant core logs
    full state objects at DEBUG, which is outside the integration's control.
    """
    caplog.set_level(logging.DEBUG)
    access = make_jwt(60, marker="initial")
    entry = make_entry(access_token=access)
    fake_inpost.parcels = [make_parcel(1), make_parcel(2, status=None)]
    await setup_entry(hass, entry)
    fake_inpost.token_queue = [
        HttpResponse(body={"error": "invalid_grant"}, status=400)
    ]
    fake_inpost.parcels_queue = [HttpResponse(body={}, status=401)]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    own_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.inpost_paczkomaty")
    )
    assert "Skipping parcel" in own_logs and "Status: 400" in own_logs

    for secret in (
        access,
        entry.data["access_token"],
        "refresh-original",
        "refresh-rotated",
        "680001",
        "680002",
        PHONE,
    ):
        assert secret not in own_logs
