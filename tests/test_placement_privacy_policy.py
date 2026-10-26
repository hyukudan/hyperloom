"""CPU-only privacy policy regressions."""

try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from tests.placement_fixtures import identity
from tests.test_placement_privacy import private


def test_06_unmatched_4090_and_matched_policy_agree_across_views(identity):
    from hyperloom.web import api
    from hyperloom.web.features import machines
    raw = dict(uuid=identity, id=identity, name="NVIDIA RTX 4090")
    declared = [dict(uuid="other", name=raw["name"], excluded=False)]
    assert api._accelerator(raw, {}, {}, declared)["excluded"]
    declared[0]["uuid"] = identity
    assert not machines.excluded_card(raw, declared)
    for card, configured in ((dict(raw, device=identity[:12]), {"id": identity, "excluded": False}),
                             (dict(name=raw["name"], device=identity), {"uuid": identity, "excluded": False}),
                             (dict(name=raw["name"], pci="bus-test"), {"pci": "bus-test", "excluded": False})):
        assert not machines.excluded_card(card, [configured])
        assert not api._accelerator(card, {}, {}, [configured])["excluded"]


def test_11_all_committed_artifacts_pass_privacy_without_exceptions():
    from tests.test_placement_privacy import findings
    assert findings() == {}


@pytest.mark.parametrize("address", ["192.0.2.10", "127.0.0.1", "::1", "2001:db8::1",
                                     "fe80::abcd%test", "::ffff:192.0.2.1",
                                     "2001:0db8:0000:0000:0000:0000:0000:0001"])
def test_11_addresses_are_private_in_values_keys_and_endpoints(address):
    for value in (address, f"address {address}.", {"endpoint": f"http://[{address}]:8096/v1"},
                  {address: {"bytes": 42}}):
        assert private(value)


def test_11_address_placeholders_round_trip_original_bytes():
    from tests.portable_fixtures import restore, sanitize
    raw = '"http://192.0.2.10:8096", "192.0.2.1", "[2001:db8::1]", "[::1]"\n'
    portable = sanitize(raw)
    assert not private(portable)
    assert restore(portable).encode() == raw.encode()
