"""The operator against the kind cluster, QuotaBase as the only source.

The operator runs as `kopf run` in a subprocess with a kubeconfig holding only
E2E_CONTEXT, so it cannot reach whatever cluster the ambient context points at.
"""

import os
import subprocess
import sys
import time

import pytest
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from conftest import LABEL, MANAGED_LABEL

GROUP = "quota-warden.gnush.github.io"
VERSION = "v1alpha1"
CONFIG = "e2e"
BASE = "e2e"
QUOTA = "quota-warden"
NAMESPACE = "e2e-operator"


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(0.2)


def hard(api, namespace=NAMESPACE):
    """spec.hard by value: the operator writes 1Gi as 1073741824."""
    try:
        live = api.read_namespaced_resource_quota(QUOTA, namespace).spec.hard
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise
    return {key: parse_quantity(value) for key, value in live.items()}


def q(**values):
    return {key: parse_quantity(value) for key, value in values.items()}


def selector(value):
    return {"spec": {"managedNamespaces": value}}


@pytest.fixture(scope="module")
def operator(api, custom, tmp_path_factory):
    context = os.environ["E2E_CONTEXT"]
    kubeconfig = tmp_path_factory.mktemp("operator") / "kubeconfig"
    kubeconfig.write_text(
        subprocess.run(
            ["kubectl", "config", "view", "--minify", "--flatten", "--context", context],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    custom.create_cluster_custom_object(
        GROUP,
        VERSION,
        "quotawardenconfigs",
        {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "QuotaWardenConfig",
            "metadata": {"name": CONFIG},
            "spec": {
                "managedNamespaces": {"matchLabels": MANAGED_LABEL},
                "protectedNamespaces": ["quota-protected"],
                "baseName": BASE,
            },
        },
    )
    api.create_namespace({"metadata": {"name": NAMESPACE}})
    log = kubeconfig.parent / "operator.log"
    with log.open("w") as out:
        process = subprocess.Popen(
            [sys.executable, "-m", "kopf", "run", "--all-namespaces", "--standalone"]
            + ["-m", "quota_warden.handlers"],
            env={**os.environ, "KUBECONFIG": str(kubeconfig), "QUOTA_WARDEN_CONFIG": CONFIG},
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    try:
        yield process
    finally:
        _delete(custom.delete_cluster_custom_object, GROUP, VERSION, "quotabases", BASE)
        # the operator removes its own quotas once the base is gone
        wait_for(lambda: hard(api) is None and hard(api, "quota-user-a") is None)
        process.terminate()
        process.wait(timeout=30)
        _delete(custom.delete_cluster_custom_object, GROUP, VERSION, "quotawardenconfigs", CONFIG)
        _delete(api.delete_namespace, NAMESPACE)
        if process.returncode not in (0, -15):
            print(log.read_text())


def _delete(fn, *args):
    try:
        fn(*args)
    except ApiException as exc:
        if exc.status != 404:
            raise


@pytest.fixture
def base(custom, operator):
    """Set the base's resources, creating it if needed."""

    def _set(**resources):
        body = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "QuotaBase",
            "metadata": {"name": BASE},
            "spec": {"resources": resources},
        }
        try:
            custom.create_cluster_custom_object(GROUP, VERSION, "quotabases", body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            # JSON patch: a merge patch would keep keys left out of `resources`
            custom.patch_cluster_custom_object(
                GROUP,
                VERSION,
                "quotabases",
                BASE,
                [{"op": "replace", "path": "/spec/resources", "value": resources}],
                _content_type="application/json-patch+json",
            )

    return _set


def label(api, value):
    api.patch_namespace(NAMESPACE, {"metadata": {"labels": {LABEL: value}}})


def ready(custom):
    status = custom.get_cluster_custom_object(GROUP, VERSION, "quotawardenconfigs", CONFIG).get(
        "status", {}
    )
    return {c["type"]: c for c in status.get("conditions", [])}.get("Ready")


def test_labeling_a_namespace_creates_the_quota(api, base):
    base(cpu="2", memory="1Gi")
    assert wait_for(lambda: hard(api, "quota-user-a")) == q(cpu="2", memory="1Gi")
    assert hard(api) is None
    label(api, MANAGED_LABEL[LABEL])
    assert wait_for(lambda: hard(api)) == q(cpu="2", memory="1Gi")


def test_unmanaged_and_protected_namespaces_get_none(api, base):
    base(cpu="2", memory="1Gi")
    wait_for(lambda: hard(api, "quota-user-a"))
    assert hard(api, "quota-user-b") is None
    assert hard(api, "quota-protected") is None


def test_editing_the_base_updates_the_quota(api, base):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="4")
    assert wait_for(lambda: hard(api) == q(cpu="4")), hard(api)


def test_a_sweep_with_nothing_to_change_writes_nothing(api, custom, base):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="4")
    wait_for(lambda: hard(api) == q(cpu="4"))
    quota = api.read_namespaced_resource_quota(QUOTA, NAMESPACE).metadata.resource_version
    config = custom.get_cluster_custom_object(GROUP, VERSION, "quotawardenconfigs", CONFIG)
    # an annotation is a watch event, so it requests a sweep, but changes nothing it reads
    api.patch_namespace(NAMESPACE, {"metadata": {"annotations": {"e2e/poke": str(time.time())}}})
    time.sleep(3)
    assert api.read_namespaced_resource_quota(QUOTA, NAMESPACE).metadata.resource_version == quota
    assert (
        custom.get_cluster_custom_object(GROUP, VERSION, "quotawardenconfigs", CONFIG)["metadata"][
            "resourceVersion"
        ]
        == config["metadata"]["resourceVersion"]
    )


def test_malformed_selector_sets_the_condition_and_leaves_quotas(api, custom, base):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="4")
    wait_for(lambda: hard(api) == q(cpu="4"))
    patch = custom.patch_cluster_custom_object
    patch(
        GROUP,
        VERSION,
        "quotawardenconfigs",
        CONFIG,
        selector({"matchExpressions": [{"key": "k", "operator": "Bogus"}]}),
    )
    try:
        condition = wait_for(lambda: (c := ready(custom)) and c["status"] == "False" and c)
        assert condition["reason"] == "ConfigError" and "Bogus" in condition["message"]
        base(cpu="8")
        time.sleep(3)
        assert hard(api) == q(cpu="4")
    finally:
        # merge patch would keep matchExpressions next to the restored matchLabels
        body = custom.get_cluster_custom_object(GROUP, VERSION, "quotawardenconfigs", CONFIG)
        body["spec"]["managedNamespaces"] = {"matchLabels": MANAGED_LABEL}
        custom.replace_cluster_custom_object(GROUP, VERSION, "quotawardenconfigs", CONFIG, body)
    assert wait_for(lambda: ready(custom)["status"] == "True")
    assert wait_for(lambda: hard(api) == q(cpu="8")), hard(api)


def test_deleting_the_base_removes_the_quota(api, custom, base):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="4")
    wait_for(lambda: hard(api))
    custom.delete_cluster_custom_object(GROUP, VERSION, "quotabases", BASE)
    assert wait_for(lambda: hard(api) is None and hard(api, "quota-user-a") is None)


def test_operator_is_still_running(operator):
    assert operator.poll() is None
