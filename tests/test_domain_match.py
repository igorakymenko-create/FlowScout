"""risk.domain_allowed() -- how a URL's host[:port] is compared to
allowed_domains. An entry without a port allows that host on any port;
an entry with one allows exactly that host:port. The regression: a
config of ["localhost"] stopped matching an app on localhost:8080, so
the start page itself read as an external excursion."""
import pytest

from flowscout.risk import classify, domain_allowed, Risk


@pytest.mark.parametrize("netloc, allowed, expected", [
    ("localhost:8080", ["localhost"], True),
    ("localhost:8080", ["localhost:8080"], True),
    ("localhost:8080", ["localhost:9090"], False),
    ("localhost", ["localhost:8080"], False),
    ("www.saucedemo.com", ["www.saucedemo.com"], True),
    ("www.saucedemo.com", ["saucedemo.com"], False),
    ("EXAMPLE.com:443", ["example.com"], True),
    ("user:pw@example.com", ["example.com"], True),
    ("[::1]:8080", ["[::1]"], True),
    ("[::1]:8080", ["[::1]:9000"], False),
    ("evil.com:8080", ["localhost"], False),
    ("localhost:8080", [], False),
    ("127.0.0.1:8996", ["127.0.0.1"], True),
])
def test_domain_allowed(netloc, allowed, expected):
    assert domain_allowed(netloc, allowed) is expected


def test_classify_treats_same_host_other_port_as_in_app_for_a_bare_host():
    risk, _ = classify("Other", "http://localhost:9000/x", "localhost:8080", ["localhost"])
    assert risk != Risk.DESTRUCTIVE


def test_classify_still_blocks_a_different_host():
    risk, reason = classify("Other", "http://evil.com/x", "localhost:8080", ["localhost"])
    assert risk == Risk.DESTRUCTIVE and "evil.com" in reason
