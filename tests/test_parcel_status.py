"""Parcel status classification: the table itself and what the sensors show."""

import logging
import re
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component

from custom_components.inpost_paczkomaty.const import DOMAIN
from custom_components.inpost_paczkomaty.models import ApiParcel
from custom_components.inpost_paczkomaty.parcel_status import (
    STATUS_DELIVERED,
    ParcelState,
    classify_parcel,
    describe_status,
    statuses_in,
)

from .common import ACCOUNT_SLUG, LOCKER, make_entry, make_parcel

SENSOR = f"sensor.inpost_{ACCOUNT_SLUG}"
LOCKER_SENSOR = f"{SENSOR}_{LOCKER.lower()}"
LOCKER_BINARY = f"binary_sensor.inpost_{ACCOUNT_SLUG}_{LOCKER.lower()}"

# InPost's public status dictionary (api-shipx-pl.easypack24.net/v1/statuses)
# as published on 2026-10-08, upper-cased like the mobile API returns it.
OFFICIAL_STATUSES = frozenset(
    """
    CREATED OFFERS_PREPARED OFFER_SELECTED CONFIRMED DISPATCHED_BY_SENDER
    COLLECTED_FROM_SENDER TAKEN_BY_COURIER ADOPTED_AT_SOURCE_BRANCH
    SENT_FROM_SOURCE_BRANCH READY_TO_PICKUP_FROM_POK
    READY_TO_PICKUP_FROM_POK_REGISTERED OVERSIZED ADOPTED_AT_SORTING_CENTER
    SENT_FROM_SORTING_CENTER ADOPTED_AT_TARGET_BRANCH OUT_FOR_DELIVERY
    READY_TO_PICKUP PICKUP_REMINDER_SENT DELIVERED PICKUP_TIME_EXPIRED AVIZO
    CLAIMED RETURNED_TO_SENDER CANCELED OTHER DISPATCHED_BY_SENDER_TO_POK
    OUT_FOR_DELIVERY_TO_ADDRESS PICKUP_REMINDER_SENT_ADDRESS REJECTED_BY_RECEIVER
    UNDELIVERED_WRONG_ADDRESS UNDELIVERED_INCOMPLETE_ADDRESS
    UNDELIVERED_UNKNOWN_RECEIVER UNDELIVERED_COD_CASH_RECEIVER
    TAKEN_BY_COURIER_FROM_POK UNDELIVERED RETURN_PICKUP_CONFIRMATION_TO_SENDER
    READY_TO_PICKUP_FROM_BRANCH DELAY_IN_DELIVERY REDIRECT_TO_BOX
    CANCELED_REDIRECT_TO_BOX READDRESSED UNDELIVERED_NO_MAILBOX
    UNDELIVERED_NOT_LIVE_ADDRESS UNDELIVERED_LACK_OF_ACCESS_LETTERBOX MISSING
    STACK_IN_CUSTOMER_SERVICE_POINT STACK_PARCEL_PICKUP_TIME_EXPIRED
    UNSTACK_FROM_CUSTOMER_SERVICE_POINT COURIER_AVIZO_IN_CUSTOMER_SERVICE_POINT
    TAKEN_BY_COURIER_FROM_CUSTOMER_SERVICE_POINT STACK_IN_BOX_MACHINE
    UNSTACK_FROM_BOX_MACHINE STACK_PARCEL_IN_BOX_MACHINE_PICKUP_TIME_EXPIRED
    """.split()
)
# The dictionary's own placeholders: they say nothing about the parcel
MEANINGLESS = {"OTHER", "MISSING"}

