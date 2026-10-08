"""Shared helpers for integration-level tests."""

import base64
import json
import time
from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.inpost_paczkomaty.const import DOMAIN
from custom_components.inpost_paczkomaty.models import HttpResponse

PREFIX = "+48"
PHONE = "123456789"  # national number: not unique without the prefix
ACCOUNT_ID = f"{PREFIX}{PHONE}"
# How the account ID appears in entity IDs ("+" is dropped by slugify)
ACCOUNT_SLUG = ACCOUNT_ID.lstrip("+")
LOCKER = "GDA117M"
PARCELS_PATH = "/v4/parcels/tracked"
PROFILE_PATH = "/izi/app/shopping/v2/profile"
TOKEN_PATH = "/global/oauth2/token"
LOCKERS_URL = "https://inpost.pl/sites/default/files/points.json"


def make_jwt(exp_offset: int = 7200, marker: str = "tok") -> str:
    """Create an unsigned JWT expiring ``exp_offset`` seconds from now."""

    def encode(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    payload = {"sub": marker, "exp": int(time.time()) + exp_offset}
    return f"{encode({'alg': 'none'})}.{encode(payload)}.sig-{marker}"


def make_parcel(
    number: int = 1,
    status: str = "READY_TO_PICKUP",
    locker: str | None = LOCKER,
    **extra: Any,
) -> dict:
    """Create a tracked parcel record as returned by the InPost API."""
    parcel: dict[str, Any] = {
        "shipmentNumber": f"6950800865801800277{number:05d}",
        "shipmentType": "parcel",
        "openCode": f"68{number:04d}",
        "qrCode": f"P|+48{PHONE}|68{number:04d}",
        "status": status,
        "receiver": {"phoneNumber": {"prefix": "+48", "value": PHONE}},
        "sender": {"name": "Some Sender Sp. z o.o."},
        "ownershipStatus": "OWN",
    }
    if locker:
        parcel["pickUpPoint"] = {
            "name": locker,
            "location": {"latitude": 54.3188, "longitude": 18.58508},
            "locationDescription": "obiekt mieszkalny",
            "addressDetails": {
                "postCode": "80-180",
                "city": "Gdańsk",
                "street": "Wieżycka",
                "buildingNumber": "8",
            },
            "type": ["parcel_locker"],
        }
    parcel.update(extra)
    return parcel


def make_profile(
    phone: str | None = PHONE,
    favorites: tuple[str, ...] = (LOCKER,),
    prefix: str | None = PREFIX,
):
    """Create a profile response."""
    return {
        "personal": {"phoneNumber": phone, "phoneNumberPrefix": prefix},
        "delivery": {
            "points": {
                "items": [
                    {
                        "name": code,
                        "type": "PL",
                        "addressLines": ["Wieżycka 8"],
                        "active": True,
                        "preferred": True,
                    }
                    for code in favorites
                ]
            }
        },
    }


def make_locker(code: str, lat: float = 54.3188, lon: float = 18.58508) -> dict:
    """Create an entry of the public parcel lockers list."""
    return {
        "n": code,
        "t": 1,
        "d": f"opis {code}",
        "m": "",
        "q": "",
        "f": "004",
        "c": "Gdańsk",
        "g": "gdansk",
        "e": "Wieżycka",
        "r": "pomorskie",
        "o": "80-180",
        "b": "8",
        "h": "24/7",
        "i": "[]",
        "l": {"a": lat, "o": lon},
        "p": 0,
        "s": 1,
    }


def make_lockers_body(*codes: str) -> dict:
    """Create the public parcel lockers list response."""
    return {
        "date": "2026-10-08 03:13:11",
        "page": 1,
        "total_pages": 1,
        "items": [make_locker(code) for code in codes or (LOCKER, "GDA145M", "WAW01M")],
    }


def make_entry(
    phone: str = PHONE,
    access_token: str | None = None,
    refresh_token: str = "refresh-original",
    lockers: tuple[str, ...] = (LOCKER,),
    prefix: str = PREFIX,
    legacy_unique_id: str | None | bool = False,
) -> MockConfigEntry:
    """Create a config entry the way the config flow stores it.

    Pass ``legacy_unique_id`` (None or the national number) for an entry
    created before 0.5.0: no account ID, the national number kept in the data.
    """
    legacy = legacy_unique_id is not False
    data = {
        "access_token": access_token or make_jwt(),
        "refresh_token": refresh_token,
        "token_expires_in": 7199,
        "token_type": "Bearer",
    }
    if legacy:
        data["phone_number"] = phone
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"InPost: +48 {phone}" if legacy else f"InPost: {prefix}{phone}",
        unique_id=legacy_unique_id if legacy else f"{prefix}{phone}",
        data=data,
        options={
            "lockers": [
                {
                    "code": code,
                    "description": f"opis {code}",
                    "city": "Gdańsk",
                    "street": "Wieżycka",
                    "building": "8",
                    "zip_code": "80-180",
                }
                for code in lockers
            ]
        },
    )


class FakeInPost:
    """Programmable stand-in for the InPost HTTP endpoints.

    Installed in place of ``HttpClient._request`` so that everything above the
    transport (API client, coordinator, flows, entities) runs for real.
    """

    def __init__(self) -> None:
        self.parcels: list = [make_parcel(1)]
        self.parcels_extra: dict = {}
        # Each item: HttpResponse or Exception; consumed before normal handling
        self.parcels_queue: list = []
        self.token_queue: list = []
        self.token_body: dict | None = None
        self.profile: Any = make_profile()
        self.profile_queue: list = []
        self.lockers: Any = make_lockers_body()
        self.lockers_queue: list = []
        self.calls: list[dict] = []

    def count(self, fragment: str) -> int:
        """Return the number of requests whose URL contains ``fragment``."""
        return sum(1 for call in self.calls if fragment in call["url"])

    def last(self, fragment: str) -> dict:
        """Return the most recent request whose URL contains ``fragment``."""
        return [call for call in self.calls if fragment in call["url"]][-1]

    @staticmethod
    def _pop(queue: list):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def request(self, client, method: str, url: str, **kwargs) -> HttpResponse:
        """Handle a request issued through HttpClient."""
        headers = {**client.headers, **(kwargs.get("custom_headers") or {})}
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "data": kwargs.get("data"),
            }
        )
        if url.endswith(TOKEN_PATH):
            if self.token_queue:
                return self._pop(self.token_queue)
            body = self.token_body or {
                "access_token": make_jwt(marker="new"),
                "refresh_token": "refresh-rotated",
                "token_type": "Bearer",
                "expires_in": 7199,
            }
            return HttpResponse(body=body, status=200)
        if url.endswith(PARCELS_PATH):
            if self.parcels_queue:
                return self._pop(self.parcels_queue)
            return HttpResponse(
                body={"parcels": self.parcels, **self.parcels_extra}, status=200
            )
        if url.endswith(PROFILE_PATH):
            if self.profile_queue:
                return self._pop(self.profile_queue)
            return HttpResponse(body=self.profile, status=200)
        if self.lockers_queue:
            return self._pop(self.lockers_queue)
        return HttpResponse(body=self.lockers, status=200)
