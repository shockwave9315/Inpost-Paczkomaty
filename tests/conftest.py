"""Fixtures for testing."""

import logging
from unittest.mock import patch

import pytest

from custom_components.inpost_paczkomaty.http_client import HttpClient

from .common import FakeInPost

disable_loggers = ["sqlalchemy.engine.Engine"]


def pytest_configure():
    for logger_name in disable_loggers:
        logger = logging.getLogger(logger_name)
        logger.disabled = True


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    pass


@pytest.fixture(autouse=True)
def no_home_assistant_usage_reports(caplog):
    """Fail any test in which Home Assistant flags how the integration uses it.

    Home Assistant only logs a call to an API it is about to remove; without
    this no assertion would ever notice.
    """
    yield
    reports = [
        record.getMessage()
        for phase in ("setup", "call")
        for record in caplog.get_records(phase)
        if record.name == "homeassistant.helpers.frame"
        and "custom integration 'inpost_paczkomaty'" in record.getMessage()
    ]
    assert not reports, reports


@pytest.fixture
def fake_inpost():
    """Replace the HTTP transport with a programmable fake InPost backend."""
    fake = FakeInPost()

    async def _request(self, method, url, **kwargs):
        return await fake.request(self, method, url, **kwargs)

    with patch.object(HttpClient, "_request", _request):
        yield fake
