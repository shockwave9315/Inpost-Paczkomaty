"""Config flow for InPost Paczkomaty integration."""

from __future__ import annotations

import heapq
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .api import InPostApiClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_HTTP_TIMEOUT,
    CONF_LOCKERS,
    CONF_PARCEL_LOCKERS_URL,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRES_IN,
    CONF_TOKEN_TYPE,
    DATA_LOCKERS_CACHE,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_PARCEL_LOCKERS_URL,
    DOMAIN,
    ENTRY_PHONE_NUMBER_CONFIG,
    LOCKERS_CACHE_TTL,
    LOCKERS_SELECT_LIMIT,
    OAUTH_REDIRECT_URI,
)
from .entity import get_tracked_lockers
from .exceptions import ApiClientError, InPostApiError
from .inpost_auth_flow import InpostAuth
from .models import AuthTokens, UserProfile
from .utils import haversine

_LOGGER = logging.getLogger(__name__)

CONF_REDIRECT_URL = "redirect_url"


@dataclass
class SimpleParcelLocker:
    """Simple parcel locker data container."""

    code: str
    description: str
    city: str
    street: str
    building: str
    zip_code: str
    latitude: float
    longitude: float

    def as_option_data(self) -> dict[str, Any]:
        """Return the representation stored in the config entry options."""
        return {
            "code": self.code,
            "description": self.description,
            "city": self.city,
            "street": self.street,
            "building": self.building,
            "zip_code": self.zip_code,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


REDIRECT_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_REDIRECT_URL,
        ): TextSelector(TextSelectorConfig(type="text", multiline=True))
    }
)


# =============================================================================
# Parcel locker selection helpers (shared by the config and options flows)
# =============================================================================


async def async_get_parcel_lockers(
    hass: HomeAssistant,
) -> dict[str, SimpleParcelLocker]:
    """Return all parcel lockers keyed by code, using a time-limited cache.

    The public list is an ~8 MB download; the cache avoids repeating it every
    time the options dialog is opened.

    Raises:
        ApiClientError: If the list cannot be downloaded or read.
    """
    domain_config = hass.data.get(DOMAIN) or {}
    url = domain_config.get(CONF_PARCEL_LOCKERS_URL, DEFAULT_PARCEL_LOCKERS_URL)

    cached = hass.data.get(DATA_LOCKERS_CACHE)
    if cached and cached[0] == url and time.monotonic() - cached[1] < LOCKERS_CACHE_TTL:
        return cached[2]

    api_client = InPostApiClient(
        hass,
        http_timeout=domain_config.get(CONF_HTTP_TIMEOUT, DEFAULT_HTTP_TIMEOUT),
        parcel_lockers_url=url,
    )
    try:
        raw_lockers = await api_client.get_parcel_lockers_list()
    finally:
        await api_client.close()

    lockers = {
        locker.n: SimpleParcelLocker(
            code=locker.n,
            description=locker.d,
            city=locker.c,
            street=locker.e,
            building=locker.b,
            zip_code=locker.o,
            latitude=locker.l.a,
            longitude=locker.l.o,
        )
        for locker in raw_lockers
    }
    hass.data[DATA_LOCKERS_CACHE] = (url, time.monotonic(), lockers)
    return lockers


