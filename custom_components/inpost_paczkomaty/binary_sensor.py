"""Binary sensors for InPost Paczkomaty."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import InpostDataCoordinator
from .entity import InPostLockerEntity, get_tracked_lockers


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up binary sensors for every tracked parcel locker."""
    coordinator: InpostDataCoordinator = entry.runtime_data

    entities: list[BinarySensorEntity] = []
    for locker_id in get_tracked_lockers(entry):
        entities.append(
            ParcelLockerBinarySensor(
                coordinator, entry, locker_id, "en_route", "Parcels en route"
            )
        )
        entities.append(
            ParcelLockerBinarySensor(
                coordinator, entry, locker_id, "ready_for_pickup", "Ready for pickup"
            )
        )

    async_add_entities(entities)


class ParcelLockerBinarySensor(InPostLockerEntity, BinarySensorEntity):
    """On when the locker has at least one parcel in the given group."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(
        self,
        coordinator: InpostDataCoordinator,
        entry: ConfigEntry,
        locker_id: str,
        group: str,
        name: str,
    ) -> None:
        """Initialize the binary sensor.

        Args:
            coordinator: Data coordinator.
            entry: Config entry of the account.
            locker_id: Parcel locker code.
            group: ParcelsSummary attribute to read (en_route/ready_for_pickup).
            name: Entity name relative to the locker device.
        """
        super().__init__(coordinator, entry, locker_id, group)
        self._group = group
        self._attr_name = name

    @property
    def is_on(self) -> bool:
        """Return True if any parcel of the group is assigned to the locker."""
        locker = getattr(self.coordinator.data, self._group).get(self._locker_id)
        return locker is not None and locker.count > 0
