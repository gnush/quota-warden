"""The environment the operator suite is tested against.

Not a test of the operator. The namespace cases use real objects because a mocked
label dict passes while the real selector path is broken. The quantity cases
record observed API-server behaviour the operator depends on.
"""

from decimal import Decimal

import pytest
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from conftest import LABEL, MANAGED_LABEL


def _labels(api, name):
    return api.read_namespace(name).metadata.labels or {}


def test_managed_namespace_carries_the_selector_label(api):
    assert _labels(api, "quota-user-a").get(LABEL) == MANAGED_LABEL[LABEL]


def test_unmanaged_namespace_has_no_selector_label(api):
    assert _labels(api, "quota-user-b").get(LABEL) is None


def test_protected_namespace_is_labeled_like_a_managed_one(api):
    # Must match the selector AND be in protectedNamespaces, so targeting has a
    # real case where protection has to win.
    assert _labels(api, "quota-protected").get(LABEL) == MANAGED_LABEL[LABEL]


def _roundtrip(api, name, key, value):
    """Write `key: value` and return (stored_string, http_status)."""
    rq = {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": name, "namespace": "quota-user-a"},
        "spec": {"hard": {key: value}},
    }
    try:
        api.create_namespaced_resource_quota("quota-user-a", rq)
    except ApiException as exc:
        return None, exc.status
    try:
        stored = api.read_namespaced_resource_quota(name, "quota-user-a").spec.hard[key]
        return stored, 200
    finally:
        api.delete_namespaced_resource_quota(name, "quota-user-a")


# --- properties the operator depends on ------------------------------------


def test_quantity_survives_the_round_trip_by_value(api):
    # The operator writes quantities; this is the contract that what they write is
    # what the cluster holds.
    for i, (key, value) in enumerate(
        [
            ("requests.storage", "422Ti"),
            ("cpu", "500m"),
            ("cpu", "1.5"),
            ("cpu", "1024Gi"),
            ("memory", "130Gi"),
            ("cpu", "0"),
        ]
    ):
        stored, status = _roundtrip(api, f"quantity-value-{i}", key, value)
        assert status == 200, f"{key}={value} rejected"
        assert parse_quantity(stored) == parse_quantity(value), (
            f"{key}={value} stored as {stored}, which is a different value"
        )


def test_api_server_canonicalises_the_display_form(api):
    # Same value, different text. Comparing written-vs-read strings would
    # report drift on every reconcile, so it must compare parsed Decimals.
    assert _roundtrip(api, "canon-gi", "cpu", "1024Gi")[0] == "1Ti"
    assert _roundtrip(api, "canon-frac", "cpu", "1.5")[0] == "1500m"


def test_exponent_notation_is_accepted_and_canonicalised_not_corrupted(api):
    # `4.6E+14` is accepted and stored as `460e12` — the same number, so the cost
    # of an exponent is legibility, not correctness.
    stored, status = _roundtrip(api, "exp", "cpu", "4.6E+14")
    assert status == 200
    assert stored == "460e12"
    assert parse_quantity(stored) == Decimal("4.6E+14")


def test_sub_milli_values_round_up_to_1m(api):
    # The one lossy case. Nothing in this design emits a sub-milli quantity; this
    # guards a source containing one against being silently bumped.
    for i, value in enumerate(["100n", "0.0001"]):
        stored, status = _roundtrip(api, f"submilli-{i}", "cpu", value)
        assert status == 200
        assert parse_quantity(stored) == parse_quantity("1m")


# --- fractional acceptance is Kubernetes' rule, not the operator's ----------


@pytest.mark.parametrize(
    "key,value,accepted",
    [
        # Quantity-shaped keys take a fraction.
        ("cpu", "1.5", True),
        ("memory", "1.5", True),
        ("requests.storage", "1.5", True),
        ("requests.ephemeral-storage", "1.5", True),
        # Integer-shaped keys do not.
        ("pods", "1.5", False),
        ("persistentvolumeclaims", "1.5", False),
        ("nvidia.com/gpu", "1.5", False),
        ("example.com/foo", "1.5", False),
        # an API-server inconsistency, not a meaningful prefix: not schedulable,
        # yet fractional here while `nvidia.com/gpu` above is rejected
        ("requests.nvidia.com/gpu", "1.5", True),
    ],
)
def test_fractional_acceptance_depends_on_key_shape(api, key, value, accepted):
    stored, status = _roundtrip(api, "frac", key, value)
    assert (status == 200) is accepted, f"{key}={value} -> {status}"
    if accepted:
        assert parse_quantity(stored) == parse_quantity(value)
