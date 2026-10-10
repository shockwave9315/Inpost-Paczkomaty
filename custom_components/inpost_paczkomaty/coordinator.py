"""InPost API data coordinator."""

import logging
from datetime import timedelta
from typing import Optional

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import InPostApiClient
from .const import (
    DEFAULT_UPDATE_INTERVAL,
    MAX_BACKOFF_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
)
from .exceptions import ApiAuthError, ApiClientError, RateLimitedError
from .models import ParcelsSummary

_LOGGER = logging.getLogger(__name__)


class InpostDataCoordinator(DataUpdateCoordinator[ParcelsSummary]):
    """Data coordinator for InPost parcels API."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: InPostApiClient,
        update_interval_seconds: int = DEFAULT_UPDATE_INTERVAL,
        config_entry: Optional[ConfigEntry] = None,
    ) -> None:
        """Initialize the data coordinator.

        Args:
            hass: Home Assistant instance.
            api_client: InPost API client instance.
            update_interval_seconds: Data refresh interval in seconds.
            config_entry: Config entry this coordinator belongs to.
        """
        super().__init__(
            hass,
            _LOGGER,
            name="InPost Paczkomaty data coordinator",
            config_entry=config_entry,
            update_interval=timedelta(seconds=update_interval_seconds),
        )
        self.api_client = api_client
        self._base_interval = timedelta(seconds=update_interval_seconds)
        self._consecutive_failures = 0
        self._reauth_requested = False

    @callback
    def _async_dismiss_reauth(self) -> None:
        """Withdraw a reauth request once the stored credentials work again.

        A rejection can be followed by a successful update (for example when a
        later token refresh goes through); the pending request would then ask
        the user to sign in although nothing is wrong.
        """
        if self.config_entry is None:
            return
        for flow in self.config_entry.async_get_active_flows(
            self.hass, {SOURCE_REAUTH}
        ):
            self.hass.config_entries.flow.async_abort(flow["flow_id"])

    def _apply_backoff(self, retry_after: Optional[float] = None) -> None:
        """Slow polling down after a failed update.

        The interval doubles with every consecutive failure, capped at
        MAX_BACKOFF_SECONDS, and never drops below a server-provided
        Retry-After. The configured interval is restored on the next success.
        """
        self._consecutive_failures += 1
        base = self._base_interval.total_seconds()
        delay = base * 2 ** min(self._consecutive_failures, 16)
        delay = max(base, min(delay, MAX_BACKOFF_SECONDS))
        if retry_after is not None:
            delay = max(delay, min(retry_after, MAX_RETRY_AFTER_SECONDS))
        self.update_interval = timedelta(seconds=delay)

    async def _async_update_data(self) -> ParcelsSummary:
        """Fetch parcels data from InPost API.

        Returns:
            ParcelsSummary with current parcels data.

        Raises:
            ConfigEntryAuthFailed: If InPost no longer accepts the credentials.
            UpdateFailed: If API request fails.
        """
        try:
            data = await self.api_client.get_parcels()
        except ApiAuthError as err:
            self._apply_backoff()
            self._reauth_requested = True
            raise ConfigEntryAuthFailed(
                f"InPost rejected the stored credentials: {err}"
            ) from err
        except RateLimitedError as err:
            self._apply_backoff(err.retry_after)
            raise UpdateFailed(
                f"InPost API rate limit reached, next attempt in "
                f"{int(self.update_interval.total_seconds())} s"
            ) from err
        except ApiClientError as err:
            self._apply_backoff()
            raise UpdateFailed(f"Error fetching InPost parcels: {err}") from err

        self._consecutive_failures = 0
        self.update_interval = self._base_interval
        if self._reauth_requested:
            self._reauth_requested = False
            self._async_dismiss_reauth()
        return data
