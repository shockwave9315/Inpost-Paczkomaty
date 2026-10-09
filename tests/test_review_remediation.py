"""Regressions from the final review: temporary pickup, titles and setup 429."""

from datetime import timedelta
from email.utils import format_datetime
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

import custom_components.inpost_paczkomaty as integration
from custom_components.inpost_paczkomaty.const import DATA_SETUP_RATE_LIMITS
from custom_components.inpost_paczkomaty.models import HttpResponse
from custom_components.inpost_paczkomaty.parcel_status import (
    ParcelState,
    classify_parcel,
)

from .common import (
    ACCOUNT_SLUG,
    LOCKER,
    PARCELS_PATH,
    PHONE,
    PROFILE_PATH,
    TOKEN_PATH,
    make_entry,
    make_jwt,
    make_parcel,
    make_profile,
)

SENSOR = f"sensor.inpost_{ACCOUNT_SLUG}"
LOCKER_SENSOR = f"{SENSOR}_{LOCKER.lower()}"
LOCKER_BINARY = f"binary_sensor.inpost_{ACCOUNT_SLUG}_{LOCKER.lower()}"
STACK_STATUSES = ("STACK_IN_BOX_MACHINE", "STACK_IN_CUSTOMER_SERVICE_POINT")


async def setup_entry(hass, entry, title=None):
    entry.add_to_hass(hass)
    if title is not None:
        hass.config_entries.async_update_entry(entry, title=title)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("status", STACK_STATUSES)
@pytest.mark.parametrize("point", [LOCKER, "GDA145M", None])
async def test_temporary_storage_is_ready_without_locker_assignment(
    hass, fake_inpost, status, point
):
    """Even an API point matching a monitored locker cannot claim pickup there."""
    raw = make_parcel(1, status=status, locker=point)
    fake_inpost.parcels = [raw]
    entry = make_entry()
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get(f"{SENSOR}_all_parcels_count").state == "1"
    assert hass.states.get(f"{SENSOR}_ready_for_pickup_parcels_count").state == "1"
    assert hass.states.get(f"{SENSOR}_en_route_parcels_count").state == "0"
    listed = hass.states.get(f"{SENSOR}_parcels_list").attributes
    (ready,) = listed["ready_for_pickup"]
    assert not listed["en_route"]
    assert ready["status"] == status
    assert ready["open_code"] == raw["openCode"]
    assert ready["qr_code"] == raw["qrCode"]
    assert ready["pickup_point_name"] == point  # retained as reported metadata
    assert ready["pickup_point_unverified"] is True

    assert hass.states.get(f"{LOCKER_SENSOR}_ready_for_pickup_count").state == "0"
    assert hass.states.get(f"{LOCKER_SENSOR}_en_route_count").state == "0"
    assert hass.states.get(f"{LOCKER_BINARY}_ready_for_pickup").state == "off"
    assert hass.states.get(f"{LOCKER_BINARY}_parcels_en_route").state == "off"
    assert entry.runtime_data.data.ready_for_pickup == {}
    assert entry.runtime_data.data.en_route == {}
    classification = classify_parcel(entry.runtime_data.api_client._parse_parcel(raw))
    assert classification.state is ParcelState.READY
    assert classification.assign_to_locker is False


@pytest.mark.parametrize("status", STACK_STATUSES)
async def test_temporary_storage_transition_updates_existing_locker_sensors(
    hass, fake_inpost, status
):
    fake_inpost.parcels = [make_parcel(1, status="OUT_FOR_DELIVERY")]
    entry = make_entry()
    await setup_entry(hass, entry)
    assert hass.states.get(f"{LOCKER_BINARY}_parcels_en_route").state == "on"

    fake_inpost.parcels = [make_parcel(1, status=status)]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(f"{SENSOR}_ready_for_pickup_parcels_count").state == "1"
    assert hass.states.get(f"{LOCKER_SENSOR}_ready_for_pickup_count").state == "0"
    assert hass.states.get(f"{LOCKER_SENSOR}_en_route_count").state == "0"
    assert hass.states.get(f"{LOCKER_BINARY}_ready_for_pickup").state == "off"
    assert hass.states.get(f"{LOCKER_BINARY}_parcels_en_route").state == "off"

    fake_inpost.parcels = [make_parcel(1, status="READY_TO_PICKUP")]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(f"{LOCKER_SENSOR}_ready_for_pickup_count").state == "1"
    assert hass.states.get(f"{LOCKER_BINARY}_ready_for_pickup").state == "on"
    listed = hass.states.get(f"{SENSOR}_parcels_list").attributes["ready_for_pickup"]
    assert listed[0]["pickup_point_unverified"] is False


