"""Sensors for InPost Paczkomaty."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfMass
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import InpostDataCoordinator
from .entity import (
    InPostAccountEntity,
    InPostLockerEntity,
    InPostLockerEntityMixin,
    get_tracked_lockers,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up account sensors and sensors for every tracked parcel locker."""
    coordinator: InpostDataCoordinator = entry.runtime_data

    entities: list[SensorEntity] = [
        # Global sensors
        AllParcelsCount(coordinator, entry),
        EnRouteParcelsCount(coordinator, entry),
        ReadyForPickupParcelsCount(coordinator, entry),
        # Parcels list sensor for dashboard markdown card
        ParcelsListSensor(coordinator, entry),
        # Carbon footprint sensors
        TotalCarbonFootprintSensor(coordinator, entry),
        TodayCarbonFootprintSensor(coordinator, entry),
        CarbonFootprintStatisticsSensor(coordinator, entry),
    ]

    for locker_id, locker_data in get_tracked_lockers(entry).items():
        entities.append(
            ParcelLockerCountSensor(
                coordinator, entry, locker_id, "en_route", "En route count"
            )
        )
        entities.append(
            ParcelLockerCountSensor(
                coordinator,
                entry,
                locker_id,
                "ready_for_pickup",
                "Ready for pickup count",
            )
        )
        entities.append(
            ParcelLockerStaticSensor(
                entry, locker_id, "locker_id", "Locker ID", locker_id
            )
        )
        entities.append(
            ParcelLockerStaticSensor(
                entry,
                locker_id,
                "description",
                "Description",
                locker_data.get("description", ""),
            )
        )
        entities.append(
            ParcelLockerStaticSensor(
                entry,
                locker_id,
                "address",
                "Address",
                "{}, {}, {} {}".format(
                    locker_data.get("city", ""),
                    locker_data.get("zip_code", ""),
                    locker_data.get("street", ""),
                    locker_data.get("building", ""),
                ),
            )
        )

    async_add_entities(entities)


# =============================================================================
# Account sensors
# =============================================================================


class AllParcelsCount(InPostAccountEntity, SensorEntity):
    """Number of all tracked parcels of the account."""

    _attr_name = "All parcels count"

    def __init__(self, coordinator: InpostDataCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, "total_count")

    @property
    def native_value(self) -> int:
        """Return the number of tracked parcels."""
        return self.coordinator.data.all_count


class EnRouteParcelsCount(InPostAccountEntity, SensorEntity):
    """Number of parcels en route to any destination."""

    _attr_name = "En route parcels count"

    def __init__(self, coordinator: InpostDataCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, "en_route_count")

    @property
    def native_value(self) -> int:
        """Return the number of parcels en route."""
        return self.coordinator.data.en_route_count


class ReadyForPickupParcelsCount(InPostAccountEntity, SensorEntity):
    """Number of parcels ready for pickup at any destination."""

    _attr_name = "Ready for pickup parcels count"

    def __init__(self, coordinator: InpostDataCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, "ready_for_pickup_count")

    @property
    def native_value(self) -> int:
        """Return the number of parcels ready for pickup."""
        return self.coordinator.data.ready_for_pickup_count


class ParcelsListSensor(InPostAccountEntity, SensorEntity):
    """Sensor with parcels list for dashboard markdown card display.

    This sensor provides lists of en_route and ready_for_pickup parcels
    as attributes that can be used with markdown cards with QR code
    generation via JavaScript.
    """

    _attr_name = "Parcels list"
    _attr_icon = "mdi:package-variant"
    # The lists are unbounded and carry pickup codes / QR payloads: keep them
    # in the live state for dashboards, but out of the recorder database
    # (which also rejects attribute sets above 16 kB).
    _unrecorded_attributes = frozenset({"ready_for_pickup", "en_route"})

    def __init__(self, coordinator: InpostDataCoordinator, entry: ConfigEntry) -> None:
        """Initialize the parcels list sensor."""
        super().__init__(coordinator, entry, "parcels_list")

    @property
    def native_value(self) -> int:
        """Return total active parcels count as state value."""
        data = self.coordinator.data
        return data.ready_for_pickup_count + data.en_route_count

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return parcels lists for dashboard display.

        Attributes include:
        - ready_for_pickup: List of parcels ready for pickup with QR codes
        - en_route: List of parcels in transit
        - ready_for_pickup_count: Count of parcels ready for pickup
        - en_route_count: Count of parcels en route
        - invalid_parcels_count: Parcels skipped because the API returned
          them in an unexpected format
        - has_more: True if the API reported more parcels than it returned
        """
        data = self.coordinator.data

        return {
            "ready_for_pickup": [p.to_dict() for p in data.ready_for_pickup_list],
            "en_route": [p.to_dict() for p in data.en_route_list],
            "ready_for_pickup_count": data.ready_for_pickup_count,
            "en_route_count": data.en_route_count,
            "invalid_parcels_count": data.invalid_parcels_count,
            "has_more": data.has_more,
        }


# =============================================================================
# Parcel locker sensors
# =============================================================================


class ParcelLockerCountSensor(InPostLockerEntity, SensorEntity):
    """Number of parcels of one group assigned to a tracked locker."""

    def __init__(
        self,
        coordinator: InpostDataCoordinator,
        entry: ConfigEntry,
        locker_id: str,
        group: str,
        name: str,
    ) -> None:
        """Initialize the sensor.

        Args:
            coordinator: Data coordinator.
            entry: Config entry of the account.
            locker_id: Parcel locker code.
            group: ParcelsSummary attribute to read (en_route/ready_for_pickup).
            name: Entity name relative to the locker device.
        """
        super().__init__(coordinator, entry, locker_id, f"{group}_count")
        self._group = group
        self._attr_name = name

    @property
    def native_value(self) -> int:
        """Return the number of parcels of the group in this locker."""
        locker = getattr(self.coordinator.data, self._group).get(self._locker_id)
        return locker.count if locker is not None else 0


class ParcelLockerStaticSensor(InPostLockerEntityMixin, SensorEntity):
    """Fixed information about a tracked locker (code, description, address)."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, entry: ConfigEntry, locker_id: str, key: str, name: str, value: str
    ) -> None:
        """Initialize the sensor with its constant value."""
        self._init_locker(entry, locker_id, key)
        self._attr_name = name
        self._attr_native_value = value


