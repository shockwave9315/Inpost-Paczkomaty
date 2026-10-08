"""Base entities and identity helpers for InPost Paczkomaty.

Identity scheme (all scoped to the config entry, so several accounts can track
the same parcel locker without colliding):

* account device  - identifier ``<entry_id>``
* locker device   - identifier ``<entry_id>_<LOCKER>``
* account entity  - unique_id ``<entry_id>_account_<key>``
* locker entity   - unique_id ``<entry_id>_locker_<LOCKER>_<key>``

Entity names are relative to the device name (``has_entity_name``), which
yields ``sensor.inpost_<phone>_...`` and ``sensor.inpost_<phone>_<locker>_...``.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOCKERS, DOMAIN, ENTRY_PHONE_NUMBER_CONFIG
from .coordinator import InpostDataCoordinator


def get_tracked_lockers(entry: ConfigEntry) -> dict[str, dict[str, Any]]:
    """Return tracked lockers from entry options as ``{code: locker data}``.

    Accepts both the current format (list of dicts with a ``code`` key) and
    the original one (plain list of locker codes).
    """
    lockers: dict[str, dict[str, Any]] = {}
    for item in entry.options.get(CONF_LOCKERS, []):
        if isinstance(item, dict):
            code = item.get("code")
            data = item
        else:
            code = item
            data = {"code": item}
        if code:
            lockers[str(code)] = data
    return lockers


def account_label(entry: ConfigEntry) -> str:
    """Return the label identifying the account in device names."""
    return entry.data.get(ENTRY_PHONE_NUMBER_CONFIG) or entry.entry_id[:8]


def account_device_identifier(entry: ConfigEntry) -> tuple[str, str]:
    """Return the device registry identifier of the account device."""
    return (DOMAIN, entry.entry_id)


def locker_device_identifier(entry: ConfigEntry, locker_id: str) -> tuple[str, str]:
    """Return the device registry identifier of a tracked locker."""
    return (DOMAIN, f"{entry.entry_id}_{locker_id}")


def account_unique_id_prefix(entry: ConfigEntry) -> str:
    """Return the unique_id prefix shared by all account entities."""
    return f"{entry.entry_id}_account_"


def locker_unique_id_prefix(entry: ConfigEntry, locker_id: str) -> str:
    """Return the unique_id prefix shared by all entities of a locker."""
    return f"{entry.entry_id}_locker_{locker_id}_"


class InPostAccountEntity(CoordinatorEntity[InpostDataCoordinator]):
    """Base class for entities describing the whole InPost account."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: InpostDataCoordinator, entry: ConfigEntry, key: str
    ) -> None:
        """Initialize the account entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{account_unique_id_prefix(entry)}{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={account_device_identifier(entry)},
            name=f"InPost {account_label(entry)}",
            manufacturer="InPost",
            model="Account",
            entry_type=DeviceEntryType.SERVICE,
        )


class InPostLockerEntityMixin(Entity):
    """Identity shared by all entities of a tracked parcel locker."""

    _attr_has_entity_name = True

    def _init_locker(self, entry: ConfigEntry, locker_id: str, key: str) -> None:
        """Set unique_id and device info for a locker entity."""
        self._locker_id = locker_id
        self._attr_unique_id = f"{locker_unique_id_prefix(entry, locker_id)}{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={locker_device_identifier(entry, locker_id)},
            name=f"InPost {account_label(entry)} {locker_id}",
            manufacturer="InPost",
            model="Paczkomat",
            via_device=account_device_identifier(entry),
        )


class InPostLockerEntity(
    InPostLockerEntityMixin, CoordinatorEntity[InpostDataCoordinator]
):
    """Base class for locker entities backed by coordinator data."""

    def __init__(
        self,
        coordinator: InpostDataCoordinator,
        entry: ConfigEntry,
        locker_id: str,
        key: str,
    ) -> None:
        """Initialize the locker entity."""
        super().__init__(coordinator)
        self._init_locker(entry, locker_id, key)
