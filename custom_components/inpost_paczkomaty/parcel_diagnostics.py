"""Privacy-preserving diagnostics for tracked parcel API records."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from hashlib import sha256
import json
import re
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from .models import ApiParcel

_GROUPING_HINTS = (
    "multi",
    "group",
    "compartment",
    "linked",
    "relation",
    "bundle",
)
_SENSITIVE_HINTS = (
    "access_token",
    "refreshtoken",
    "refresh_token",
    "authorization",
    "auth",
    "opencode",
    "open_code",
    "qrcode",
    "qr_code",
    "phone",
    "password",
    "secret",
    "token",
    "pin",
    "code",
)
_MAX_TRAVERSAL_DEPTH = 8
_MAX_LIST_ITEMS = 50
_MAX_VISITED_NODES = 500
_FINGERPRINT_DOMAIN = b"inpost-paczkomaty:grouping-candidate:v1\0"
_CONTAINER_FINGERPRINT_DOMAIN = b"inpost-paczkomaty:grouping-container:v1\0"
_DYNAMIC_KEY_FINGERPRINT_DOMAIN = b"inpost-paczkomaty:dynamic-key:v1\0"
_SCHEMA_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _normalized_key(key: object) -> str:
    """Normalize a key for conservative privacy checks."""
    return str(key).lower().replace("-", "_").replace(" ", "_")


def _is_sensitive(key: object) -> bool:
    normalized = _normalized_key(key)
    compact = normalized.replace("_", "")
    return any(
        hint in normalized or hint.replace("_", "") in compact
        for hint in _SENSITIVE_HINTS
    )


def _is_grouping_candidate(key: object) -> bool:
    normalized = _normalized_key(key)
    return any(hint in normalized for hint in _GROUPING_HINTS)


def _diagnostic_key(key: object) -> str:
    """Return a schema key or a stable opaque segment for dynamic map keys."""
    text = str(key)
    if _SCHEMA_KEY.fullmatch(text):
        return text
    digest = sha256(_DYNAMIC_KEY_FINGERPRINT_DOMAIN + text.encode("utf-8")).hexdigest()[
        :24
    ]
    return f"<key:sha256:{digest}>"


def _dataclass_type(annotation: Any) -> type[Any] | None:
    """Return the dataclass represented by an optional/container annotation."""
    if isinstance(annotation, type) and is_dataclass(annotation):
        return annotation
    origin = get_origin(annotation)
    if origin in (Union, UnionType, list):
        for argument in get_args(annotation):
            result = _dataclass_type(argument)
            if result is not None:
                return result
    return None


def _model_fields(model: type[Any]) -> dict[str, type[Any] | None]:
    hints = get_type_hints(model)
    return {
        field.name: _dataclass_type(hints.get(field.name, field.type))
        for field in fields(model)
    }


def _bounded_fingerprint_value(
    value: Any, traversal: dict[str, int], depth: int = 0
) -> Any:
    """Build a bounded canonical value used only as fingerprint input."""
    if depth > _MAX_TRAVERSAL_DEPTH or traversal["nodes"] >= _MAX_VISITED_NODES:
        return ["limit"]
    traversal["nodes"] += 1
    if isinstance(value, dict):
        items = []
        for key, item in sorted(
            value.items(), key=lambda pair: _diagnostic_key(pair[0])
        ):
            if traversal["nodes"] >= _MAX_VISITED_NODES:
                break
            if _is_sensitive(key):
                continue
            items.append(
                [
                    _diagnostic_key(key),
                    _bounded_fingerprint_value(item, traversal, depth + 1),
                ]
            )
        return ["dict", items]
    if isinstance(value, list):
        return [
            "list",
            [
                _bounded_fingerprint_value(item, traversal, depth + 1)
                for item in value[:_MAX_LIST_ITEMS]
                if traversal["nodes"] < _MAX_VISITED_NODES
            ],
        ]
    value_type = f"{type(value).__module__}.{type(value).__qualname__}"
    return ["scalar", value_type, repr(value)]


def _container_fingerprint(value: dict[Any, Any] | list[Any]) -> str:
    canonical = _bounded_fingerprint_value(value, {"nodes": 0})
    encoded = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False).encode()
    digest = sha256(_CONTAINER_FINGERPRINT_DOMAIN + encoded).hexdigest()[:32]
    return f"sha256:{digest}"


def _structural_summary(value: Any) -> Any:
    """Summarize containers without copying their potentially private values."""
    if isinstance(value, dict):
        return {
            "type": "dict",
            "keys": sorted(
                _diagnostic_key(key) for key in value if not _is_sensitive(key)
            ),
            "size": len(value),
            "fingerprint": _container_fingerprint(value),
        }
    if isinstance(value, list):
        return {
            "type": "list",
            "size": len(value),
            "item_types": sorted({type(item).__name__ for item in value}),
            "fingerprint": _container_fingerprint(value),
        }
    if value is None or isinstance(value, bool):
        return value
    value_type = f"{type(value).__module__}.{type(value).__qualname__}"
    digest = sha256(
        _FINGERPRINT_DOMAIN
        + value_type.encode("utf-8")
        + b"\0"
        + repr(value).encode("utf-8")
    ).hexdigest()[:24]
    return {"type": type(value).__name__, "fingerprint": f"sha256:{digest}"}


def _inspect_mapping(
    value: dict[str, Any],
    model: type[Any] | None,
    path: str,
    unknown_fields: list[dict[str, str]],
    grouping_candidates: list[dict[str, Any]],
    traversal: dict[str, int],
    depth: int = 0,
) -> None:
    if depth > _MAX_TRAVERSAL_DEPTH or traversal["nodes"] >= _MAX_VISITED_NODES:
        return
    modeled = _model_fields(model) if model is not None else {}
    for key, item in value.items():
        if traversal["nodes"] >= _MAX_VISITED_NODES:
            return
        traversal["nodes"] += 1
        if _is_sensitive(key):
            continue
        safe_key = _diagnostic_key(key)
        item_path = f"{path}.{safe_key}" if path else safe_key
        child_model = modeled.get(key)
        if key not in modeled:
            unknown_fields.append({"path": item_path, "type": type(item).__name__})
            if _is_grouping_candidate(key):
                grouping_candidates.append(
                    {"path": item_path, "value": _structural_summary(item)}
                )
        if isinstance(item, dict):
            _inspect_mapping(
                item,
                child_model,
                item_path,
                unknown_fields,
                grouping_candidates,
                traversal,
                depth + 1,
            )
        elif isinstance(item, list):
            _inspect_list(
                item,
                child_model,
                item_path,
                unknown_fields,
                grouping_candidates,
                traversal,
                depth + 1,
            )


def _inspect_list(
    value: list[Any],
    model: type[Any] | None,
    path: str,
    unknown_fields: list[dict[str, str]],
    grouping_candidates: list[dict[str, Any]],
    traversal: dict[str, int],
    depth: int,
) -> None:
    """Inspect bounded container items without exposing scalar list contents."""
    if depth > _MAX_TRAVERSAL_DEPTH:
        return
    for index, item in enumerate(value[:_MAX_LIST_ITEMS]):
        if traversal["nodes"] >= _MAX_VISITED_NODES:
            return
        item_path = f"{path}[{index}]"
        if isinstance(item, dict):
            _inspect_mapping(
                item,
                model,
                item_path,
                unknown_fields,
                grouping_candidates,
                traversal,
                depth + 1,
            )
        elif isinstance(item, list):
            traversal["nodes"] += 1
            _inspect_list(
                item,
                model,
                item_path,
                unknown_fields,
                grouping_candidates,
                traversal,
                depth + 1,
            )


def build_parcel_diagnostics(raw_parcels: Any) -> dict[str, Any]:
    """Build a safe discovery view of raw tracked-parcel records."""
    records: list[dict[str, Any]] = []
    if not isinstance(raw_parcels, list):
        return {"records": records}

    for raw_record in raw_parcels:
        if not isinstance(raw_record, dict):
            continue
        unknown_fields: list[dict[str, str]] = []
        grouping_candidates: list[dict[str, Any]] = []
        _inspect_mapping(
            raw_record,
            ApiParcel,
            "",
            unknown_fields,
            grouping_candidates,
            {"nodes": 0},
        )
        shipment_number = raw_record.get("shipment_number")
        records.append(
            {
                "shipment_suffix": (
                    str(shipment_number)[-6:] if shipment_number is not None else None
                ),
                "status": raw_record.get("status"),
                "unknown_fields": unknown_fields,
                "grouping_candidates": grouping_candidates,
            }
        )
    return {"records": records}
