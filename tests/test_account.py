"""Unit tests for the account identity model."""

import pytest
from dacite import from_dict

from custom_components.inpost_paczkomaty.account import build_account_id, is_account_id
from custom_components.inpost_paczkomaty.models import UserProfile


@pytest.mark.parametrize(
    ("prefix", "number", "expected"),
    [
        ("+48", "123456789", "+48123456789"),
        ("+380", "123456789", "+380123456789"),
        # Formatting never changes the identity
        ("48", "123456789", "+48123456789"),
        ("0048", "123456789", "+48123456789"),
        (" +48 ", "123 456-789", "+48123456789"),
        # A national number may start with zero; that zero is part of it
        ("+39", "0612345678", "+390612345678"),
        # Without both parts there is no identity
        (None, "123456789", None),
        ("", "123456789", None),
        ("+", "123456789", None),
        ("+48", None, None),
        ("+48", "", None),
        ("+48", "n/a", None),
        (None, None, None),
    ],
)
def test_build_account_id(prefix, number, expected):
    assert build_account_id(prefix, number) == expected


def test_same_national_number_gives_different_accounts():
    assert build_account_id("+48", "501234567") != build_account_id("+380", "501234567")


@pytest.mark.parametrize(
    ("unique_id", "expected"),
    [
        ("+48123456789", True),
        ("123456789", False),  # written by versions before 0.5.0
        ("", False),
        (None, False),
    ],
)
def test_is_account_id(unique_id, expected):
    assert is_account_id(unique_id) is expected


def test_every_built_account_id_is_recognised():
    assert is_account_id(build_account_id("0048", "123456789"))


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (
            {"personal": {"phone_number": "123456789", "phone_number_prefix": "+48"}},
            "+48123456789",
        ),
        ({"personal": {"phone_number": "123456789"}}, None),
        ({"personal": {"phone_number_prefix": "+48"}}, None),
        ({"personal": {}}, None),
        ({}, None),
    ],
    ids=["complete", "no-prefix", "no-number", "empty-personal", "no-personal"],
)
def test_profile_account_id(profile, expected):
    assert from_dict(UserProfile, profile).account_id == expected
