"""Tests for the config, re-authentication and options flows."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.inpost_paczkomaty.const import DOMAIN
from custom_components.inpost_paczkomaty.models import HttpResponse

from .common import (
    ACCOUNT_ID,
    ACCOUNT_SLUG,
    LOCKER,
    LOCKERS_URL,
    PHONE,
    PROFILE_PATH,
    TOKEN_PATH,
    make_entry,
    make_jwt,
    make_locker,
    make_profile,
)

REDIRECT = {"redirect_url": "https://account.inpost-group.com/callback?code=CODE123"}


async def start_user_flow(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


def _pkce(result: dict) -> tuple[str, str]:
    query = parse_qs(urlsplit(result["description_placeholders"]["login_url"]).query)
    return query["code_challenge"][0], query["state"][0]


async def login(hass: HomeAssistant, result: dict) -> dict:
    return await hass.config_entries.flow.async_configure(result["flow_id"], REDIRECT)


async def test_first_account_setup(hass, fake_inpost):
    """Full flow: login, favourite pre-selected, entry created and loaded."""
    result = await start_user_flow(hass)
    placeholders = result["description_placeholders"]
    assert "code_challenge=" in placeholders["login_url"]
    assert placeholders["callback_url"] == (
        "https://account.inpost-group.com/callback?code=..."
    )

    result = await login(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "lockers"
    assert not result["errors"]
    exchange = fake_inpost.last(TOKEN_PATH)["data"]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "CODE123"
    assert exchange["code_verifier"]
    assert fake_inpost.count(PROFILE_PATH) == 1  # one request: account + favourites

    schema = result["data_schema"].schema
    key = next(iter(schema))
    assert key.default() == [LOCKER]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"lockers": [LOCKER, " waw01m "]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "InPost: +48123456789"
    entry = result["result"]
    assert entry.unique_id == ACCOUNT_ID == "+48123456789"
    # The account is identified by the unique ID alone; data is credentials
    assert set(entry.data) == {
        "access_token",
        "refresh_token",
        "token_expires_in",
        "token_type",
    }
    assert entry.data["refresh_token"] == "refresh-rotated"
    assert [item["code"] for item in entry.options["lockers"]] == [LOCKER, "WAW01M"]
    assert entry.options["lockers"][1]["description"] == "opis WAW01M"
    assert entry.state is ConfigEntryState.LOADED


async def test_same_account_cannot_be_added_twice(hass, fake_inpost):
    """A second flow for an already configured account is aborted."""
    entry = make_entry()
    entry.add_to_hass(hass)

    result = await login(hass, await start_user_flow(hass))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize(
    "profile",
    [
        make_profile(prefix="48"),
        make_profile(prefix="0048"),
        make_profile(prefix=" +48 ", phone="123 456 789"),
    ],
    ids=["no-plus", "double-zero", "spaces"],
)
async def test_same_account_is_recognised_however_the_number_is_written(
    hass, fake_inpost, profile
):
    """The account ID does not depend on how InPost formats the number."""
    make_entry().add_to_hass(hass)
    fake_inpost.profile = profile

    result = await login(hass, await start_user_flow(hass))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_same_national_number_under_another_prefix_is_another_account(
    hass, fake_inpost
):
    """+380 123456789 is not +48 123456789: it gets its own entry.

    Regression: accounts used to be identified by the national number alone,
    so the second one was rejected as "already configured".
    """
    polish = make_entry()
    polish.add_to_hass(hass)
    fake_inpost.profile = make_profile(prefix="+380", favorites=())

    result = await login(hass, await start_user_flow(hass))
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "lockers"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"lockers": [LOCKER]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "InPost: +380123456789"
    assert result["result"].unique_id == "+380123456789"
    assert polish.unique_id == "+48123456789"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


@pytest.mark.parametrize(
    "profile",
    [make_profile(prefix=None), make_profile(prefix=""), make_profile(prefix="+")],
    ids=["missing", "empty", "no-digits"],
)
async def test_profile_without_country_prefix_does_not_identify_an_account(
    hass, fake_inpost, profile
):
    """No entry is ever created under a number that lacks its prefix."""
    fake_inpost.profile = profile

    result = await login(hass, await start_user_flow(hass))

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "profile_failed"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_second_different_account(hass, fake_inpost):
    """Different accounts coexist."""
    make_entry().add_to_hass(hass)
    fake_inpost.profile = make_profile(phone="987654321", favorites=())

    result = await login(hass, await start_user_flow(hass))
    assert result["step_id"] == "lockers"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"lockers": [LOCKER]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "+48987654321"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


@pytest.mark.parametrize(
    "token_response",
    [
        HttpResponse(body={"error": "invalid_grant"}, status=400),
        HttpResponse(body={"access_token": "only-access"}, status=200),
        HttpResponse(body="<html>", status=200),
    ],
    ids=["invalid-grant", "no-refresh-token", "not-json"],
)
async def test_failed_code_exchange_shows_error_and_allows_retry(
    hass, fake_inpost, token_response
):
    """A failed code exchange keeps the user on the login form."""
    fake_inpost.token_queue = [token_response]
    result = await start_user_flow(hass)
    pkce = _pkce(result)

    result = await login(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth_response"}
    # PKCE stays the same so the code from the open browser tab remains valid
    assert _pkce(result) == pkce

    result = await login(hass, result)
    assert result["step_id"] == "lockers"


async def test_state_mismatch_is_rejected(hass, fake_inpost):
    """A redirect belonging to another login attempt is not exchanged."""
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"redirect_url": "https://x/callback?code=CODE&state=someone-elses"},
    )
    assert result["errors"] == {"base": "invalid_auth_response"}
    assert fake_inpost.count(TOKEN_PATH) == 0


async def test_tokens_rotated_during_login_are_the_ones_stored(hass, fake_inpost):
    """If the profile request has to renew the token, the entry gets the new one."""
    fake_inpost.token_queue = [
        HttpResponse(
            body={"access_token": make_jwt(marker="first"), "refresh_token": "r-1"},
            status=200,
        ),
        HttpResponse(
            body={"access_token": make_jwt(marker="second"), "refresh_token": "r-2"},
            status=200,
        ),
    ]
    fake_inpost.profile_queue = [HttpResponse(body={}, status=401)]

    result = await login(hass, await start_user_flow(hass))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"lockers": [LOCKER]}
    )
    await hass.async_block_till_done()

    entry = result["result"]
    assert entry.data["refresh_token"] == "r-2"
    assert entry.data["access_token"].endswith("sig-second")


async def test_lockers_list_unavailable_allows_manual_codes(hass, fake_inpost):
    """The flow can be finished by typing codes when the list cannot be loaded."""
    fake_inpost.lockers_queue = [HttpResponse(body="error", status=503)]

    result = await login(hass, await start_user_flow(hass))
    assert result["step_id"] == "lockers"
    assert result["errors"] == {"base": "cannot_fetch_lockers"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"lockers": ["gda117m"]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options["lockers"] == [{"code": LOCKER}]


async def test_unlisted_locker_code_needs_confirmation(hass, fake_inpost):
    """A code missing from the public list is flagged once, then accepted.

    The list InPost publishes can be outdated, so a hard rejection would make
    newly opened lockers impossible to track.
    """
    result = await login(hass, await start_user_flow(hass))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"lockers": [LOCKER, "NEW99M"]}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"lockers": "unknown_locker"}

    # A different typo is flagged again
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"lockers": [LOCKER, "NEW98M"]}
    )
    assert result["errors"] == {"lockers": "unknown_locker"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"lockers": [LOCKER, "NEW99M"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options["lockers"][1] == {"code": "NEW99M"}


async def test_lockers_dropdown_is_limited_to_nearest(hass, fake_inpost):
    """Only the nearest lockers are offered; favourites are always included."""
    hass.config.latitude, hass.config.longitude = 54.3188, 18.58508
    items = [make_locker(f"L{i:04d}", lat=54.0 + i * 0.001) for i in range(1000)]
    items.append(make_locker("FAR01M", lat=49.0, lon=22.0))
    fake_inpost.lockers = {"date": "x", "page": 1, "total_pages": 1, "items": items}
    fake_inpost.profile = make_profile(favorites=("FAR01M",))

    result = await login(hass, await start_user_flow(hass))
    selector = next(iter(result["data_schema"].schema.values()))
    values = [option["value"] for option in selector.config["options"]]

    assert len(values) == 301
    assert "FAR01M" in values
    assert selector.config["custom_value"] is True


# =============================================================================
# Re-authentication
# =============================================================================


async def _setup_entry_needing_reauth(hass, fake_inpost):
    fake_inpost.token_queue = [
        HttpResponse(body={"error": "invalid_grant"}, status=400)
    ]
    entry = make_entry(access_token=make_jwt(-10))
    entry.add_to_hass(hass)
    # Entities registered by an earlier successful run
    registry = er.async_get(hass)
    known = registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_account_total_count", config_entry=entry
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["step_id"] == "reauth_confirm"
    return entry, flows[0]["flow_id"], known


async def test_reauth_restores_the_same_entry(hass, fake_inpost):
    """Reauth stores new tokens in place: entry and entities are preserved."""
    entry, flow_id, known = await _setup_entry_needing_reauth(hass, fake_inpost)
    options_before = dict(entry.options)

    result = await hass.config_entries.flow.async_configure(flow_id, REDIRECT)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["refresh_token"] == "refresh-rotated"
    assert entry.data["access_token"].endswith("sig-new")
    assert entry.options == options_before
    registry = er.async_get(hass)
    assert registry.async_get(known.entity_id).id == known.id
    assert not hass.config_entries.flow.async_progress()


async def test_reauth_with_another_account_is_refused(hass, fake_inpost):
    """Tokens of a different account must not replace the entry's tokens."""
    entry, flow_id, _ = await _setup_entry_needing_reauth(hass, fake_inpost)
    old_refresh = entry.data["refresh_token"]
    fake_inpost.profile = make_profile(phone="555000111")

    result = await hass.config_entries.flow.async_configure(flow_id, REDIRECT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data["refresh_token"] == old_refresh
    assert entry.unique_id == ACCOUNT_ID


async def test_reauth_with_same_number_under_another_prefix_is_refused(
    hass, fake_inpost
):
    """The other country's account must not take over the entry.

    Regression: only the national number was compared, so this login was
    accepted and the entry silently switched to the other account's tokens.
    """
    entry, flow_id, _ = await _setup_entry_needing_reauth(hass, fake_inpost)
    data_before = dict(entry.data)
    fake_inpost.profile = make_profile(prefix="+380")

    result = await hass.config_entries.flow.async_configure(flow_id, REDIRECT)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert dict(entry.data) == data_before
    assert entry.unique_id == "+48123456789"
    assert entry.state is ConfigEntryState.SETUP_ERROR


@pytest.mark.parametrize("legacy_unique_id", [None, PHONE], ids=["none", "national"])
async def test_reauth_gives_a_legacy_entry_its_account_id(
    hass, fake_inpost, legacy_unique_id
):
    """An entry that was never identified takes the account that signs in."""
    fake_inpost.token_queue = [
        HttpResponse(body={"error": "invalid_grant"}, status=400)
    ]
    entry = make_entry(access_token=make_jwt(-10), legacy_unique_id=legacy_unique_id)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    (flow,) = hass.config_entries.flow.async_progress()

    result = await hass.config_entries.flow.async_configure(flow["flow_id"], REDIRECT)
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == ACCOUNT_ID
    assert entry.state is ConfigEntryState.LOADED
    assert fake_inpost.count(PROFILE_PATH) == 1  # setup did not ask again


async def test_reauth_error_keeps_form_open(hass, fake_inpost):
    """A failed login during reauth can be retried."""
    entry, flow_id, _ = await _setup_entry_needing_reauth(hass, fake_inpost)
    fake_inpost.token_queue = [
        HttpResponse(body={"error": "invalid_grant"}, status=400)
    ]

    result = await hass.config_entries.flow.async_configure(flow_id, REDIRECT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth_response"}


# =============================================================================
# Options flow
# =============================================================================


async def _loaded_entry(hass, lockers=(LOCKER,)):
    entry = make_entry(lockers=lockers)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _entity_ids(hass, entry):
    registry = er.async_get(hass)
    return {
        e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


async def test_options_flow_adds_and_removes_lockers(hass, fake_inpost):
    """Changing the selection reloads the entry and updates its entities."""
    entry = await _loaded_entry(hass)
    data_before = dict(entry.data)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert next(iter(result["data_schema"].schema)).default() == [LOCKER]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"lockers": ["GDA145M"]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [item["code"] for item in entry.options["lockers"]] == ["GDA145M"]
    assert entry.data == data_before
    assert entry.state is ConfigEntryState.LOADED
    ids = _entity_ids(hass, entry)
    assert len(ids) == 14
    assert f"sensor.inpost_{ACCOUNT_SLUG}_gda145m_en_route_count" in ids
    assert not any(LOCKER.lower() in entity_id for entity_id in ids)


async def test_removing_a_locker_in_options_removes_its_device(hass, fake_inpost):
    """The dropped locker's device and entities go; the rest is left alone."""
    entry = await _loaded_entry(hass, lockers=(LOCKER, "GDA145M"))
    registry = dr.async_get(hass)

    def devices() -> dict[str, dr.DeviceEntry]:
        return {
            next(iter(device.identifiers))[1]: device
            for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        }

    before = devices()
    account = before[entry.entry_id]
    kept = before[f"{entry.entry_id}_{LOCKER}"]
    dropped = before[f"{entry.entry_id}_GDA145M"]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch.object(
        registry, "async_remove_device", wraps=registry.async_remove_device
    ) as remove_device:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"lockers": [LOCKER]}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.state is ConfigEntryState.LOADED
    remove_device.assert_called_once_with(dropped.id)
    after = devices()
    assert set(after) == {entry.entry_id, f"{entry.entry_id}_{LOCKER}"}
    assert after[entry.entry_id].id == account.id
    assert after[f"{entry.entry_id}_{LOCKER}"].id == kept.id
    assert after[f"{entry.entry_id}_{LOCKER}"].via_device_id == account.id
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entities) == 14
    assert {e.device_id for e in entities} == {account.id, kept.id}


async def test_setup_options_and_reload_raise_no_home_assistant_reports(
    hass, fake_inpost, caplog
):
    """Home Assistant has nothing to say about how its registries are used.

    Regression: device links (``via_device``) and the removal of a locker's
    device went through calls Home Assistant 2026.10 reports as deprecated.
    """
    entry = await _loaded_entry(hass, lockers=(LOCKER, "GDA145M"))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"lockers": [LOCKER, "WAW01M"]}
    )
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)

    assert not [
        record.getMessage()
        for record in caplog.records
        if record.name == "homeassistant.helpers.frame"
    ]