def normalize_locker_codes(codes: list[str] | None) -> list[str]:
    """Trim and upper-case locker codes, dropping blanks and duplicates."""
    result: list[str] = []
    for code in codes or []:
        normalized = str(code).strip().upper()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def build_lockers_schema(
    hass: HomeAssistant,
    lockers: dict[str, SimpleParcelLocker],
    selected: list[str],
) -> vol.Schema:
    """Build the locker selection form.

    Only the nearest lockers (plus the already selected ones) are listed to
    keep the form responsive; any other locker can be added by typing its code.
    """

    def distance(locker: SimpleParcelLocker) -> float:
        return haversine(
            hass.config.longitude,
            hass.config.latitude,
            locker.longitude,
            locker.latitude,
        )

    nearest = heapq.nsmallest(
        LOCKERS_SELECT_LIMIT,
        ((distance(locker), locker.code) for locker in lockers.values()),
    )
    listed = {code: dist for dist, code in nearest}
    for code in selected:
        if code in lockers and code not in listed:
            listed[code] = distance(lockers[code])

    options = [
        SelectOptionDict(
            label=(
                f"{code} [{dist:.2f}km] ({lockers[code].description} - "
                f"{lockers[code].city}, {lockers[code].street} "
                f"{lockers[code].building})"
            ),
            value=code,
        )
        for code, dist in sorted(listed.items(), key=lambda item: item[1])
    ]
    # Keep selected codes selectable even if the list could not be loaded
    options.extend(
        SelectOptionDict(label=code, value=code)
        for code in selected
        if code not in listed
    )

    return vol.Schema(
        {
            vol.Optional(CONF_LOCKERS, default=selected): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def resolve_selected_lockers(
    codes: list[str],
    lockers: dict[str, SimpleParcelLocker],
    previous: dict[str, dict[str, Any]],
    confirmed_unknown: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn selected codes into the data stored in the entry options.

    Codes missing from the public list are reported once so typos get caught,
    but can be confirmed by submitting again: the list InPost publishes may
    lag behind newly opened lockers.

    Args:
        codes: Normalized locker codes chosen by the user.
        lockers: Full lockers list (empty if it could not be loaded).
        previous: Locker data already stored for this entry.
        confirmed_unknown: Unlisted codes the user has already been warned
            about; updated in place with the ones reported by this call.

    Returns:
        Tuple of (locker data list, unlisted codes that need confirmation).
    """
    lockers_data: list[dict[str, Any]] = []
    unknown: list[str] = []
    for code in codes:
        if code in lockers:
            lockers_data.append(lockers[code].as_option_data())
        elif code in previous:
            lockers_data.append(previous[code])
        elif lockers and code not in confirmed_unknown:
            unknown.append(code)
        else:
            # Unverifiable (list unavailable) or explicitly confirmed code
            lockers_data.append({"code": code})
    confirmed_unknown.update(unknown)
    return lockers_data, unknown


# =============================================================================
# Config flow
# =============================================================================


class InPostConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle InPost Paczkomaty config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict = {}
        self._auth: InpostAuth | None = None
        self._profile: UserProfile | None = None
        self._lockers: dict[str, SimpleParcelLocker] = {}
        self._confirmed_unknown: set[str] = set()

    async def _cleanup_auth(self) -> None:
        """Clean up the authentication session."""
        if self._auth:
            await self._auth.close()
            self._auth = None

    @callback
    def async_remove(self) -> None:
        """Release the authentication session when the flow is discarded."""
        if self._auth:
            self.hass.async_create_task(self._auth.close())
            self._auth = None

    def _ensure_auth(self) -> InpostAuth:
        """Return the auth handler, keeping PKCE stable across form renders."""
        if self._auth is None:
            self._auth = InpostAuth(language=self.hass.config.language)
        return self._auth

    async def _async_fetch_profile(self) -> UserProfile:
        """Fetch the profile of the account that has just logged in.

        Raises:
            ApiClientError: If the profile cannot be fetched.
        """
        domain_config = self.hass.data.get(DOMAIN) or {}

        def keep_refreshed_tokens(tokens: AuthTokens) -> None:
            """Store tokens renewed while fetching, so the entry gets live ones."""
            self._data[CONF_ACCESS_TOKEN] = tokens.access_token
            self._data[CONF_REFRESH_TOKEN] = tokens.refresh_token
            self._data[CONF_TOKEN_EXPIRES_IN] = tokens.expires_in
            self._data[CONF_TOKEN_TYPE] = tokens.token_type

        api_client = InPostApiClient(
            self.hass,
            access_token=self._data.get(CONF_ACCESS_TOKEN),
            refresh_token=self._data.get(CONF_REFRESH_TOKEN),
            on_token_refresh=keep_refreshed_tokens,
            http_timeout=domain_config.get(CONF_HTTP_TIMEOUT, DEFAULT_HTTP_TIMEOUT),
        )
        try:
            return await api_client.get_profile()
        finally:
            await api_client.close()

    async def _async_login(self, user_input: dict[str, Any]) -> str | None:
        """Exchange the pasted redirect for tokens and identify the account.

        The account phone number is required: it identifies the account, so
        the same one cannot be added twice.

        Returns:
            None on success, otherwise the error key to show in the form.
        """
        if CONF_ACCESS_TOKEN not in self._data:
            auth = self._ensure_auth()
            try:
                auth_code = auth.extract_authorization_code(
                    user_input[CONF_REDIRECT_URL]
                )
                tokens = await auth.exchange_code_for_tokens(auth_code)
            except (
                InPostApiError,
                ValueError,
                aiohttp.ClientError,
                TimeoutError,
                OSError,
            ) as err:
                _LOGGER.warning("InPost authentication failed: %s", err)
                return "invalid_auth_response"

            self._data[CONF_ACCESS_TOKEN] = tokens.access_token
            self._data[CONF_REFRESH_TOKEN] = tokens.refresh_token
            self._data[CONF_TOKEN_EXPIRES_IN] = tokens.expires_in
            self._data[CONF_TOKEN_TYPE] = tokens.token_type
            await self._cleanup_auth()

        # The authorization code is single-use. If the profile request fails
        # the tokens are kept, so submitting the form again only retries it.
        try:
            self._profile = await self._async_fetch_profile()
        except ApiClientError as err:
            _LOGGER.warning("Failed to fetch InPost profile: %s", err)
            return "cannot_fetch_profile"

        personal = self._profile.personal
        if not personal or not personal.phone_number:
            _LOGGER.warning("InPost profile does not contain a phone number")
            return "cannot_fetch_profile"

        self._data[ENTRY_PHONE_NUMBER_CONFIG] = personal.phone_number
        return None

    def _show_login_form(self, step_id: str, errors: dict[str, str]):
        """Show the browser login form."""
        return self.async_show_form(
            step_id=step_id,
            data_schema=REDIRECT_SCHEMA,
            errors=errors,
            description_placeholders={
                "login_url": self._ensure_auth().build_login_url(),
                "callback_url": f"{OAUTH_REDIRECT_URI}?code=...",
            },
        )

    async def async_step_user(self, user_input=None):
        """Handle the initial step - external browser login.

        The user opens the InPost login URL in a browser and completes the login
        (phone number, SMS code, captcha and email confirmation). InPost then
        redirects the browser to ``.../callback?code=...``; the user pastes that
        URL (or the code) back here and we exchange it for tokens using our own
        PKCE ``code_verifier``.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await self._async_login(user_input)
            if error is None:
                phone_number = self._data[ENTRY_PHONE_NUMBER_CONFIG]
                await self.async_set_unique_id(phone_number)
                self._abort_if_unique_id_configured()
                # Entries created before unique IDs were introduced
                if any(
                    entry.data.get(ENTRY_PHONE_NUMBER_CONFIG) == phone_number
                    for entry in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                return await self.async_step_lockers()
            errors["base"] = error

        return self._show_login_form("user", errors)

    async def async_step_reauth(self, entry_data):
        """Start re-authentication after InPost rejected the stored tokens."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Log in again and store fresh tokens in the existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            error = await self._async_login(user_input)
            if error is None:
                phone_number = self._data[ENTRY_PHONE_NUMBER_CONFIG]
                expected = entry.data.get(ENTRY_PHONE_NUMBER_CONFIG)
                if expected and expected != phone_number:
                    return self.async_abort(reason="wrong_account")
                if any(
                    other.entry_id != entry.entry_id
                    and phone_number
                    in (other.unique_id, other.data.get(ENTRY_PHONE_NUMBER_CONFIG))
                    for other in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=phone_number,
                    data_updates=self._data,
                )
            errors["base"] = error

        return self._show_login_form("reauth_confirm", errors)

    def _entry_title(self) -> str:
        """Build the entry title from the account phone number."""
        personal = self._profile.personal if self._profile else None
        prefix = (personal.phone_number_prefix if personal else None) or ""
        phone_number = self._data.get(ENTRY_PHONE_NUMBER_CONFIG, "")
        return f"InPost: {prefix} {phone_number}".replace("  ", " ").strip()

    async def async_step_lockers(self, user_input=None):
        """Handle parcel locker selection step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = normalize_locker_codes(user_input.get(CONF_LOCKERS))
            lockers_data, unknown = resolve_selected_lockers(
                selected, self._lockers, {}, self._confirmed_unknown
            )
            if not unknown:
                return self.async_create_entry(
                    title=self._entry_title(),
                    data=self._data,
                    options={CONF_LOCKERS: lockers_data},
                )
            errors[CONF_LOCKERS] = "unknown_locker"
        else:
            # Pre-select favorite lockers from the profile
            favorites = (
                self._profile.get_favorite_locker_codes() if self._profile else []
            )
            try:
                self._lockers = await async_get_parcel_lockers(self.hass)
            except ApiClientError as err:
                _LOGGER.warning("Failed to fetch parcel lockers: %s", err)
                errors["base"] = "cannot_fetch_lockers"
            selected = [code for code in favorites if code in self._lockers]

        return self.async_show_form(
            step_id="lockers",
            data_schema=build_lockers_schema(self.hass, self._lockers, selected),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow handler."""
        return InPostOptionsFlow()


class InPostOptionsFlow(config_entries.OptionsFlow):
    """Handle InPost Paczkomaty options flow."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._lockers: dict[str, SimpleParcelLocker] = {}
        self._confirmed_unknown: set[str] = set()

    async def async_step_init(self, user_input=None):
        """Let the user change the tracked parcel lockers."""
        errors: dict[str, str] = {}
        entry = self.config_entry
        previous = get_tracked_lockers(entry)

        if user_input is not None:
            selected = normalize_locker_codes(user_input.get(CONF_LOCKERS))
            lockers_data, unknown = resolve_selected_lockers(
                selected, self._lockers, previous, self._confirmed_unknown
            )
            if not unknown:
                options_data = {**entry.options, CONF_LOCKERS: lockers_data}
                self.hass.config_entries.async_update_entry(entry, options=options_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_create_entry(title="", data=options_data)
            errors[CONF_LOCKERS] = "unknown_locker"
        else:
            try:
                self._lockers = await async_get_parcel_lockers(self.hass)
            except ApiClientError as err:
                _LOGGER.warning("Failed to fetch parcel lockers: %s", err)
                errors["base"] = "cannot_fetch_lockers"
            selected = list(previous)

        return self.async_show_form(
            step_id="init",
            data_schema=build_lockers_schema(self.hass, self._lockers, selected),
            errors=errors,
        )
