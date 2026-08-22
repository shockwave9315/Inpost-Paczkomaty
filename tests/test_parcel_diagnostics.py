"""Tests for bounded, privacy-preserving parcel diagnostics traversal."""

import json

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


def test_dynamic_mapping_keys_are_fingerprinted_recursively():
    """Never expose identifier-like mapping keys in paths or summaries."""
    shipment_key = "620070123456789012345678"
    payload = {
        "shipment_number": "123456789012345678901234",
        "relations_by_shipment": {shipment_key: [{"group_id": "shared-private-group"}]},
    }

    first = build_parcel_diagnostics([payload])
    second = build_parcel_diagnostics([payload])
    serialized = json.dumps(first)
    candidates = first["records"][0]["grouping_candidates"]
    container = next(
        candidate
        for candidate in candidates
        if candidate["path"] == "relations_by_shipment"
    )
    nested = next(
        candidate
        for candidate in candidates
        if candidate["path"].endswith("[0].group_id")
    )

    assert shipment_key not in serialized
    assert "shared-private-group" not in serialized
    assert container["value"]["keys"][0].startswith("<key:sha256:")
    assert ".<key:sha256:" in nested["path"]
    assert first == second


def test_grouping_container_fingerprints_are_private_and_content_aware():
    """Compare bounded container contents rather than only their shapes."""
    records = [
        {
            "linked_parcels": ["parcel-alpha", "parcel-beta"],
            "relation_map": {"group_id": "private-group", "relation_id": 7},
        },
        {
            "linked_parcels": ["parcel-alpha", "parcel-beta"],
            "relation_map": {"relation_id": 7, "group_id": "private-group"},
        },
        {
            "linked_parcels": ["parcel-alpha", "parcel-other"],
            "relation_map": {"group_id": "different-group", "relation_id": 7},
        },
    ]

    diagnostics = build_parcel_diagnostics(records)

    def fingerprint(record_index: int, path: str) -> str:
        candidate = next(
            item
            for item in diagnostics["records"][record_index]["grouping_candidates"]
            if item["path"] == path
        )
        return candidate["value"]["fingerprint"]

    assert fingerprint(0, "linked_parcels") == fingerprint(1, "linked_parcels")
    assert fingerprint(0, "linked_parcels") != fingerprint(2, "linked_parcels")
    assert fingerprint(0, "relation_map") == fingerprint(1, "relation_map")
    assert fingerprint(0, "relation_map") != fingerprint(2, "relation_map")

    serialized = json.dumps(diagnostics)
    for private_value in (
        "parcel-alpha",
        "parcel-beta",
        "parcel-other",
        "private-group",
        "different-group",
    ):
        assert private_value not in serialized