async def test_options_flow_keeps_locker_data_when_list_is_unavailable(
    hass, fake_inpost
):
    """A failed download must not strip stored locker descriptions."""
    entry = await _loaded_entry(hass)
    stored = entry.options["lockers"][0]
    fake_inpost.lockers_queue = [ConnectionResetError("down")]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["errors"] == {"base": "cannot_fetch_lockers"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"lockers": [LOCKER]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["lockers"] == [stored]


async def test_options_flow_flags_unlisted_code(hass, fake_inpost):
    """Unlisted codes need confirmation in the options flow as well."""
    entry = await _loaded_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"lockers": ["NEW99M"]}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"lockers": "unknown_locker"}
    assert [item["code"] for item in entry.options["lockers"]] == [LOCKER]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"lockers": ["NEW99M"]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["lockers"] == [{"code": "NEW99M"}]
    assert f"sensor.inpost_{ACCOUNT_SLUG}_new99m_en_route_count" in _entity_ids(
        hass, entry
    )


async def test_lockers_list_is_downloaded_once_and_cached(hass, fake_inpost):
    """Re-opening the options dialog does not download 8 MB again."""
    entry = await _loaded_entry(hass)
    for _ in range(3):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        hass.config_entries.options.async_abort(result["flow_id"])
    assert fake_inpost.count(LOCKERS_URL) == 1


async def test_options_flow_reads_legacy_code_list(hass, fake_inpost):
    """Options stored as a plain list of codes are still understood."""
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"lockers": [LOCKER]})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(_entity_ids(hass, entry)) == 14

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert next(iter(result["data_schema"].schema)).default() == [LOCKER]
