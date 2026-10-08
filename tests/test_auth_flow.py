"""Auth state machine of the config and re-authentication flows.

Every test drives the real flow through the fake InPost backend; only the HTTP
transport is replaced. Each scenario runs for a new account ("user") and for
re-authentication of an existing entry ("reauth").

Failure classes and the contract they must honour:

* permanent auth failure (tokens rejected)  -> fresh login state, new PKCE
* transient failure (timeout, 5xx, 429...)  -> tokens kept, retry offered
* user/redirect error                       -> pending login (PKCE) kept
"""

from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType

from custom_components.inpost_paczkomaty.const import DOMAIN
from custom_components.inpost_paczkomaty.exceptions import RequestTimeoutError
from custom_components.inpost_paczkomaty.models import HttpResponse

from .common import (
    LOCKER,
    PARCELS_PATH,
    PHONE,
    PROFILE_PATH,
    TOKEN_PATH,
    make_entry,
    make_jwt,
    make_profile,
)

SOURCES = [SOURCE_USER, SOURCE_REAUTH]
REJECTED = HttpResponse(body={"error": "invalid_grant"}, status=400)
UNAUTHORIZED = HttpResponse(body={}, status=401)
TRANSIENT = {
    "http-500": HttpResponse(body={}, status=500),
    "http-503-html": HttpResponse(body="<html>", status=503),
    "http-429": HttpResponse(body={}, status=429, headers={"Retry-After": "5"}),
    "http-403": HttpResponse(body={}, status=403),
    "not-json": HttpResponse(body="garbage", status=200),
    "timeout": RequestTimeoutError("Request timed out"),
    "connection": aiohttp.ClientConnectionError("refused"),
    "reset": ConnectionResetError("reset"),
}


def redirect(code: str) -> dict:
    return {"redirect_url": f"https://account.inpost-group.com/callback?code={code}"}


def tokens(marker: str) -> HttpResponse:
    return HttpResponse(
        body={"access_token": make_jwt(marker=marker), "refresh_token": f"r-{marker}"},
        status=200,
    )


def exchanged_codes(fake_inpost) -> list[str]:
    """Authorization codes that were really sent to the token endpoint."""
    return [
        call["data"]["code"]
        for call in fake_inpost.calls
        if call["url"].endswith(TOKEN_PATH)
        and call["data"]["grant_type"] == "authorization_code"
    ]


def pkce(result: dict) -> str:
    """Code challenge of the login URL shown in a form."""
    url = result["description_placeholders"]["login_url"]
    return parse_qs(urlsplit(url).query)["code_challenge"][0]


