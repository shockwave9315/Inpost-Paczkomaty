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


@pytest.fixture
def fake_inpost():
    """Replace the HTTP transport with a programmable fake InPost backend."""
    fake = FakeInPost()

    async def _request(self, method, url, **kwargs):
        return await fake.request(self, method, url, **kwargs)

    with patch.object(HttpClient, "_request", _request):
        yield fake
