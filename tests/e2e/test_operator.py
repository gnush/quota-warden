"""The operator against the kind cluster, with every source kind.

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
    # a previous run's namespace may still be terminating; creating it would 409
    wait_for(lambda: not _exists(api.read_namespace, NAMESPACE), timeout=120)
    process = Operator(kubeconfig)
    try:
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
        process.start()
        yield process
    finally:
        _delete(custom.delete_cluster_custom_object, GROUP, VERSION, "quotabases", BASE)
        if process.poll() is None:
            # the operator removes its own quotas once the base is gone
            wait_for(lambda: hard(api) is None and hard(api, "quota-user-a") is None)
        failed = process.stop()
        _delete(custom.delete_cluster_custom_object, GROUP, VERSION, "quotawardenconfigs", CONFIG)
        _delete(api.delete_namespace, NAMESPACE)
        wait_for(lambda: not _exists(api.read_namespace, NAMESPACE), timeout=120)
        if failed:
            print(process.log.read_text())


class Operator:
    """`kopf run` as a subprocess; restartable, the log spans every run."""

    def __init__(self, kubeconfig):
        self.kubeconfig = kubeconfig
        self.log = kubeconfig.parent / "operator.log"
        self.process = None

    def start(self):
        with self.log.open("a") as out:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "kopf", "run", "--all-namespaces", "--standalone"]
                + ["-m", "quota_warden.handlers"],
                env={
                    **os.environ,
                    "KUBECONFIG": str(self.kubeconfig),
                    "QUOTA_WARDEN_CONFIG": CONFIG,
                },
                stdout=out,
                stderr=subprocess.STDOUT,
            )

    def poll(self):
        return None if self.process is None else self.process.poll()

    def stop(self) -> bool:
        """Whether the process had already died or exited uncleanly."""
        if self.process is None:
            return False
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=30)
        failed = self.process.returncode not in (0, -15)
        self.process = None
        return failed


def _exists(fn, *args):
    try:
        fn(*args)
    except ApiException as exc:
        if exc.status == 404:
            return False
        raise
    return True


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


def dry_run_events(api):
    # events about cluster-scoped objects land in `default`
    events = api.list_namespaced_event(
        "default", field_selector=f"involvedObject.name={NAMESPACE},reason=DryRun"
    ).items
    return [e.message for e in events]


def test_dry_run_reports_on_the_namespace_and_writes_nothing(api, custom, base):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="4")
    wait_for(lambda: hard(api) == q(cpu="4"))

    def set_dry_run(value):
        patch = {"spec": {"dryRun": value}}
        custom.patch_cluster_custom_object(GROUP, VERSION, "quotawardenconfigs", CONFIG, patch)

    set_dry_run(True)
    try:
        wait_for(lambda: ready(custom)["reason"] == "DryRun")
        base(cpu="6")
        expected = "dry run: would apply quota-warden: cpu=6"
        assert wait_for(lambda: expected in dry_run_events(api)), dry_run_events(api)
        assert hard(api) == q(cpu="4")
    finally:
        set_dry_run(False)
    assert wait_for(lambda: hard(api) == q(cpu="6")), hard(api)


def test_renaming_the_quota_removes_the_old_one(api, custom, base):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="4")
    wait_for(lambda: hard(api))

    def names():
        return {q.metadata.name for q in api.list_namespaced_resource_quota(NAMESPACE).items}

    def rename(value):
        patch = {"spec": {"resourceQuotaName": value}}
        custom.patch_cluster_custom_object(GROUP, VERSION, "quotawardenconfigs", CONFIG, patch)

    rename("e2e-renamed")
    try:
        assert wait_for(lambda: names() == {"e2e-renamed"}), names()
    finally:
        rename(QUOTA)
    assert wait_for(lambda: names() == {QUOTA}), names()


@pytest.fixture
def override(custom, operator):
    """Create an override; all are deleted on teardown."""
    created = []

    def _create(target, **resources):
        body = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "QuotaOverride",
            "metadata": {"name": target},
            "spec": {"namespace": target, "resources": resources},
        }
        custom.create_cluster_custom_object(GROUP, VERSION, "quotaoverrides", body)
        created.append(target)

    yield _create
    for name in created:
        _delete(custom.delete_cluster_custom_object, GROUP, VERSION, "quotaoverrides", name)


def conditions(custom, name):
    status = custom.get_cluster_custom_object(GROUP, VERSION, "quotaoverrides", name).get(
        "status", {}
    )
    return {c["type"]: c for c in status.get("conditions", [])}


def test_override_raises_a_key_and_omitted_keys_keep_the_base(api, base, override):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="2", memory="1Gi")
    override(NAMESPACE, cpu="8")
    assert wait_for(lambda: hard(api) == q(cpu="8", memory="1Gi")), hard(api)
    # a sweep writes namespaces in order; quota-user-a can still hold the last test's base
    assert wait_for(lambda: hard(api, "quota-user-a") == q(cpu="2", memory="1Gi")), hard(
        api, "quota-user-a"
    )


def test_override_lowers_below_the_base(api, base, override):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="2", memory="1Gi")
    override(NAMESPACE, cpu="1")
    assert wait_for(lambda: hard(api) == q(cpu="1", memory="1Gi")), hard(api)


def test_deleting_the_override_returns_the_namespace_to_the_base(api, custom, base, override):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="2")
    override(NAMESPACE, cpu="8")
    wait_for(lambda: hard(api) == q(cpu="8"))
    custom.delete_cluster_custom_object(GROUP, VERSION, "quotaoverrides", NAMESPACE)
    assert wait_for(lambda: hard(api) == q(cpu="2")), hard(api)


def test_override_on_an_unmanaged_namespace_is_inert(api, custom, base, override):
    base(cpu="2")
    override("quota-user-b", cpu="8")
    targeted = wait_for(lambda: conditions(custom, "quota-user-b").get("Targeted"))
    assert (targeted["status"], targeted["reason"]) == ("False", "Unmanaged")
    assert hard(api, "quota-user-b") is None


def test_deleting_the_base_removes_the_quota(api, custom, base):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="4")
    wait_for(lambda: hard(api))
    custom.delete_cluster_custom_object(GROUP, VERSION, "quotabases", BASE)
    assert wait_for(lambda: hard(api) is None and hard(api, "quota-user-a") is None)


@pytest.fixture
def extension(custom, operator):
    """Create an extension; any left are deleted on teardown."""
    created = []

    def _create(name, target, expires, **resources):
        body = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "QuotaExtension",
            "metadata": {"name": name},
            "spec": {"namespace": target, "expirationDate": expires, "resources": resources},
        }
        custom.create_cluster_custom_object(GROUP, VERSION, "quotaextensions", body)
        created.append(name)

    yield _create
    for name in created:
        _delete(custom.delete_cluster_custom_object, GROUP, VERSION, "quotaextensions", name)


# the cluster's clock is real; these dates are safely on either side of it
FUTURE = "2099-12-31"
PAST = "2020-01-01"


def extension_exists(custom, name):
    return _exists(custom.get_cluster_custom_object, GROUP, VERSION, "quotaextensions", name)


def test_extension_adds_on_top_of_the_override(api, base, override, extension):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="2", memory="1Gi")
    override(NAMESPACE, cpu="4")
    extension("e2e-ext-raise", NAMESPACE, FUTURE, cpu="8")
    assert wait_for(lambda: hard(api) == q(cpu="12", memory="1Gi")), hard(api)


def test_extensions_on_one_key_sum(api, base, extension):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="2")
    extension("e2e-ext-one", NAMESPACE, FUTURE, cpu="1")
    extension("e2e-ext-two", NAMESPACE, FUTURE, cpu="500m")
    assert wait_for(lambda: hard(api) == q(cpu="3.5")), hard(api)


def test_extension_created_expired_is_deleted_and_never_applied(api, custom, base, extension):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="2")
    wait_for(lambda: hard(api) == q(cpu="2"))
    extension("e2e-ext-past", NAMESPACE, PAST, cpu="8")
    assert wait_for(lambda: not extension_exists(custom, "e2e-ext-past"))
    assert hard(api) == q(cpu="2")


def test_expiry_while_the_operator_is_stopped(api, custom, operator, base, extension):
    label(api, MANAGED_LABEL[LABEL])
    base(cpu="2")
    extension("e2e-ext-stopped", NAMESPACE, FUTURE, cpu="8")
    assert wait_for(lambda: hard(api) == q(cpu="10")), hard(api)
    assert not operator.stop()
    try:
        # stands in for the clock passing expirationDate while nothing is watching
        patch = {"spec": {"expirationDate": PAST}}
        custom.patch_cluster_custom_object(
            GROUP, VERSION, "quotaextensions", "e2e-ext-stopped", patch
        )
        time.sleep(2)
        assert hard(api) == q(cpu="10")
    finally:
        operator.start()
    assert wait_for(lambda: hard(api) == q(cpu="2")), hard(api)
    assert wait_for(lambda: not extension_exists(custom, "e2e-ext-stopped"))


def test_operator_is_still_running(operator):
    assert operator.poll() is None