READY = [
    "READY_TO_PICKUP",
    "PICKUP_REMINDER_SENT",
    "READY_TO_PICKUP_FROM_POK",
    "COURIER_AVIZO_IN_CUSTOMER_SERVICE_POINT",
    "STACK_IN_BOX_MACHINE",
    "STACK_IN_CUSTOMER_SERVICE_POINT",
]
EN_ROUTE = [
    "DISPATCHED_BY_SENDER",
    "COLLECTED_FROM_SENDER",
    "TAKEN_BY_COURIER",
    "ADOPTED_AT_SOURCE_BRANCH",
    "SENT_FROM_SOURCE_BRANCH",
    "ADOPTED_AT_SORTING_CENTER",
    "SENT_FROM_SORTING_CENTER",
    "ADOPTED_AT_TARGET_BRANCH",
    "OUT_FOR_DELIVERY",
    "PICKUP_TIME_EXPIRED",
    "STACK_PARCEL_IN_BOX_MACHINE_PICKUP_TIME_EXPIRED",
    "UNSTACK_FROM_BOX_MACHINE",
    "STACK_PARCEL_PICKUP_TIME_EXPIRED",
    "UNSTACK_FROM_CUSTOMER_SERVICE_POINT",
]
TERMINAL = ["DELIVERED", "RETURNED_TO_SENDER", "CANCELED", "REJECTED_BY_RECEIVER"]
UNKNOWN = ["SOMETHING_INPOST_ADDS_LATER", "OTHER", "ready_to_pickup", ""]


def parcel(status: str) -> ApiParcel:
    return ApiParcel(shipment_number="1", status=status)


# =============================================================================
# The table
# =============================================================================


@pytest.mark.parametrize(
    ("statuses", "state"),
    [
        (READY, ParcelState.READY),
        (EN_ROUTE, ParcelState.EN_ROUTE),
        (TERMINAL, ParcelState.TERMINAL),
        (UNKNOWN, ParcelState.UNKNOWN),
    ],
    ids=["ready", "en-route", "terminal", "unknown"],
)
def test_classify_parcel(statuses, state):
    assert {
        status: classify_parcel(parcel(status)).state for status in statuses
    } == dict.fromkeys(statuses, state)


def test_every_official_status_has_exactly_one_state():
    """Nothing InPost documents is left to chance - or filed twice."""
    states = (ParcelState.READY, ParcelState.EN_ROUTE, ParcelState.TERMINAL)
    classified = [status for state in states for status in statuses_in(state)]

    assert len(classified) == len(set(classified))
    assert set(classified) == OFFICIAL_STATUSES - MEANINGLESS
    assert not statuses_in(ParcelState.UNKNOWN)


def test_readme_documents_exactly_the_classified_statuses():
    """The status tables in the README are the classifier's, not a second opinion."""
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    section = readme.split("### Parcel Statuses", 1)[1].split("**Example:**", 1)[0]
    by_group = {
        ParcelState.READY: section.split("**Ready for pickup:**")[1].split(
            "**En route:**"
        )[0],
        ParcelState.EN_ROUTE: section.split("**En route:**")[1].split("**Finished:**")[
            0
        ],
        ParcelState.TERMINAL: section.split("**Finished:**")[1].split("\n\n")[0],
    }
    for state, text in by_group.items():
        assert set(re.findall(r"`([A-Z][A-Z_]+)`", text)) == statuses_in(state), state


def test_delivered_is_terminal_and_still_named_for_the_carbon_footprint():
    assert STATUS_DELIVERED == "DELIVERED"
    assert classify_parcel(parcel(STATUS_DELIVERED)).state is ParcelState.TERMINAL


def test_status_descriptions():
    assert describe_status("READY_TO_PICKUP") == "Gotowa do odbioru"
    assert describe_status("ADOPTED_AT_SORTING_CENTER") == "Przyjęta w Sortowni"
    assert describe_status("SOMETHING_INPOST_ADDS_LATER") == (
        "SOMETHING_INPOST_ADDS_LATER"
    )
    for status in OFFICIAL_STATUSES - MEANINGLESS:
        assert describe_status(status) != status, status


# =============================================================================
# What the sensors show
# =============================================================================


