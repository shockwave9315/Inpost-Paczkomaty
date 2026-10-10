"""Functions to connect to InPost APIs."""

import asyncio
import logging
from datetime import timezone
from typing import Any, Callable, Dict, List, Optional

import aiohttp
from dacite import DaciteError, from_dict
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.inpost_paczkomaty.const import (
    API_BASE_URL,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_IGNORED_EN_ROUTE_STATUSES,
    DEFAULT_PARCEL_LOCKERS_URL,
    DEFAULT_SHOW_ONLY_OWN_PARCELS,
    OAUTH_CLIENT_ID,
    API_USER_AGENT,
)
from custom_components.inpost_paczkomaty.exceptions import (
    ApiAuthError,
    ApiClientError,
    ApiResponseError,
    InPostApiError,
    RateLimitedError,
)
from custom_components.inpost_paczkomaty.http_client import HttpClient
from custom_components.inpost_paczkomaty.models import (
    ApiParcel,
    AuthTokens,
    CarbonFootprintStats,
    DailyCarbonFootprint,
    HttpResponse,
    InPostParcelLocker,
    InPostParcelLockerPointCoordinates,
    Locker,
    ParcelListItem,
    ParcelsSummary,
    UserProfile,
)
from custom_components.inpost_paczkomaty.parcel_status import (
    STATUS_DELIVERED,
    ParcelState,
    classify_parcel,
)
from custom_components.inpost_paczkomaty.utils import (
    convert_keys_to_snake_case,
    drop_nulls,
    get_header,
    get_language_code,
    is_token_expiring_soon,
    parse_retry_after,
)

_LOGGER = logging.getLogger(__name__)

# Parcel fields that are strings in the API contract but safe to coerce
_STRING_PARCEL_FIELDS = ("shipment_number", "open_code", "qr_code")
# Distinct unknown parcel statuses reported in the log before giving up
_MAX_REPORTED_UNKNOWN_STATUSES = 20


def _parse_parcel_lockers(body: Any) -> list[InPostParcelLocker]:
    """Convert the public points.json payload into locker objects.

    Runs in an executor: the payload holds tens of thousands of entries.

    Raises:
        ApiResponseError: If the payload does not have the expected shape or
            none of its entries can be read.
    """
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise ApiResponseError("Unexpected parcel lockers list format")

    lockers: list[InPostParcelLocker] = []
    skipped = 0
    for item in body["items"]:
        try:
            location = item["l"]
            lockers.append(
                InPostParcelLocker(
                    n=str(item["n"]),
                    t=item.get("t", 0),
                    d=item.get("d") or "",
                    m=item.get("m") or "",
                    q=item.get("q", ""),
                    f=item.get("f") or "",
                    c=item.get("c") or "",
                    g=item.get("g") or "",
                    e=item.get("e") or "",
                    r=item.get("r") or "",
                    o=item.get("o") or "",
                    b=item.get("b") or "",
                    h=item.get("h") or "",
                    i=item.get("i") or "",
                    l=InPostParcelLockerPointCoordinates(
                        a=float(location["a"]), o=float(location["o"])
                    ),
                    p=item.get("p", 0),
                    s=item.get("s", 0),
                )
            )
        except AttributeError, KeyError, TypeError, ValueError:
            skipped += 1

    if skipped:
        _LOGGER.warning("Skipped %d unreadable entries in parcel lockers list", skipped)
    if body["items"] and not lockers:
        raise ApiResponseError("No readable entries in parcel lockers list")
    return lockers