class Flow:
    """A config or reauth flow under test."""

    def __init__(self, hass, fake_inpost, source):
        self.hass = hass
        self.fake = fake_inpost
        self.source = source
        self.entry = None
        self.flow_id = None
        self.entry_data_before = None

    async def start(self) -> dict:
        if self.source == SOURCE_USER:
            result = await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
            self.flow_id = result["flow_id"]
            assert result["step_id"] == "user"
            self.check_invariant()
            return result

        self.fake.token_queue = [REJECTED]
        self.entry = make_entry(access_token=make_jwt(-10))
        self.entry.add_to_hass(self.hass)
        await self.hass.config_entries.async_setup(self.entry.entry_id)
        await self.hass.async_block_till_done()
        assert self.entry.state is ConfigEntryState.SETUP_ERROR
        self.entry_data_before = dict(self.entry.data)
        self.fake.calls.clear()
        (flow,) = self.hass.config_entries.flow.async_progress()
        self.flow_id = flow["flow_id"]
        assert flow["step_id"] == "reauth_confirm"
        return await self.hass.config_entries.flow.async_configure(self.flow_id)

    @property
    def login_step(self) -> str:
        return "user" if self.source == SOURCE_USER else "reauth_confirm"

    @property
    def handler(self):
        return self.hass.config_entries.flow._progress[self.flow_id]

    def check_invariant(self) -> None:
        """A pending login and unvalidated tokens never coexist."""
        handler = self.handler
        assert handler._auth is None or handler._tokens is None
        if handler._tokens is None and handler._auth is None:
            assert handler._data == {} or handler._profile is not None

    def assert_entry_untouched(self) -> None:
        """A failed or unfinished reauth never modifies the config entry."""
        if self.entry is not None:
            assert dict(self.entry.data) == self.entry_data_before
            assert self.hass.config_entries.async_entries(DOMAIN) == [self.entry]
        else:
            assert not self.hass.config_entries.async_entries(DOMAIN)

    async def submit(self, code: str) -> dict:
        result = await self.hass.config_entries.flow.async_configure(
            self.flow_id, redirect(code)
        )
        await self.hass.async_block_till_done()
        return result

    async def choose(self, option: str) -> dict:
        result = await self.hass.config_entries.flow.async_configure(
            self.flow_id, {"next_step_id": option}
        )
        await self.hass.async_block_till_done()
        return result

    def assert_login_form(self, result: dict, error: str | None) -> None:
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == self.login_step
        assert result["errors"] == ({"base": error} if error else {})
        self.check_invariant()
        assert self.handler._auth is not None
        assert self.handler._tokens is None
        self.assert_entry_untouched()

    def assert_profile_menu(self, result: dict) -> None:
        assert result["type"] is FlowResultType.MENU
        assert result["step_id"] == "profile_failed"
        assert result["menu_options"] == ["retry_profile", "restart_login"]
        self.check_invariant()
        assert self.handler._tokens is not None
        self.assert_entry_untouched()

    async def assert_success(self, result: dict, marker: str) -> None:
        """The flow ends with the tokens identified by ``marker`` stored."""
        if self.source == SOURCE_USER:
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "lockers"
            result = await self.hass.config_entries.flow.async_configure(
                self.flow_id, {"lockers": [LOCKER]}
            )
            await self.hass.async_block_till_done()
            assert result["type"] is FlowResultType.CREATE_ENTRY
            entry = result["result"]
        else:
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == "reauth_successful"
            entry = self.entry
            assert self.hass.config_entries.async_entries(DOMAIN) == [entry]

        assert entry.state is ConfigEntryState.LOADED
        assert entry.unique_id == PHONE
        assert entry.data["phone_number"] == PHONE
        assert entry.data["access_token"].endswith(f"sig-{marker}")
        assert entry.data["refresh_token"] == f"r-{marker}"
        # The stored token is the one the running integration really uses
        assert self.fake.last(PARCELS_PATH)["headers"]["Authorization"] == (
            f"Bearer {entry.data['access_token']}"
        )
        assert not self.hass.config_entries.flow.async_progress()


@pytest.fixture(params=SOURCES)
async def flow(request, hass, fake_inpost):
    return Flow(hass, fake_inpost, request.param)


# =============================================================================
# Happy paths
# =============================================================================


async def test_login_and_profile_ok(flow):
    await flow.start()
    flow.fake.token_queue = [tokens("first")]
    await flow.assert_success(await flow.submit("CODE1"), "first")
    assert exchanged_codes(flow.fake) == ["CODE1"]


async def test_profile_401_recovered_by_refresh_keeps_the_login(flow):
    """401 on the profile + successful refresh is not an auth failure."""
    await flow.start()
    flow.fake.token_queue = [tokens("first"), tokens("refreshed")]
    flow.fake.profile_queue = [UNAUTHORIZED]

    await flow.assert_success(await flow.submit("CODE1"), "refreshed")
    assert exchanged_codes(flow.fake) == ["CODE1"]
    assert flow.fake.count(PROFILE_PATH) == 2


# =============================================================================
# Permanent authentication failures -> fresh login state
# =============================================================================


@pytest.mark.parametrize(
    ("token_responses", "profile_responses"),
    [
        ([REJECTED], [UNAUTHORIZED]),
        ([UNAUTHORIZED], [UNAUTHORIZED]),
        ([tokens("refreshed")], [UNAUTHORIZED, UNAUTHORIZED]),
    ],
    ids=["refresh-invalid-grant", "refresh-401", "401-after-successful-refresh"],
)
async def test_rejected_tokens_reset_auth_state_and_new_login_works(
    flow, token_responses, profile_responses
):
    """The reported bug: stale tokens must not suppress the next exchange."""
    first_pkce = pkce(await flow.start())
    flow.fake.token_queue = [tokens("first"), *token_responses]
    flow.fake.profile_queue = list(profile_responses)

    result = await flow.submit("CODE1")
    flow.assert_login_form(result, "auth_rejected")
    assert flow.handler._data == {}
    assert pkce(result) != first_pkce  # a really new login session

    flow.fake.token_queue = [tokens("second")]
    result = await flow.submit("CODE2")
    assert exchanged_codes(flow.fake) == ["CODE1", "CODE2"]
    exchange = [c for c in flow.fake.calls if c["url"].endswith(TOKEN_PATH)][-1]
    assert exchange["data"]["grant_type"] == "authorization_code"
    await flow.assert_success(result, "second")