async def setup_with(hass, fake_inpost, *parcels) -> None:
    fake_inpost.parcels = list(parcels)
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def snapshot(hass) -> dict:
    """Everything a status can influence, in one comparable value."""
    attributes = hass.states.get(f"{SENSOR}_parcels_list").attributes
    return {
        "all": hass.states.get(f"{SENSOR}_all_parcels_count").state,
        "ready": hass.states.get(f"{SENSOR}_ready_for_pickup_parcels_count").state,
        "en_route": hass.states.get(f"{SENSOR}_en_route_parcels_count").state,
        "listed": hass.states.get(f"{SENSOR}_parcels_list").state,
        "ready_list": [p["status"] for p in attributes["ready_for_pickup"]],
        "en_route_list": [p["status"] for p in attributes["en_route"]],
        "unknown": attributes["unknown_parcels_count"],
        "unknown_statuses": attributes["unknown_statuses"],
        "locker_ready": hass.states.get(
            f"{LOCKER_SENSOR}_ready_for_pickup_count"
        ).state,
        "locker_en_route": hass.states.get(f"{LOCKER_SENSOR}_en_route_count").state,
        "locker_ready_on": hass.states.get(f"{LOCKER_BINARY}_ready_for_pickup").state,
        "locker_en_route_on": hass.states.get(
            f"{LOCKER_BINARY}_parcels_en_route"
        ).state,
    }


NOTHING = {
    "ready": "0",
    "en_route": "0",
    "listed": "0",
    "ready_list": [],
    "en_route_list": [],
    "unknown": 0,
    "unknown_statuses": [],
    "locker_ready": "0",
    "locker_en_route": "0",
    "locker_ready_on": "off",
    "locker_en_route_on": "off",
}


@pytest.mark.parametrize("status", ["READY_TO_PICKUP", "PICKUP_REMINDER_SENT"])
async def test_parcel_waiting_in_the_locker_is_ready_for_pickup(
    hass, fake_inpost, status
):
    """Regression: after the reminder the parcel - and its code - disappeared."""
    await setup_with(hass, fake_inpost, make_parcel(1, status=status))

    assert snapshot(hass) == NOTHING | {
        "all": "1",
        "ready": "1",
        "listed": "1",
        "ready_list": [status],
        "locker_ready": "1",
        "locker_ready_on": "on",
    }
    listed = hass.states.get(f"{SENSOR}_parcels_list").attributes["ready_for_pickup"][0]
    assert listed["open_code"] == "680001"
    assert listed["qr_code"] == "P|+48123456789|680001"
    assert listed["pickup_point_unverified"] is False


@pytest.mark.parametrize(
    "status",
    [
        "COLLECTED_FROM_SENDER",
        "ADOPTED_AT_SORTING_CENTER",
        "SENT_FROM_SORTING_CENTER",
        "ADOPTED_AT_TARGET_BRANCH",
        "STACK_PARCEL_IN_BOX_MACHINE_PICKUP_TIME_EXPIRED",
        "UNSTACK_FROM_BOX_MACHINE",
        "STACK_PARCEL_PICKUP_TIME_EXPIRED",
        "UNSTACK_FROM_CUSTOMER_SERVICE_POINT",
        "OUT_FOR_DELIVERY",
    ],
)
async def test_parcel_in_transit_is_en_route(hass, fake_inpost, status):
    """Regression: only six transport statuses counted; the rest vanished."""
    await setup_with(hass, fake_inpost, make_parcel(1, status=status))

    assert snapshot(hass) == NOTHING | {
        "all": "1",
        "en_route": "1",
        "listed": "1",
        "en_route_list": [status],
        "locker_en_route": "1",
        "locker_en_route_on": "on",
    }
    listed = hass.states.get(f"{SENSOR}_parcels_list").attributes["en_route"][0]
    assert listed["status_description"] != status  # a readable description


async def test_delivered_parcel_is_finished_and_feeds_the_carbon_footprint(
    hass, fake_inpost
):
    delivered = make_parcel(
        1,
        status="DELIVERED",
        pickUpDate="2026-10-01T10:00:00.000Z",
        carbonFootprint={"boxMachineDelivery": "0.25", "addressDelivery": "0.9"},
    )
    await setup_with(hass, fake_inpost, delivered)

    assert snapshot(hass) == NOTHING | {"all": "1"}
    footprint = hass.states.get(f"{SENSOR}_total_carbon_footprint")
    assert float(footprint.state) == 0.25
    assert footprint.attributes["total_parcels"] == 1


