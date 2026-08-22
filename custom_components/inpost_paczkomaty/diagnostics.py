"""Diagnostics support for InPost Paczkomaty."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the latest privacy-preserving parcel discovery data."""
    return {"tracked_parcels": entry.runtime_data.api_client.parcel_diagnostics}
