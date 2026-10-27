"""The CRD schema contract the operator reads against."""

import os
import subprocess

import pytest
from kubernetes.client.exceptions import ApiException

GROUP = "quota-warden.gnush.github.io"
VERSION = "v1alpha1"


@pytest.fixture
def create(custom):
    """Create a cluster-scoped object and return it as stored; deleted on teardown."""
    created = []

    def _create(plural, name, spec):
        body = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": {
                "quotawardenconfigs": "QuotaWardenConfig",
                "quotabases": "QuotaBase",
                "quotaoverrides": "QuotaOverride",
                "quotaextensions": "QuotaExtension",
            }[plural],
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
    assert spec == {
        "resourceQuotaName": "quota-warden",
        "dryRun": False,
        "foreignResourceQuotas": "report",
        "onUnmanage": "delete",
    }


@pytest.mark.parametrize("field", ["foreignResourceQuotas", "onUnmanage"])
def test_config_rejects_an_unknown_mode(create, field):
    with pytest.raises(ApiException) as exc:
        create("quotawardenconfigs", "e2e-bad-mode", {field: "bogus"})
    assert exc.value.status == 422


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


def test_override_takes_any_key_as_int_or_string(create):
    spec = {"namespace": "quota-user-a", "resources": {"cpu": "120", "pods": 30}}
    assert create("quotaoverrides", "quota-user-a", spec)["spec"] == spec


@pytest.mark.parametrize("spec", [{"resources": {"cpu": "1"}}, {"namespace": ""}])
def test_override_requires_a_namespace(create, spec):
    with pytest.raises(ApiException) as exc:
        create("quotaoverrides", "e2e-override-no-ns", spec)
    assert exc.value.status == 422


def test_override_name_must_be_its_namespace(create):
    with pytest.raises(ApiException) as exc:
        create("quotaoverrides", "quota-user-a-override", {"namespace": "quota-user-a"})
    assert exc.value.status == 422
    assert "metadata.name must equal spec.namespace" in exc.value.body


def test_override_cannot_be_retargeted(create, custom):
    create("quotaoverrides", "quota-user-a", {"namespace": "quota-user-a"})
    patch = {"spec": {"namespace": "quota-user-b"}}
    with pytest.raises(ApiException) as exc:
        custom.patch_cluster_custom_object(GROUP, VERSION, "quotaoverrides", "quota-user-a", patch)
    assert exc.value.status == 422


def test_extension_takes_any_key_as_int_or_string(create):
    spec = {
        "namespace": "quota-user-a",
        "expirationDate": "2026-11-08",
        "resources": {"requests.nvidia.com/gpu": "8", "pods": 30},
    }
    assert create("quotaextensions", "e2e-extension", spec)["spec"] == spec


def test_extension_accepts_a_past_date(create):
    # schema checks syntax only; expiry is the operator's
    spec = {"namespace": "quota-user-a", "expirationDate": "2020-01-01"}
    assert create("quotaextensions", "e2e-extension-past", spec)["spec"] == spec


@pytest.mark.parametrize(
    "spec",
    [
        {"expirationDate": "2026-11-08"},
        {"namespace": "", "expirationDate": "2026-11-08"},
        {"namespace": "quota-user-a"},
        {"namespace": "quota-user-a", "expirationDate": "2026-02-30"},
        {"namespace": "quota-user-a", "expirationDate": "20261108"},
        {"namespace": "quota-user-a", "expirationDate": "2026-11-08T00:00:00Z"},
        {"namespace": "quota-user-a", "expirationDate": "next week"},
    ],
)
def test_extension_requires_a_namespace_and_a_valid_date(create, spec):
    with pytest.raises(ApiException) as exc:
        create("quotaextensions", "e2e-extension-invalid", spec)
    assert exc.value.status == 422


COLUMNS = {
    "quotabases": (
        {"e2e-columns": {"resources": {"cpu": "18", "memory": "130Gi"}}},
        ["NAME", "CPU", "MEMORY", "VALID", "APPLIED", "AGE", "ALL"],
    ),
    # an override is named after its namespace
    "quotaoverrides": (
        {"quota-user-a": {"namespace": "quota-user-a", "resources": {"cpu": "2"}}},
        ["NAME", "NAMESPACE", "CPU", "MEMORY", "VALID", "APPLIED", "AGE", "ALL"],
    ),
    "quotaextensions": (
        {"e2e-columns": {"namespace": "quota-user-a", "expirationDate": "2026-11-08",
                         "resources": {"cpu": "2"}}},
        ["NAME", "NAMESPACE", "EXPIRES", "CPU", "MEMORY", "VALID", "STATUS", "AGE", "ALL"],
    ),
    "quotawardenconfigs": (
        {"e2e-columns": {}},
        ["NAME", "READY", "DRYRUN", "QUOTA", "AGE"],
    ),
}


@pytest.mark.parametrize("plural", COLUMNS)
def test_printer_columns(create, plural):
    # `kubectl get` prints no header for an empty list, so each kind needs a row
    specs, expected = COLUMNS[plural]
    for name, spec in specs.items():
        create(plural, name, spec)
    header = subprocess.run(
        ["kubectl", "--context", os.environ["E2E_CONTEXT"], "get", plural],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()[0]
    assert header.split() == expected


def test_the_all_column_shows_keys_no_other_column_names(create):
    spec = {"namespace": "quota-user-a", "expirationDate": "2026-11-08",
            "resources": {"requests.nvidia.com/gpu": "8"}}
    create("quotaextensions", "e2e-extension-columns", spec)
    row = subprocess.run(
        ["kubectl", "--context", os.environ["E2E_CONTEXT"], "get", "quotaextensions",
         "e2e-extension-columns"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()[1]
    assert '"requests.nvidia.com/gpu":"8"' in row