@pytest.mark.parametrize("status", ["RETURNED_TO_SENDER", "CANCELED"])
async def test_other_finished_parcel_is_neither_active_nor_unknown(
    hass, fake_inpost, status
):
    finished = make_parcel(
        1,
        status=status,
        pickUpDate="2026-10-01T10:00:00.000Z",
        carbonFootprint={"boxMachineDelivery": "0.25"},
    )
    await setup_with(hass, fake_inpost, finished)

    assert snapshot(hass) == NOTHING | {"all": "1"}
    assert float(hass.states.get(f"{SENSOR}_total_carbon_footprint").state) == 0


async def test_unknown_status_is_counted_and_reported_once(hass, fake_inpost, caplog):
    """A status nobody has seen yet stays visible instead of vanishing."""
    entry = await setup_with(
        hass,
        fake_inpost,
        make_parcel(1),
        make_parcel(2, status="TELEPORTED"),
        make_parcel(3, status="TELEPORTED"),
        make_parcel(4, status="OTHER"),
    )

    assert snapshot(hass) == NOTHING | {
        "all": "4",
        "ready": "1",
        "listed": "1",
        "ready_list": ["READY_TO_PICKUP"],
        "unknown": 3,
        "unknown_statuses": ["OTHER", "TELEPORTED"],
        "locker_ready": "1",
        "locker_ready_on": "on",
    }

    for _ in range(3):
        await entry.runtime_data.async_refresh()
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "does not know" in record.getMessage()
    ]
    assert len(warnings) == 2  # one per status, however often it is polled
    assert sum("TELEPORTED" in message for message in warnings) == 1
    assert "680002" not in caplog.text  # the parcel's pickup code is not logged

    # A status appearing later is still reported
    fake_inpost.parcels = [make_parcel(5, status="BEAMED_UP")]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert snapshot(hass)["unknown_statuses"] == ["BEAMED_UP"]
    assert sum("BEAMED_UP" in record.getMessage() for record in caplog.records) == 1


async def test_log_is_not_flooded_by_ever_changing_statuses(hass, fake_inpost, caplog):
    """A broken API sending a new status every poll cannot fill the log."""
    entry = await setup_with(hass, fake_inpost, make_parcel(1))
    for number in range(40):
        fake_inpost.parcels = [make_parcel(1, status=f"GARBAGE_{number}")]
        await entry.runtime_data.async_refresh()

    warnings = [r for r in caplog.records if "does not know" in r.getMessage()]
    assert len(warnings) == 20
    assert snapshot(hass)["unknown_statuses"] == ["GARBAGE_39"]


async def test_ignored_en_route_statuses_apply_to_every_en_route_status(
    hass, fake_inpost
):
    """The option still filters the en route counts - for old and new statuses."""
    parcels = [
        make_parcel(1, status="CONFIRMED"),
        make_parcel(2, status="ADOPTED_AT_SORTING_CENTER"),
        make_parcel(3, status="OUT_FOR_DELIVERY"),
    ]
    await setup_with(hass, fake_inpost, *parcels)
    # Default: only CONFIRMED is left out
    assert snapshot(hass)["en_route_list"] == [
        "ADOPTED_AT_SORTING_CENTER",
        "OUT_FOR_DELIVERY",
    ]
    assert snapshot(hass)["all"] == "3"
    assert snapshot(hass)["unknown"] == 0


async def test_user_can_ignore_a_newly_recognised_status(hass, fake_inpost):
    assert await async_setup_component(
        hass,
        DOMAIN,
        {DOMAIN: {"ignored_en_route_statuses": ["ADOPTED_AT_SORTING_CENTER"]}},
    )
    parcels = [
        make_parcel(1, status="CONFIRMED"),
        make_parcel(2, status="ADOPTED_AT_SORTING_CENTER"),
    ]
    await setup_with(hass, fake_inpost, *parcels)

    assert snapshot(hass)["en_route_list"] == ["CONFIRMED"]
    assert snapshot(hass)["all"] == "2"