class InPostApiClient:
    """Client for InPost APIs.

    Supports both authenticated endpoints (parcels, profile) and
    public endpoints (parcel lockers list).
    """

    PARCELS_ENDPOINT = "/v4/parcels/tracked"
    PROFILE_ENDPOINT = "/izi/app/shopping/v2/profile"
    TOKEN_ENDPOINT = "/global/oauth2/token"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Optional[ConfigEntry] = None,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        on_token_refresh: Optional[Callable[[AuthTokens], None]] = None,
        ignored_en_route_statuses: Optional[List[str]] = None,
        http_timeout: int = DEFAULT_HTTP_TIMEOUT,
        parcel_lockers_url: str = DEFAULT_PARCEL_LOCKERS_URL,
        show_only_own_parcels: bool = DEFAULT_SHOW_ONLY_OWN_PARCELS,
    ) -> None:
        """Initialize the InPost API client.

        Args:
            hass: Home Assistant instance.
            entry: Optional config entry containing authentication data.
            access_token: Optional access token override.
            refresh_token: Optional refresh token override.
            on_token_refresh: Optional callback when token is refreshed.
            ignored_en_route_statuses: List of en_route statuses to ignore.
            http_timeout: HTTP request timeout in seconds.
            parcel_lockers_url: URL for fetching parcel lockers list.
            show_only_own_parcels: If True, only show parcels with OWN ownership.
        """
        self._parcel_lockers_url = parcel_lockers_url
        self._show_only_own_parcels = show_only_own_parcels
        self.hass = hass
        data = entry.data if entry and entry.data else {}
        self._access_token = access_token or data.get(CONF_ACCESS_TOKEN)
        self._refresh_token = refresh_token or data.get(CONF_REFRESH_TOKEN)
        self._on_token_refresh = on_token_refresh
        self._refresh_lock = asyncio.Lock()
        # Identifiers of problems already reported, to log each of them once
        self._reported_invalid_parcels: set[str] = set()
        self._reported_unknown_statuses: set[str] = set()
        self._reported_has_more = False
        self._ignored_en_route_statuses = frozenset(
            ignored_en_route_statuses
            if ignored_en_route_statuses is not None
            else DEFAULT_IGNORED_EN_ROUTE_STATUSES
        )

        # Authenticated client for InPost mobile API
        self._http_client = HttpClient(
            auth_type="Bearer" if self._access_token else None,
            auth_value=self._access_token,
            custom_headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Accept-Language": get_language_code(hass.config.language),
            },
            default_timeout=http_timeout,
        )

        # Unauthenticated client for public endpoints
        self._public_http_client = HttpClient(
            custom_headers={
                "Accept": "application/json",
            },
            default_timeout=http_timeout,
        )

    @staticmethod
    async def _send(request: Callable[..., Any], **kwargs: Any) -> HttpResponse:
        """Run an HTTP request and normalize transport failures.

        Raises:
            ApiClientError: If the request times out or the connection fails.
        """
        try:
            return await request(**kwargs)
        except (InPostApiError, aiohttp.ClientError, TimeoutError, OSError) as err:
            raise ApiClientError(
                f"Error communicating with InPost API! ({type(err).__name__}: {err})"
            ) from err

    @staticmethod
    def _raise_for_status(response: HttpResponse, message: str) -> None:
        """Translate an HTTP error status into the matching exception.

        Raises:
            ApiAuthError: On HTTP 401.
            RateLimitedError: On HTTP 429 (carries the Retry-After delay).
            ApiClientError: On any other error status.
        """
        if not response.is_error:
            return
        text = f"{message} Status: {response.status}"
        if response.status == 401:
            raise ApiAuthError(text)
        if response.status == 429:
            raise RateLimitedError(
                text,
                retry_after=parse_retry_after(
                    get_header(response.headers, "Retry-After")
                ),
            )
        raise ApiClientError(text)

    async def _ensure_valid_token(self) -> None:
        """Ensure the access token is valid, refreshing if needed.

        Checks if the current access token is about to expire and
        refreshes it using the refresh token if necessary.

        Raises:
            ApiClientError: If token refresh fails.
        """
        if not self._access_token and not self._refresh_token:
            # Unauthenticated client (public endpoints only)
            return

        if self._access_token and not is_token_expiring_soon(self._access_token):
            return

        if not self._refresh_token:
            _LOGGER.warning("Access token is expiring but no refresh token available")
            return

        _LOGGER.debug("Access token is missing or expiring soon, refreshing")
        await self.refresh_access_token()

    async def refresh_access_token(self) -> AuthTokens:
        """Refresh the access token using the refresh token.

        Returns:
            AuthTokens with the new access token and the refresh token to use
            from now on (the previous one if the server did not rotate it).

        Raises:
            ApiAuthError: If InPost rejects the refresh token.
            ApiClientError: If token refresh fails for another reason.
        """
        if not self._refresh_token:
            raise ApiAuthError("No refresh token available")

        async with self._refresh_lock:
            response = await self._send(
                self._public_http_client.post,
                url=f"{API_BASE_URL}{self.TOKEN_ENDPOINT}",
                data={
                    "client_id": OAUTH_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
                custom_headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": API_USER_AGENT,
                },
            )

            if response.is_error:
                text = f"Error refreshing access token! Status: {response.status}"
                # RFC 6749 5.2: a rejected grant is reported as 400/401.
                if response.status in (400, 401):
                    raise ApiAuthError(text)
                self._raise_for_status(response, "Error refreshing access token!")

            body = response.body
            if not isinstance(body, dict) or not body.get("access_token"):
                raise ApiResponseError(
                    "Token refresh response did not contain an access token"
                )

            tokens = AuthTokens(
                access_token=body["access_token"],
                # Keep the current refresh token when the server does not rotate it
                refresh_token=body.get("refresh_token") or self._refresh_token,
                token_type=body.get("token_type", "Bearer"),
                expires_in=body.get("expires_in", 7199),
                scope=body.get("scope", "openid"),
                id_token=body.get("id_token"),
            )

            # Update internal state
            self._access_token = tokens.access_token
            self._refresh_token = tokens.refresh_token

            # Update HTTP client authorization header
            self._http_client.update_headers(
                {"Authorization": f"Bearer {tokens.access_token}"}
            )

            # Notify callback if set
            if self._on_token_refresh:
                self._on_token_refresh(tokens)

            _LOGGER.debug("Access token refreshed successfully")
            return tokens

    async def _authorized_get(
        self, url: str, custom_headers: Optional[dict] = None
    ) -> HttpResponse:
        """Send an authenticated GET, renewing the token once on HTTP 401.

        Raises:
            ApiAuthError: If the refresh token is rejected.
            ApiClientError: If the request cannot be sent.
        """
        kwargs: dict[str, Any] = {"url": url}
        if custom_headers:
            kwargs["custom_headers"] = custom_headers

        await self._ensure_valid_token()
        response = await self._send(self._http_client.get, **kwargs)

        if response.status == 401 and self._refresh_token:
            # The access token was revoked before its expiry; try one renewal.
            _LOGGER.debug("Access token rejected, refreshing and retrying once")
            await self.refresh_access_token()
            response = await self._send(self._http_client.get, **kwargs)

        return response

    def _parse_parcel(self, item: Any) -> ApiParcel:
        """Parse a single parcel record from the tracked parcels response.

        Raises:
            DaciteError, TypeError: If the record does not match the model.
        """
        if not isinstance(item, dict):
            raise TypeError("parcel record is not an object")

        data = drop_nulls(convert_keys_to_snake_case(item))
        for key in _STRING_PARCEL_FIELDS:
            value = data.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                data[key] = str(value)

        return from_dict(ApiParcel, data)

    def _report_invalid_parcel(self, item: Any, err: Exception) -> None:
        """Log a parcel record that had to be skipped (once per parcel).

        Only the shipment number tail and the offending field are logged;
        values are left out because they may hold pickup codes.
        """
        number = item.get("shipmentNumber") if isinstance(item, dict) else None
        ident = f"...{str(number)[-4:]}" if number else "<unknown>"
        field_path = getattr(err, "field_path", None)
        key = f"{ident}:{type(err).__name__}:{field_path}"
        if key in self._reported_invalid_parcels:
            return
        self._reported_invalid_parcels.add(key)
        _LOGGER.warning(
            "Skipping parcel %s: InPost API returned data in an unexpected "
            "format (%s, field: %s). Other parcels are not affected",
            ident,
            type(err).__name__,
            field_path or "n/a",
        )

    async def get_parcels(self) -> ParcelsSummary:
        """Get tracked parcels and convert to ParcelsSummary.

        Parcels are parsed one by one, so a single malformed record is
        skipped (and counted in ``invalid_parcels_count``) instead of failing
        the whole update.

        Returns:
            ParcelsSummary with parcels grouped by status.

        Raises:
            ApiAuthError: If the credentials are no longer accepted.
            RateLimitedError: If the API asks us to slow down.
            ApiResponseError: If the response is not a parcels list at all or
                none of its records can be parsed.
            ApiClientError: If the API request fails.
        """
        response = await self._authorized_get(
            url=f"{API_BASE_URL}{self.PARCELS_ENDPOINT}"
        )
        self._raise_for_status(response, "Error communicating with InPost API!")

        body = response.body
        if not isinstance(body, dict):
            raise ApiResponseError(
                "InPost API returned an unexpected parcels response "
                f"({type(body).__name__} instead of a JSON object)"
            )
        raw_parcels = body.get("parcels")
        if raw_parcels is None:
            raw_parcels = []
        if not isinstance(raw_parcels, list):
            raise ApiResponseError("InPost API returned a malformed parcels list")

        parcels: List[ApiParcel] = []
        invalid_count = 0
        for item in raw_parcels:
            try:
                parcels.append(self._parse_parcel(item))
            except (DaciteError, TypeError, ValueError) as err:
                invalid_count += 1
                self._report_invalid_parcel(item, err)

        if raw_parcels and not parcels:
            # Nothing is readable: the API contract changed, not one record.
            raise ApiResponseError(
                f"None of the {len(raw_parcels)} parcels returned by InPost API "
                "could be parsed"
            )

        has_more = body.get("more") is True
        if has_more and not self._reported_has_more:
            self._reported_has_more = True
            _LOGGER.warning(
                "InPost API reports more tracked parcels than it returned "
                "(%d); the remaining ones are not shown",
                len(raw_parcels),
            )

        summary = self._build_parcels_summary(
            parcels, invalid_count=invalid_count, has_more=has_more
        )
        self._report_unknown_statuses(summary.unknown_statuses)
        return summary

    def _report_unknown_statuses(self, statuses: List[str]) -> None:
        """Log parcel statuses the integration cannot classify (once each)."""
        for status in statuses:
            if (
                status in self._reported_unknown_statuses
                or len(self._reported_unknown_statuses)
                >= _MAX_REPORTED_UNKNOWN_STATUSES
            ):
                continue
            self._reported_unknown_statuses.add(status)
            _LOGGER.warning(
                "InPost API returned a parcel in a status this integration does "
                "not know: %.80s. Such parcels are part of the all parcels "
                "count and of unknown_parcels_count, but are neither ready for "
                "pickup nor en route. Please report the status in an issue",
                status,
            )

    async def get_profile(self) -> UserProfile:
        """Get user profile with favorite lockers.

        Returns:
            UserProfile with delivery points and personal info.

        Raises:
            ApiClientError: If API request fails.
        """
        response = await self._authorized_get(
            url=f"{API_BASE_URL}{self.PROFILE_ENDPOINT}",
            custom_headers={
                # without this InPost API returns 500 Internal Server Error
                "User-Agent": API_USER_AGENT,
            },
        )
        self._raise_for_status(response, "Error fetching profile from InPost API!")

        if not isinstance(response.body, dict):
            raise ApiResponseError("InPost API returned an unexpected profile response")

        try:
            return from_dict(
                UserProfile, drop_nulls(convert_keys_to_snake_case(response.body))
            )
        except DaciteError as err:
            raise ApiResponseError(
                "InPost API returned a profile in an unexpected format "
                f"({type(err).__name__})"
            ) from err

    async def get_parcel_lockers_list(self) -> list[InPostParcelLocker]:
        """Get parcel lockers list from public InPost endpoint.

        This method doesn't require authentication.

        Returns:
            List of parcel locker details.

        Raises:
            ApiClientError: If API request fails.
        """
        response = await self._send(
            self._public_http_client.get, url=self._parcel_lockers_url
        )
        self._raise_for_status(response, "Error fetching parcel lockers!")

        # ~8 MB / 30k+ entries: keep the conversion off the event loop
        return await self.hass.async_add_executor_job(
            _parse_parcel_lockers, response.body
        )

    def _build_parcels_summary(
        self,
        parcels: List[ApiParcel],
        invalid_count: int = 0,
        has_more: bool = False,
    ) -> ParcelsSummary:
        """Build ParcelsSummary from list of parcels.

        Args:
            parcels: List of API parcels.
            invalid_count: Number of records skipped because of bad data.
            has_more: Whether the API signalled further, unfetched parcels.

        Returns:
            ParcelsSummary with parcels grouped by their state. What a status
            means is decided by ``classify_parcel`` alone.
        """
        ready_for_pickup: Dict[str, Locker] = {}
        en_route: Dict[str, Locker] = {}

        all_count = 0
        unknown_statuses: List[str] = []

        # Lists for dashboard display
        ready_for_pickup_list: List[ParcelListItem] = []
        en_route_list: List[ParcelListItem] = []

        def add(
            group: Dict[str, Locker],
            listed: List[ParcelListItem],
            parcel: ApiParcel,
            *,
            assign_to_locker: bool,
        ) -> None:
            """Always list the parcel; group it only when its point is usable."""
            listed.append(
                parcel.to_parcel_list_item(pickup_point_unverified=not assign_to_locker)
            )
            if not assign_to_locker:
                return
            locker_id = parcel.locker_id or "COURIER"
            locker = group.setdefault(
                locker_id, Locker(locker_id=locker_id, count=0, parcels=[])
            )
            locker.parcels.append(parcel.to_parcel_item())
            locker.count += 1

        # Carbon footprint tracking
        daily_co2: Dict[str, Dict[str, float]] = {}  # {date: {co2, count}}
        total_co2 = 0.0
        total_delivered_parcels = 0

        for parcel in parcels:
            # Skip shared parcels if show_only_own_parcels is enabled
            if self._show_only_own_parcels and parcel.ownership_status != "OWN":
                continue

            all_count += 1
            classification = classify_parcel(parcel)
            state = classification.state

            if state is ParcelState.READY:
                add(
                    ready_for_pickup,
                    ready_for_pickup_list,
                    parcel,
                    assign_to_locker=classification.assign_to_locker,
                )
            elif state is ParcelState.EN_ROUTE:
                # The user may leave some en route statuses out of the counts
                if parcel.status not in self._ignored_en_route_statuses:
                    add(
                        en_route,
                        en_route_list,
                        parcel,
                        assign_to_locker=classification.assign_to_locker,
                    )
            elif state is ParcelState.UNKNOWN:
                unknown_statuses.append(parcel.status)

            # Calculate carbon footprint for DELIVERED parcels
            if parcel.status == STATUS_DELIVERED:
                co2_value = parcel.effective_carbon_footprint
                pickup_date = parcel.pick_up_date_parsed

                if co2_value is not None and pickup_date is not None:
                    # Bucket by the day in Home Assistant's time zone, the same
                    # clock the "today" sensor uses (API timestamps are UTC).
                    if pickup_date.tzinfo is None:
                        pickup_date = pickup_date.replace(tzinfo=timezone.utc)
                    date_str = dt_util.as_local(pickup_date).strftime("%Y-%m-%d")
                    if date_str not in daily_co2:
                        daily_co2[date_str] = {"co2": 0.0, "count": 0}
                    daily_co2[date_str]["co2"] += co2_value
                    daily_co2[date_str]["count"] += 1
                    total_co2 += co2_value
                    total_delivered_parcels += 1

        # Build carbon footprint stats
        daily_data = [
            DailyCarbonFootprint(
                date=date_str,
                value=data["co2"],
                parcel_count=int(data["count"]),
            )
            for date_str, data in sorted(daily_co2.items())
        ]

        carbon_stats = CarbonFootprintStats(
            total_co2_kg=round(total_co2, 4),
            total_parcels=total_delivered_parcels,
            daily_data=daily_data,
        )

        return ParcelsSummary(
            all_count=all_count,
            ready_for_pickup_count=len(ready_for_pickup_list),
            en_route_count=len(en_route_list),
            ready_for_pickup=ready_for_pickup,
            en_route=en_route,
            carbon_footprint_stats=carbon_stats,
            ready_for_pickup_list=ready_for_pickup_list,
            en_route_list=en_route_list,
            invalid_parcels_count=invalid_count,
            unknown_count=len(unknown_statuses),
            unknown_statuses=sorted(set(unknown_statuses)),
            has_more=has_more,
        )

    async def close(self) -> None:
        """Close all HTTP client sessions."""
        await self._http_client.close()
        await self._public_http_client.close()
