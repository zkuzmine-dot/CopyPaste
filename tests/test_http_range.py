import pytest

from app.utils.http_range import RangeNotSatisfiableError, parse_range

SIZE = 1000


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=0-99", (0, 99)),
        ("bytes=100-", (100, 999)),
        ("bytes=-100", (900, 999)),
        ("bytes=-5000", (0, 999)),
        ("bytes=990-5000", (990, 999)),
        ("bytes=999-999", (999, 999)),
        (" bytes=5-6 ", (5, 6)),
    ],
    ids=[
        "closed",
        "open_end",
        "suffix",
        "suffix_longer_than_file",
        "end_past_size",
        "last_byte",
        "spaces",
    ],
)
def test_single_range(header, expected):
    assert parse_range(header, SIZE) == expected


@pytest.mark.parametrize(
    "header",
    [None, "", "bytes=0-1,5-6", "bytes=-", "bytes=9-3", "items=0-5", "bytes=a-b", "bytes=٣-٤"],
    ids=[
        "none",
        "empty",
        "several",
        "no_numbers",
        "reversed",
        "other_unit",
        "letters",
        "non_ascii_digits",
    ],
)
def test_unsupported_range_means_whole_file(header):
    assert parse_range(header, SIZE) is None


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=5000-6000", "bytes=-0"])
def test_unsatisfiable_range(header):
    with pytest.raises(RangeNotSatisfiableError):
        parse_range(header, SIZE)