async def test_two_consecutive_auth_failures_then_success(flow):
    await flow.start()
    challenges = set()
    for attempt in ("CODE1", "CODE2"):
        flow.fake.token_queue = [tokens(attempt), REJECTED]
        flow.fake.profile_queue = [UNAUTHORIZED]
        result = await flow.submit(attempt)
        flow.assert_login_form(result, "auth_rejected")
        challenges.add(pkce(result))
    assert len(challenges) == 2

    flow.fake.token_queue = [tokens("third")]
    result = await flow.submit("CODE3")
    assert exchanged_codes(flow.fake) == ["CODE1", "CODE2", "CODE3"]
    await flow.assert_success(result, "third")


# =============================================================================
# Transient failures -> tokens kept
# =============================================================================


@pytest.mark.parametrize("failure", TRANSIENT.values(), ids=TRANSIENT.keys())
async def test_transient_profile_failure_keeps_tokens(flow, failure):
    """Timeouts and 5xx never force a new login or drop the new tokens."""
    await flow.start()
    flow.fake.token_queue = [tokens("first")]
    flow.fake.profile_queue = [failure]

    result = await flow.submit("CODE1")
    flow.assert_profile_menu(result)
    assert flow.handler._tokens.refresh_token == "r-first"

    result = await flow.choose("retry_profile")
    assert exchanged_codes(flow.fake) == ["CODE1"]  # the single-use code is not re-sent
    assert flow.fake.count(TOKEN_PATH) == 1
    await flow.assert_success(result, "first")


async def test_transient_failure_during_refresh_keeps_tokens(flow):
    """Profile 401 followed by an unreachable token endpoint is transient."""
    await flow.start()
    flow.fake.token_queue = [tokens("first"), HttpResponse(body={}, status=500)]
    flow.fake.profile_queue = [UNAUTHORIZED]

    result = await flow.submit("CODE1")
    flow.assert_profile_menu(result)

    flow.fake.token_queue = [tokens("refreshed")]
    flow.fake.profile_queue = [UNAUTHORIZED]
    await flow.assert_success(await flow.choose("retry_profile"), "refreshed")
    assert exchanged_codes(flow.fake) == ["CODE1"]


async def test_repeated_transient_failures_then_success(flow):
    await flow.start()
    flow.fake.token_queue = [tokens("first")]
    flow.fake.profile_queue = [TRANSIENT["timeout"], TRANSIENT["http-500"]]

    flow.assert_profile_menu(await flow.submit("CODE1"))
    flow.assert_profile_menu(await flow.choose("retry_profile"))
    await flow.assert_success(await flow.choose("retry_profile"), "first")


async def test_transient_then_permanent_failure_resets(flow):
    """Tokens kept through a transient error are still dropped once rejected."""
    await flow.start()
    flow.fake.token_queue = [tokens("first"), REJECTED]
    flow.fake.profile_queue = [TRANSIENT["http-500"], UNAUTHORIZED]

    flow.assert_profile_menu(await flow.submit("CODE1"))
    flow.assert_login_form(await flow.choose("retry_profile"), "auth_rejected")

    flow.fake.token_queue = [tokens("second")]
    await flow.assert_success(await flow.submit("CODE2"), "second")
    assert exchanged_codes(flow.fake) == ["CODE1", "CODE2"]


