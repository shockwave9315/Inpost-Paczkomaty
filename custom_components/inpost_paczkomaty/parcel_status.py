"""What an InPost parcel status means to this integration.

This module is the only place that interprets a status. The names are the ones
InPost publishes in its status dictionary
(https://api-shipx-pl.easypack24.net/v1/statuses), upper-cased the way the
mobile API returns them; the two entries without a meaning of their own
(``OTHER``, ``MISSING``) are left out on purpose and therefore unknown.

Every known status belongs to exactly one state:

* ``READY``    - waiting for the recipient, who can collect it now
* ``EN_ROUTE`` - with InPost and not finished: on its way, delayed, stored,
  being re-routed or awaiting another delivery attempt
* ``TERMINAL`` - finished: delivered, returned, refused or cancelled

A status missing from the table is ``UNKNOWN``. Such a parcel is never dropped
silently - it is counted and reported (see ``InPostApiClient``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ApiParcel


class ParcelState(StrEnum):
    """State of a parcel as the sensors see it."""

    READY = "ready"
    EN_ROUTE = "en_route"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ParcelClassification:
    """Account state and whether the reported point can receive that state."""

    state: ParcelState
    assign_to_locker: bool


# Collection is possible, but no captured payload establishes whether
# pickUpPoint identifies the temporary location or the original destination.
_UNASSIGNED_READY_STATUSES = frozenset(
    {"STACK_IN_BOX_MACHINE", "STACK_IN_CUSTOMER_SERVICE_POINT"}
)


# The one terminal status the carbon footprint statistics are built from
STATUS_DELIVERED = "DELIVERED"

# status -> description shown to the user (Polish, like the InPost app)
_STATUSES: dict[ParcelState, dict[str, str]] = {
    ParcelState.READY: {
        "READY_TO_PICKUP": "Gotowa do odbioru",
        "PICKUP_REMINDER_SENT": "Przypomnienie o odbiorze",
        "READY_TO_PICKUP_FROM_POK": "Czeka na odbiór w PaczkoPunkcie",
        "READY_TO_PICKUP_FROM_POK_REGISTERED": "Czeka na odbiór w PaczkoPunkcie",
        "COURIER_AVIZO_IN_CUSTOMER_SERVICE_POINT": "Oczekuje na odbiór",
        "STACK_IN_BOX_MACHINE": "Paczka magazynowana w tymczasowym automacie Paczkomat",
        "STACK_IN_CUSTOMER_SERVICE_POINT": "Paczka magazynowana w PaczkoPunkcie",
    },
    ParcelState.EN_ROUTE: {
        # Not handed over to InPost yet
        "CREATED": "Przesyłka utworzona",
        "OFFERS_PREPARED": "Przygotowano oferty",
        "OFFER_SELECTED": "Oferta wybrana",
        "CONFIRMED": "Przesyłka utworzona",
        # On its way
        "DISPATCHED_BY_SENDER": "Nadana",
        "DISPATCHED_BY_SENDER_TO_POK": "Nadana w PaczkoPunkcie",
        "COLLECTED_FROM_SENDER": "Odebrana od klienta",
        "TAKEN_BY_COURIER": "Odebrana przez Kuriera",
        "TAKEN_BY_COURIER_FROM_POK": "W drodze do oddziału nadawczego InPost",
        "ADOPTED_AT_SOURCE_BRANCH": "Przyjęta w Centrum Logistycznym",
        "SENT_FROM_SOURCE_BRANCH": "W trasie",
        "ADOPTED_AT_SORTING_CENTER": "Przyjęta w Sortowni",
        "SENT_FROM_SORTING_CENTER": "Wysłana z Sortowni",
        "ADOPTED_AT_TARGET_BRANCH": "Przyjęta w Oddziale Docelowym",
        "OUT_FOR_DELIVERY": "Wydana do doręczenia",
        "OUT_FOR_DELIVERY_TO_ADDRESS": "W doręczeniu",
        # Delayed, re-routed or awaiting another attempt
        "PICKUP_REMINDER_SENT_ADDRESS": "W doręczeniu",
        "DELAY_IN_DELIVERY": "Możliwe opóźnienie doręczenia",
        "REDIRECT_TO_BOX": "Przekierowano do automatu Paczkomat",
        "CANCELED_REDIRECT_TO_BOX": "Anulowano przekierowanie",
        "READDRESSED": "Przekierowano na inny adres",
        "OVERSIZED": "Przesyłka ponadgabarytowa",
        "UNDELIVERED_WRONG_ADDRESS": "Brak możliwości doręczenia",
        "UNDELIVERED_INCOMPLETE_ADDRESS": "Brak możliwości doręczenia",
        "UNDELIVERED_UNKNOWN_RECEIVER": "Brak możliwości doręczenia",
        "UNDELIVERED_COD_CASH_RECEIVER": "Brak możliwości doręczenia",
        "UNDELIVERED_NO_MAILBOX": "Brak możliwości doręczenia",
        "UNDELIVERED_NOT_LIVE_ADDRESS": "Brak możliwości doręczenia",
        "AVIZO": "Powrót do oddziału",
        # Not collected in time: still with InPost, the outcome is open
        "PICKUP_TIME_EXPIRED": "Upłynął termin odbioru",
        "READY_TO_PICKUP_FROM_BRANCH": "Paczka nieodebrana – czeka w Oddziale",
        # Temporary storage expired, or returning to the chosen parcel locker
        "STACK_PARCEL_IN_BOX_MACHINE_PICKUP_TIME_EXPIRED": (
            "Upłynął termin odbioru paczki magazynowanej"
        ),
        "UNSTACK_FROM_BOX_MACHINE": (
            "Paczka w drodze do pierwotnie wybranego automatu Paczkomat"
        ),
        "STACK_PARCEL_PICKUP_TIME_EXPIRED": (
            "Upłynął termin odbioru paczki magazynowanej"
        ),
        "UNSTACK_FROM_CUSTOMER_SERVICE_POINT": (
            "W drodze do wybranego automatu Paczkomat"
        ),
    },
    ParcelState.TERMINAL: {
        STATUS_DELIVERED: "Doręczona",
        "RETURN_PICKUP_CONFIRMATION_TO_SENDER": "Przygotowano dokumenty zwrotne",
        "CLAIMED": "Zareklamowana w automacie Paczkomat",
        "REJECTED_BY_RECEIVER": "Odmowa przyjęcia",
        "RETURNED_TO_SENDER": "Zwrot do nadawcy",
        "TAKEN_BY_COURIER_FROM_CUSTOMER_SERVICE_POINT": "Zwrócona do nadawcy",
        "UNDELIVERED": "Przekazanie do magazynu przesyłek niedoręczalnych",
        "UNDELIVERED_LACK_OF_ACCESS_LETTERBOX": "Brak możliwości doręczenia",
        "CANCELED": "Anulowano etykietę",
    },
}

_STATE_BY_STATUS: dict[str, ParcelState] = {
    status: state for state, statuses in _STATUSES.items() for status in statuses
}
_DESCRIPTION_BY_STATUS: dict[str, str] = {
    status: description
    for statuses in _STATUSES.values()
    for status, description in statuses.items()
}


def classify_parcel(parcel: ApiParcel) -> ParcelClassification:
    """Return account state and whether it can be assigned to a locker.

    The whole parcel is passed, not only its status, so that other signals the
    API sends can be taken into account here without touching any caller. The
    obvious candidate is ``status_group``; it is not used yet because no
    payload available to this project shows which values it takes.
    """
    return ParcelClassification(
        state=_STATE_BY_STATUS.get(parcel.status, ParcelState.UNKNOWN),
        assign_to_locker=parcel.status not in _UNASSIGNED_READY_STATUSES,
    )


def describe_status(status: str) -> str:
    """Return the description of a status, or the status itself if unknown."""
    return _DESCRIPTION_BY_STATUS.get(status, status)


def statuses_in(state: ParcelState) -> frozenset[str]:
    """Return every status that belongs to a state."""
    return frozenset(_STATUSES.get(state, ()))