@pytest.mark.parametrize("reauth", [False, True], ids=["setup", "reauth"])
@pytest.mark.parametrize("legacy_unique_id", [None, PHONE], ids=["no-id", "national"])
@pytest.mark.parametrize(
    "title",
    [f"InPost: +48 {PHONE}", "Paczki Huberta", f"InPost: +48 {PHONE} dom"],
    ids=["automatic", "custom", "similar-custom"],
)
async def test_legacy_title_changes_only_when_it_was_automatically_generated(
    hass, fake_inpost, reauth, legacy_unique_id, title
):
    fake_inpost.profile = make_profile(prefix="+380")
    entry = make_entry(
        legacy_unique_id=legacy_unique_id,
        access_token=make_jwt(-10) if reauth else make_jwt(),
    )
    if reauth:
        fake_inpost.token_queue = [
            HttpResponse(body={"error": "invalid_grant"}, status=400)
        ]
    await setup_entry(hass, entry, title)
    if reauth:
        (flow,) = hass.config_entries.flow.async_progress()
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"redirect_url": "CODE"}
        )
        assert result["reason"] == "reauth_successful"
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.unique_id == "+380123456789"
    assert entry.title == (
        "InPost: +380123456789" if title == f"InPost: +48 {PHONE}" else title
    )


@pytest.mark.parametrize("reauth", [False, True], ids=["setup", "reauth"])
@pytest.mark.parametrize(
    "title", [f"InPost: +48{PHONE}", "Paczki Huberta", f"InPost: +48 {PHONE}"]
)
async def test_identified_entry_title_is_preserved(hass, fake_inpost, reauth, title):
    """An identified entry's user title may even resemble the old automatic one."""
    entry = make_entry(access_token=make_jwt(-10) if reauth else make_jwt())
    if reauth:
        fake_inpost.token_queue = [
            HttpResponse(body={"error": "invalid_grant"}, status=400)
        ]
    await setup_entry(hass, entry, title)
    if reauth:
        (flow,) = hass.config_entries.flow.async_progress()
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"redirect_url": "CODE"}
        )
        assert result["reason"] == "reauth_successful"
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.title == title


@pytest.mark.parametrize("header_kind", ["seconds", "http-date"])
@pytest.mark.parametrize("endpoint", ["parcels", "profile", "token"])
async def test_setup_retry_after_blocks_http_until_deadline(
    hass, fake_inpost, header_kind, endpoint
):
    """HA's five-second retry and explicit reload cannot bypass Retry-After."""
    hass.set_state(CoreState.running)
    header = "900"
    if header_kind == "http-date":
        header = format_datetime(dt_util.utcnow() + timedelta(seconds=900), usegmt=True)
    response = HttpResponse(body={}, status=429, headers={"Retry-After": header})
    getattr(fake_inpost, f"{endpoint}_queue").append(response)
    entry = make_entry(
        legacy_unique_id=PHONE if endpoint == "profile" else False,
        access_token=make_jwt(-10) if endpoint == "token" else make_jwt(),
    )
    with patch.object(integration, "monotonic", return_value=1000.0) as clock:
        await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert len(fake_inpost.calls) == 1
        deadline = hass.data[DATA_SETUP_RATE_LIMITS][entry.entry_id]
        assert 1898 < deadline <= 1900

        clock.return_value = 1006.0
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert "waiting" in entry.reason  # HA really called setup again
        assert len(fake_inpost.calls) == 1

        # Unloading a failed setup/reloading the entry must retain the deadline.
        assert not await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert len(fake_inpost.calls) == 1
        assert hass.data[DATA_SETUP_RATE_LIMITS][entry.entry_id] == deadline

        clock.return_value = deadline - 0.1
        assert not await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert len(fake_inpost.calls) == 1

        clock.return_value = deadline + 1
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert len(fake_inpost.calls) > 1
        assert entry.entry_id not in hass.data[DATA_SETUP_RATE_LIMITS]
        assert entry.runtime_data.update_interval == timedelta(seconds=30)


async def test_setup_rate_limit_does_not_block_another_account(hass, fake_inpost):
    fake_inpost.parcels_queue = [
        HttpResponse(body={}, status=429, headers={"Retry-After": "900"})
    ]
    first = make_entry()
    second = make_entry(phone="987654321")
    with patch.object(integration, "monotonic", return_value=1000.0):
        await setup_entry(hass, first)
        assert first.state is ConfigEntryState.SETUP_RETRY
        await setup_entry(hass, second)
        assert second.state is ConfigEntryState.LOADED
        assert fake_inpost.count(PARCELS_PATH) == 2
        assert first.entry_id in hass.data[DATA_SETUP_RATE_LIMITS]
        assert second.entry_id not in hass.data[DATA_SETUP_RATE_LIMITS]
        assert not await hass.config_entries.async_reload(first.entry_id)
        await hass.async_block_till_done()
        assert fake_inpost.count(PARCELS_PATH) == 2
        assert fake_inpost.count(PROFILE_PATH) == fake_inpost.count(TOKEN_PATH) == 0
