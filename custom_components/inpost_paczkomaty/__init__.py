"""InPost Paczkomaty integration for Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
import homeassistant.helpers.config_validation as cv

from .api import InPostApiClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_HTTP_TIMEOUT,
    CONF_IGNORED_EN_ROUTE_STATUSES,
    CONF_PARCEL_LOCKERS_URL,
    CONF_REFRESH_TOKEN,
    CONF_SHOW_ONLY_OWN_PARCELS,
    CONF_TOKEN_EXPIRES_IN,
    CONF_TOKEN_TYPE,
    CONF_UPDATE_INTERVAL,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_IGNORED_EN_ROUTE_STATUSES,
    DEFAULT_PARCEL_LOCKERS_URL,
    DEFAULT_SHOW_ONLY_OWN_PARCELS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ENTRY_PHONE_NUMBER_CONFIG,
)
from .coordinator import InpostDataCoordinator
from .entity import (
    account_device_identifier,
    account_unique_id_prefix,
    get_tracked_lockers,
    locker_device_identifier,
    locker_unique_id_prefix,
)
from .models import AuthTokens

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

# Schema for configuration.yaml
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(
                    CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
                ): cv.positive_int,
                vol.Optional(
                    CONF_IGNORED_EN_ROUTE_STATUSES,
                    default=DEFAULT_IGNORED_EN_ROUTE_STATUSES,
                ): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(
                    CONF_HTTP_TIMEOUT, default=DEFAULT_HTTP_TIMEOUT
                ): cv.positive_int,
                vol.Optional(
                    CONF_PARCEL_LOCKERS_URL, default=DEFAULT_PARCEL_LOCKERS_URL
                ): cv.url,
                vol.Optional(
                    CONF_SHOW_ONLY_OWN_PARCELS, default=DEFAULT_SHOW_ONLY_OWN_PARCELS
                ): cv.boolean,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the InPost Paczkomaty component from configuration.yaml."""
    if DOMAIN in config:
        hass.data[DOMAIN] = config[DOMAIN]
    else:
        hass.data[DOMAIN] = {}
    return True


@callback
def _async_adopt_unique_id(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Give entries created before unique IDs existed their account ID.

    This lets the config flow reject a second entry for the same account.
    """
    phone_number = entry.data.get(ENTRY_PHONE_NUMBER_CONFIG)
    if entry.unique_id is not None or not phone_number:
        return
    if any(
        other.unique_id == phone_number
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ):
        return
    hass.config_entries.async_update_entry(entry, unique_id=phone_number)


@callback
def _async_cleanup_registries(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove entities and devices this entry no longer provides.

    Covers parcel lockers removed in the options flow and entities registered
    under the identity scheme used before version 0.5.0.
    """
    lockers = get_tracked_lockers(entry)
    unique_id_prefixes = (
        account_unique_id_prefix(entry),
        *(locker_unique_id_prefix(entry, locker_id) for locker_id in lockers),
    )
    device_identifiers = {
        account_device_identifier(entry),
        *(locker_device_identifier(entry, locker_id) for locker_id in lockers),
    }

    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if not entity.unique_id.startswith(unique_id_prefixes):
            _LOGGER.debug("Removing stale entity %s", entity.entity_id)
            entity_registry.async_remove(entity.entity_id)

    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if not device.identifiers & device_identifiers:
            _LOGGER.debug("Removing stale device %s", device.name)
            device_registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up InPost Paczkomaty from a config entry."""
    _LOGGER.debug("Setting up InPost Paczkomaty entry %s", entry.entry_id)

    # Get configuration from configuration.yaml or use defaults
    domain_config = hass.data.get(DOMAIN, {})
    update_interval = domain_config.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
    ignored_en_route_statuses = domain_config.get(
        CONF_IGNORED_EN_ROUTE_STATUSES, DEFAULT_IGNORED_EN_ROUTE_STATUSES
    )
    http_timeout = domain_config.get(CONF_HTTP_TIMEOUT, DEFAULT_HTTP_TIMEOUT)
    parcel_lockers_url = domain_config.get(
        CONF_PARCEL_LOCKERS_URL, DEFAULT_PARCEL_LOCKERS_URL
    )
    show_only_own_parcels = domain_config.get(
        CONF_SHOW_ONLY_OWN_PARCELS, DEFAULT_SHOW_ONLY_OWN_PARCELS
    )

    client_is_current = True

    @callback
    def retire_client() -> None:
        """Stop this client from writing tokens once the entry moves on."""
        nonlocal client_is_current
        client_is_current = False

    entry.async_on_unload(retire_client)

    @callback
    def persist_refreshed_tokens(tokens: AuthTokens) -> None:
        """Persist refreshed OAuth tokens in the config entry.

        No update listener is registered for this entry, so storing the
        tokens never triggers a reload. A client that has been unloaded (for
        example replaced after re-authentication) must not overwrite the
        tokens its successor works with.
        """
        if not client_is_current:
            return
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token
                or entry.data.get(CONF_REFRESH_TOKEN, ""),
                CONF_TOKEN_EXPIRES_IN: tokens.expires_in,
                CONF_TOKEN_TYPE: tokens.token_type,
            },
        )

    api_client = InPostApiClient(
        hass,
        entry,
        on_token_refresh=persist_refreshed_tokens,
        ignored_en_route_statuses=ignored_en_route_statuses,
        http_timeout=http_timeout,
        parcel_lockers_url=parcel_lockers_url,
        show_only_own_parcels=show_only_own_parcels,
    )
    coordinator = InpostDataCoordinator(
        hass, api_client, update_interval, config_entry=entry
    )

    try:
        await coordinator.async_config_entry_first_refresh()
        _async_adopt_unique_id(hass, entry)
        _async_cleanup_registries(hass, entry)
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        # Setup did not complete, so async_unload_entry will not run.
        await api_client.close()
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.api_client.close()
    return unload_ok
