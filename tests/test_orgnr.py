import pytest

from signalpost.orgnr import InvalidOrgNumber, is_valid, normalize


@pytest.mark.parametrize("raw", ["923609016", "923 609 016", "923.609.016", "NO 923 609 016 MVA", " 974760673 "])
def test_valid_numbers_normalise(raw):
    assert normalize(raw) in {"923609016", "974760673"}


@pytest.mark.parametrize("raw, reason", [
    ("", "empty"),
    ("12345678", "9 digits"),
    ("9236090160", "9 digits"),
    ("92360901A", "only contain digits"),
    ("923609017", "checksum"),
    ("123456785", "start with 8 or 9"),
])
def test_invalid_numbers_rejected_with_reason(raw, reason):
    with pytest.raises(InvalidOrgNumber, match=reason):
        normalize(raw)


def test_check_digit_ten_is_invalid():
    # prefix whose weighted sum gives remainder 1 -> check digit 10 -> no valid number exists
    from signalpost.orgnr import check_digit
    prefixes = [f"8{n:07d}" for n in range(200) if check_digit(f"8{n:07d}") is None]
    assert prefixes, "expected at least one impossible prefix"
    assert not any(is_valid(p + str(d)) for p in prefixes[:3] for d in range(10))


def test_non_string_rejected():
    with pytest.raises(InvalidOrgNumber):
        normalize(923609016)  # type: ignore[arg-type]
