"""Identity of an InPost account.

An InPost account is the phone number it is registered with, and a phone
number is only unique together with its country prefix: the same national
digits exist under several prefixes (``+48 501 234 567`` and
``+380 501 234 567`` are two different accounts).

The account ID is therefore the full number in E.164 form, for example
``+48123456789``. It is

* built in exactly one place - :func:`build_account_id`, from the profile
  InPost returns for the tokens of a login,
* stored in exactly one place - the ``unique_id`` of the config entry.

Everything that needs to know which account an entry belongs to (duplicate
detection, re-authentication, the entry title, device names) reads
``entry.unique_id``. The entry data holds credentials only.
"""

from __future__ import annotations

import re


def build_account_id(prefix: str | None, number: str | None) -> str | None:
    """Build the account ID from the phone number parts of a profile.

    Only the digits matter, so the ID stays the same if InPost changes how it
    writes a number (``+48``, ``48``, ``0048``, spaces).

    Returns:
        The account ID, or None if either part is missing: a number without
        its country prefix does not identify an account.
    """
    prefix_digits = re.sub(r"[^0-9]", "", prefix or "").lstrip("0")
    number_digits = re.sub(r"[^0-9]", "", number or "")
    if not prefix_digits or not number_digits:
        return None
    return f"+{prefix_digits}{number_digits}"


def is_account_id(unique_id: str | None) -> bool:
    """Return True if a config entry's unique ID is an account ID.

    Entries created before 0.5.0 have no unique ID, or the national number
    alone; neither says which account the entry belongs to.
    """
    return bool(unique_id) and unique_id.startswith("+")
