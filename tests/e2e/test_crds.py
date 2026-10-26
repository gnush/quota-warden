"""Stage 3: the schema contract the operator reads against."""

import pytest

GROUP = "quota-warden.gnush.github.io"
VERSION = "v1alpha1"


@pytest.fixture
def create(custom):
    """Create a cluster-scoped object and return it as stored; deleted on teardown."""
    created = []

    def _create(plural, name, spec):
        body = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": {"quotawardenconfigs": "QuotaWardenConfig", "quotabases": "QuotaBase"}[plural],
            "metadata": {"name": name},
            "spec": spec,
        }
        custom.create_cluster_custom_object(GROUP, VERSION, plural, body)
        created.append((plural, name))
        return custom.get_cluster_custom_object(GROUP, VERSION, plural, name)

    yield _create
    for plural, name in created:
        custom.delete_cluster_custom_object(GROUP, VERSION, plural, name)


def test_config_defaults_are_stored(create):
    spec = create("quotawardenconfigs", "e2e-defaults", {})["spec"]
    assert spec == {"resourceQuotaName": "quota-warden", "baseName": "default", "dryRun": False}


def test_empty_selector_is_kept_distinct_from_unset(create):
    # {} manages everything, unset manages nothing; pruning must not merge them
    spec = create("quotawardenconfigs", "e2e-empty-selector", {"managedNamespaces": {}})["spec"]
    assert spec["managedNamespaces"] == {}


def test_malformed_selector_is_stored_for_the_operator_to_report(create):
    selector = {"matchExpressions": [{"key": "a", "operator": "Bogus"}]}
    spec = create("quotawardenconfigs", "e2e-malformed", {"managedNamespaces": selector})["spec"]
    assert spec["managedNamespaces"] == selector


def test_base_takes_any_key_as_int_or_string(create):
    resources = {"cpu": "18", "persistentvolumeclaims": 30, "example.com/anything": "1Gi"}
    spec = create("quotabases", "e2e-base", {"resources": resources})["spec"]
    assert spec["resources"] == resources