# =============================================================================
# Carbon Footprint Sensors
# =============================================================================


class TotalCarbonFootprintSensor(InPostAccountEntity, SensorEntity):
    """Sensor for total cumulative carbon footprint from delivered parcels."""

    _attr_name = "Total carbon footprint"
    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.KILOGRAMS
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:molecule-co2"

    def __init__(self, coordinator: InpostDataCoordinator, entry: ConfigEntry) -> None:
        """Initialize the total carbon footprint sensor."""
        super().__init__(coordinator, entry, "total_carbon_footprint")

    @property
    def native_value(self) -> float:
        """Return the total carbon footprint in kg."""
        stats = self.coordinator.data.carbon_footprint_stats
        if stats:
            return stats.total_co2_kg
        return 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        stats = self.coordinator.data.carbon_footprint_stats
        if stats:
            return {
                "total_parcels": stats.total_parcels,
                "total_co2_grams": stats.total_co2_grams,
            }
        return {}


class TodayCarbonFootprintSensor(InPostAccountEntity, SensorEntity):
    """Sensor for today's carbon footprint from delivered parcels."""

    _attr_name = "Today carbon footprint"
    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.KILOGRAMS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:molecule-co2"

    def __init__(self, coordinator: InpostDataCoordinator, entry: ConfigEntry) -> None:
        """Initialize today's carbon footprint sensor."""
        super().__init__(coordinator, entry, "today_carbon_footprint")

    def _today_entry(self):
        """Return (today's date string, matching daily data or None)."""
        # Same clock as the daily buckets: Home Assistant's local time zone.
        today = dt_util.now().strftime("%Y-%m-%d")
        stats = self.coordinator.data.carbon_footprint_stats
        if stats:
            for daily in stats.daily_data:
                if daily.date == today:
                    return today, daily
        return today, None

    @property
    def native_value(self) -> float:
        """Return today's carbon footprint in kg."""
        _, daily = self._today_entry()
        return round(daily.value, 4) if daily else 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        today, daily = self._today_entry()
        return {"parcel_count": daily.parcel_count if daily else 0, "date": today}


class CarbonFootprintStatisticsSensor(InPostAccountEntity, SensorEntity):
    """Sensor with daily carbon footprint statistics for graph visualization.

    This sensor provides daily breakdown data as attributes that can be used
    with ApexCharts, mini-graph-card, or other visualization cards.
    """

    _attr_name = "Carbon footprint statistics"
    _attr_icon = "mdi:chart-line"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfMass.KILOGRAMS
    # Grow with every delivery day; available live, not stored in the recorder.
    _unrecorded_attributes = frozenset({"daily_data", "cumulative_data"})

    def __init__(self, coordinator: InpostDataCoordinator, entry: ConfigEntry) -> None:
        """Initialize the carbon footprint statistics sensor."""
        super().__init__(coordinator, entry, "carbon_footprint_statistics")

    @property
    def native_value(self) -> float:
        """Return the total carbon footprint as state value."""
        stats = self.coordinator.data.carbon_footprint_stats
        if stats:
            return stats.total_co2_kg
        return 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return daily statistics and cumulative data for graphs.

        Attributes include:
        - daily_data: List of {date, value, parcel_count} for daily graphs
        - cumulative_data: List of {date, value} for cumulative graphs
        - total_co2_kg: Total carbon footprint
        - total_parcels: Total number of delivered parcels counted
        """
        stats = self.coordinator.data.carbon_footprint_stats
        if not stats:
            return {
                "daily_data": [],
                "cumulative_data": [],
                "total_co2_kg": 0.0,
                "total_parcels": 0,
            }

        # Build daily data list
        daily_data = [
            {
                "date": d.date,
                "value": round(d.value, 4),
                "parcel_count": d.parcel_count,
            }
            for d in stats.daily_data
        ]

        # Build cumulative data list
        cumulative_value = 0.0
        cumulative_data = []
        for d in stats.daily_data:
            cumulative_value += d.value
            cumulative_data.append(
                {
                    "date": d.date,
                    "value": round(cumulative_value, 4),
                }
            )

        return {
            "daily_data": daily_data,
            "cumulative_data": cumulative_data,
            "total_co2_kg": stats.total_co2_kg,
            "total_parcels": stats.total_parcels,
        }
