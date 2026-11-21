import pytest

from flood_agent.queries import in_cidr


@pytest.mark.parametrize(
    "ip, cidr, expected",
    [
        ("10.1.2.3", "10.1.0.0/16", 1),
        ("10.2.2.3", "10.1.0.0/16", 0),
        ("2001:db8::1", "2001:db8::/32", 1),
        ("2001:db9::1", "2001:db8::/32", 0),
        ("10.1.2.3", "2001:db8::/32", 0),
        ("2001:db8::1", "10.0.0.0/8", 0),
        ("::ffff:10.1.2.3", "10.1.0.0/16", 1),
        ("::ffff:10.2.2.3", "10.1.0.0/16", 0),
        ("not-an-ip", "10.0.0.0/8", None),
        ("10.1.2.3", "garbage", None),
        (None, "10.0.0.0/8", None),
        ("10.1.2.3", None, None),
    ],
)
def test_in_cidr(ip, cidr, expected):
    assert in_cidr(ip, cidr) == expected