async def test_user_can_sign_in_again_instead_of_retrying(flow):
    """Choosing a new login discards the kept tokens and exchanges the new code."""
    first_pkce = pkce(await flow.start())
    flow.fake.token_queue = [tokens("first")]
    flow.fake.profile_queue = [TRANSIENT["http-500"]]
    flow.assert_profile_menu(await flow.submit("CODE1"))

    result = await flow.choose("restart_login")
    flow.assert_login_form(result, None)
    assert pkce(result) != first_pkce

    flow.fake.token_queue = [tokens("second")]
    result = await flow.submit("CODE2")
    assert exchanged_codes(flow.fake) == ["CODE1", "CODE2"]
    await flow.assert_success(result, "second")


async def test_profile_without_phone_number_offers_retry_or_new_login(flow):
    """An unusable profile is not an auth failure, yet a new login must work."""
    await flow.start()
    flow.fake.token_queue = [tokens("first")]
    flow.fake.profile_queue = [HttpResponse(body=make_profile(phone=None), status=200)]
    flow.assert_profile_menu(await flow.submit("CODE1"))

    flow.fake.profile_queue = [HttpResponse(body={"personal": None}, status=200)]
    flow.assert_profile_menu(await flow.choose("retry_profile"))

    flow.assert_login_form(await flow.choose("restart_login"), None)
    flow.fake.token_queue = [tokens("second")]
    await flow.assert_success(await flow.submit("CODE2"), "second")


# =============================================================================
# User / redirect errors -> pending login kept
# =============================================================================


@pytest.mark.parametrize(
    "bad_input",
    ["   ", "https://x/callback?code=C&state=not-ours", "https://x/callback?code="],
    ids=["blank", "state-mismatch", "empty-code"],
)
async def test_unreadable_redirect_keeps_pending_login(flow, bad_input):
    first_pkce = pkce(await flow.start())

    result = await flow.hass.config_entries.flow.async_configure(
        flow.flow_id, {"redirect_url": bad_input}
    )
    flow.assert_login_form(result, "invalid_auth_response")
    assert pkce(result) == first_pkce
    assert flow.fake.count(TOKEN_PATH) == 0

    flow.fake.token_queue = [tokens("first")]
    await flow.assert_success(await flow.submit("CODE1"), "first")


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (REJECTED, "invalid_auth_response"),
        (HttpResponse(body={"access_token": "a"}, status=200), "invalid_auth_response"),
        (HttpResponse(body={}, status=500), "cannot_connect"),
        (HttpResponse(body={}, status=429), "cannot_connect"),
        (RequestTimeoutError("Request timed out"), "cannot_connect"),
        (aiohttp.ClientConnectionError("refused"), "cannot_connect"),
    ],
    ids=[
        "code-rejected",
        "incomplete-tokens",
        "http-500",
        "http-429",
        "timeout",
        "conn",
    ],
)
async def test_failed_exchange_keeps_pending_login_and_allows_resubmit(
    flow, response, error
):
    first_pkce = pkce(await flow.start())
    flow.fake.token_queue = [response]

    result = await flow.submit("CODE1")
    flow.assert_login_form(result, error)
    assert pkce(result) == first_pkce  # the open browser tab stays usable

    flow.fake.token_queue = [tokens("first")]
    result = await flow.submit("CODE1")
    assert exchanged_codes(flow.fake) == ["CODE1", "CODE1"]
    await flow.assert_success(result, "first")


async def test_login_form_shown_again_drops_unvalidated_tokens(flow):
    """Showing a login form always means "no tokens pending".

    Home Assistant does not route back to the login step while the menu is
    open, so the step is invoked directly: whatever leads there in the future
    must not produce a login URL next to tokens of an earlier login.
    """
    await flow.start()
    flow.fake.token_queue = [tokens("first")]
    flow.fake.profile_queue = [TRANSIENT["http-500"]]
    flow.assert_profile_menu(await flow.submit("CODE1"))

    handler = flow.handler
    result = await getattr(handler, f"async_step_{flow.login_step}")(None)

    assert result["step_id"] == flow.login_step
    assert handler._tokens is None
    assert handler._auth is not None
    assert handler._data == {}
    flow.assert_entry_untouched()
    await handler._async_reset_auth_state()


# =============================================================================
# Cancelling
# =============================================================================


