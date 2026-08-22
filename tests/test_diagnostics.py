"""Tests for Home Assistant config-entry diagnostics."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.inpost_paczkomaty.diagnostics import (
    async_get_config_entry_diagnostics,
)


@pytest.mark.asyncio
async def test_config_entry_diagnostics_returns_client_snapshot():
    """Expose only the client's already-sanitized diagnostics snapshot."""
    snapshot = {
        "records": [
            {
                "shipment_suffix": "100001",
                "status": "READY_TO_PICKUP",
                "unknown_fields": [],
                "grouping_candidates": [],
            }
        ]
    }
    entry = MagicMock()
    entry.runtime_data = SimpleNamespace(
        api_client=SimpleNamespace(parcel_diagnostics=snapshot)
    )

    result = await async_get_config_entry_diagnostics(MagicMock(), entry)

    assert result == {"tracked_parcels": snapshot}
