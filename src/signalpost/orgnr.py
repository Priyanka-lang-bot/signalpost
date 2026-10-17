"""Norwegian organisation number (organisasjonsnummer) normalisation and validation.

A Norwegian organisation number has 9 digits. The last digit is a modulus-11
check digit computed over the first 8 digits with weights 3,2,7,6,5,4,3,2.
A remainder that would give check digit 10 means the number is invalid.
"""

from __future__ import annotations

import re

_WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)
# Formatting people commonly use: spaces, dots, dashes, NBSP, and an "NO" prefix
# or "MVA" suffix (VAT number form, e.g. "NO 923 609 016 MVA").
_STRIP = re.compile(r"[\s.\- ]")


class InvalidOrgNumber(ValueError):
    """Raised when input cannot be a valid Norwegian organisation number."""


def check_digit(first8: str) -> int | None:
    total = sum(int(d) * w for d, w in zip(first8, _WEIGHTS))
    rem = 11 - (total % 11)
    if rem == 11:
        return 0
    if rem == 10:
        return None  # no valid number can have this prefix
    return rem


def normalize(raw: str) -> str:
    """Return the canonical 9-digit string or raise InvalidOrgNumber."""
    if not isinstance(raw, str):
        raise InvalidOrgNumber("organisation number must be given as a string")
    s = raw.strip().upper()
    if s.startswith("NO"):
        s = s[2:]
    if s.endswith("MVA"):
        s = s[:-3]
    s = _STRIP.sub("", s)
    if not s:
        raise InvalidOrgNumber("organisation number is empty")
    if not s.isdigit():
        raise InvalidOrgNumber(f"organisation number may only contain digits (got {raw!r})")
    if len(s) != 9:
        raise InvalidOrgNumber(f"organisation number must have 9 digits, got {len(s)} ({raw!r})")
    if s[0] not in "89":
        # Brreg assigns organisation numbers starting with 8 or 9.
        raise InvalidOrgNumber(f"organisation numbers start with 8 or 9 (got {raw!r})")
    expected = check_digit(s[:8])
    if expected is None or expected != int(s[8]):
        raise InvalidOrgNumber(f"organisation number {s} fails the modulus-11 checksum")
    return s


def is_valid(raw: str) -> bool:
    try:
        normalize(raw)
        return True
    except InvalidOrgNumber:
        return False