@pytest.mark.parametrize("stage", ["pending-login", "tokens-kept", "after-rejection"])
async def test_cancelled_flow_leaves_no_state_behind(hass, fake_inpost, stage):
    """A new flow after a cancelled one starts from a clean state."""
    first = Flow(hass, fake_inpost, SOURCE_USER)
    first_pkce = pkce(await first.start())
    if stage == "tokens-kept":
        fake_inpost.token_queue = [tokens("first")]
        fake_inpost.profile_queue = [TRANSIENT["http-500"]]
        first.assert_profile_menu(await first.submit("CODE1"))
    elif stage == "after-rejection":
        fake_inpost.token_queue = [tokens("first"), REJECTED]
        fake_inpost.profile_queue = [UNAUTHORIZED]
        first.assert_login_form(await first.submit("CODE1"), "auth_rejected")
    auth = first.handler._auth
    hass.config_entries.flow.async_abort(first.flow_id)
    await hass.async_block_till_done()
    if auth is not None and auth._http_client.session is not None:
        assert auth._http_client.session.closed

    second = Flow(hass, fake_inpost, SOURCE_USER)
    assert pkce(await second.start()) != first_pkce
    fake_inpost.token_queue = [tokens("second")]
    result = await second.submit("CODE2")
    assert exchanged_codes(fake_inpost)[-1] == "CODE2"
    await second.assert_success(result, "second")


# =============================================================================
# Re-authentication specifics
# =============================================================================


async def test_reauth_with_another_account_is_refused_then_can_be_repeated(
    hass, fake_inpost
):
    """A wrong account never touches the entry; a later reauth still works."""
    flow = Flow(hass, fake_inpost, SOURCE_REAUTH)
    await flow.start()
    fake_inpost.token_queue = [tokens("other")]
    fake_inpost.profile_queue = [
        HttpResponse(body=make_profile(phone="555000111"), status=200)
    ]

    result = await flow.submit("CODE1")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    flow.assert_entry_untouched()

    flow.entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    (pending,) = hass.config_entries.flow.async_progress()
    flow.flow_id = pending["flow_id"]
    fake_inpost.token_queue = [tokens("mine")]
    await flow.assert_success(await flow.submit("CODE2"), "mine")


async def test_reauth_updates_only_its_own_entry(hass, fake_inpost):
    """With two accounts, reauth of one leaves the other entry alone."""
    other = make_entry(phone="987654321", refresh_token="other-refresh")
    other.add_to_hass(hass)
    await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()
    other_data = dict(other.data)

    flow = Flow(hass, fake_inpost, SOURCE_REAUTH)
    fake_inpost.token_queue = [REJECTED]
    flow.entry = make_entry(access_token=make_jwt(-10))
    flow.entry.add_to_hass(hass)
    await hass.config_entries.async_setup(flow.entry.entry_id)
    await hass.async_block_till_done()
    (pending,) = hass.config_entries.flow.async_progress()
    flow.flow_id = pending["flow_id"]

    fake_inpost.token_queue = [tokens("mine")]
    result = await flow.submit("CODE1")
    assert result["reason"] == "reauth_successful"
    assert flow.entry.data["refresh_token"] == "r-mine"
    assert dict(other.data) == other_data
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


async def test_reauth_into_account_of_another_entry_is_refused(hass, fake_inpost):
    """An entry without a known phone number cannot adopt a configured account."""
    other = make_entry(phone="987654321")
    other.add_to_hass(hass)

    flow = Flow(hass, fake_inpost, SOURCE_REAUTH)
    fake_inpost.token_queue = [REJECTED]
    flow.entry = make_entry(phone="", unique_id=None, access_token=make_jwt(-10))
    flow.entry.add_to_hass(hass)
    await hass.config_entries.async_setup(flow.entry.entry_id)
    await hass.async_block_till_done()
    before = dict(flow.entry.data)
    (pending,) = hass.config_entries.flow.async_progress()
    flow.flow_id = pending["flow_id"]

    fake_inpost.token_queue = [tokens("x")]
    fake_inpost.profile_queue = [
        HttpResponse(body=make_profile(phone="987654321"), status=200)
    ]
    result = await flow.submit("CODE1")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert dict(flow.entry.data) == before


# =============================================================================
# Running integration: token refresh outcomes
# =============================================================================


async def _loaded_entry(hass, fake_inpost):
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _reauth_flows(hass):
    return [
        f
        for f in hass.config_entries.flow.async_progress()
        if f["context"]["source"] == SOURCE_REAUTH
    ]


