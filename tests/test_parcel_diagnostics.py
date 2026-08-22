"""Tests for bounded, privacy-preserving parcel diagnostics traversal."""

from custom_components.inpost_paczkomaty.parcel_diagnostics import (
    build_parcel_diagnostics,
)


def test_grouping_discovery_limits_list_items_and_depth():
    """Keep traversal bounded for unusually large or deeply nested payloads."""
    relations = [{"group_id": f"group-{index}"} for index in range(51)]
    nested = {"group_id": "too-deep"}
    for _ in range(9):
        nested = {"child": nested}

    diagnostics = build_parcel_diagnostics(
        [
            {
                "shipment_number": "123456789012345678901234",
                "relations": relations,
                "nested": nested,
            }
        ]
    )
    candidates = diagnostics["records"][0]["grouping_candidates"]
    paths = {candidate["path"] for candidate in candidates}

    assert "relations[49].group_id" in paths
    assert "relations[50].group_id" not in paths
    assert all(path.startswith("relations[") for path in paths)