@pytest.mark.parametrize(
    "refresh_failure",
    [
        HttpResponse(body={}, status=500),
        HttpResponse(body={}, status=429),
        HttpResponse(body="<html>", status=200),
        HttpResponse(body={"token_type": "Bearer"}, status=200),
        RequestTimeoutError("Request timed out"),
        aiohttp.ClientConnectionError("refused"),
    ],
    ids=["http-500", "http-429", "not-json", "no-access-token", "timeout", "conn"],
)
async def test_transient_refresh_failure_keeps_credentials(
    hass, fake_inpost, refresh_failure
):
    """A refresh that fails for non-auth reasons changes nothing and recovers."""
    entry = await _loaded_entry(hass, fake_inpost)
    coordinator = entry.runtime_data
    client = coordinator.api_client
    before = dict(entry.data)

    fake_inpost.parcels_queue = [UNAUTHORIZED]  # forces a refresh attempt
    fake_inpost.token_queue = [refresh_failure]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert not _reauth_flows(hass)
    assert dict(entry.data) == before
    assert client._refresh_token == before["refresh_token"]
    assert client._access_token == before["access_token"]

    await coordinator.async_refresh()
    assert coordinator.last_update_success


@pytest.mark.parametrize("status", [400, 401])
async def test_rejected_refresh_requests_reauth_and_keeps_entry(
    hass, fake_inpost, status
):
    """A rejected refresh asks for reauth; stored data is left for the flow."""
    entry = await _loaded_entry(hass, fake_inpost)
    coordinator = entry.runtime_data
    before = dict(entry.data)

    fake_inpost.parcels_queue = [UNAUTHORIZED]
    fake_inpost.token_queue = [HttpResponse(body={"error": "x"}, status=status)]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(_reauth_flows(hass)) == 1
    assert dict(entry.data) == before
    assert entry.state is ConfigEntryState.LOADED


async def test_reauth_request_is_withdrawn_when_credentials_work_again(
    hass, fake_inpost
):
    """No stale "sign in again" prompt once an update succeeds."""
    entry = await _loaded_entry(hass, fake_inpost)
    coordinator = entry.runtime_data

    fake_inpost.parcels_queue = [UNAUTHORIZED, UNAUTHORIZED]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(_reauth_flows(hass)) == 1

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    assert not _reauth_flows(hass)


async def test_reauth_of_a_running_entry_replaces_the_live_client_tokens(
    hass, fake_inpost
):
    """After reauth the running client, entry.data and the flow all agree."""
    entry = await _loaded_entry(hass, fake_inpost)
    fake_inpost.parcels_queue = [UNAUTHORIZED]
    fake_inpost.token_queue = [REJECTED]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    (pending,) = _reauth_flows(hass)

    fake_inpost.token_queue = [tokens("fresh")]
    result = await hass.config_entries.flow.async_configure(
        pending["flow_id"], redirect("CODE1")
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    client = entry.runtime_data.api_client
    assert client._refresh_token == entry.data["refresh_token"] == "r-fresh"
    assert client._access_token == entry.data["access_token"]
    assert entry.runtime_data.last_update_success
    assert not _reauth_flows(hass)


async def test_client_without_access_token_refreshes_before_the_first_request(
    hass, fake_inpost
):
    """Validity is not inferred from a token merely being present or absent."""
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "access_token": ""}
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert fake_inpost.count(TOKEN_PATH) == 1
    assert fake_inpost.count(PARCELS_PATH) == 1  # no unauthenticated probe request
    assert entry.data["refresh_token"] == "refresh-rotated"


async def test_unloaded_client_cannot_overwrite_newer_tokens(hass, fake_inpost):
    """A refresh finishing on a replaced client must not clobber entry.data."""
    entry = await _loaded_entry(hass, fake_inpost)
    old_client = entry.runtime_data.api_client

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "refresh_token": "from-reauth"}
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    new_client = entry.runtime_data.api_client
    assert new_client is not old_client

    await old_client.refresh_access_token()  # late refresh on the retired client
    assert entry.data["refresh_token"] == "from-reauth"
    assert new_client._refresh_token == "from-reauth"
    await old_client.close()
