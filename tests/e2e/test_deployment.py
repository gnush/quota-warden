"""The operator as deployed: the manifests, the image, and the ClusterRole.

The other modules run `kopf run` from the host as the kubeconfig's user — an
admin in kind. Only this one runs under the ServiceAccount, so it is the only
place deploy/rbac.yaml is exercised.

`make e2e-deploy`, not `make e2e`: test_operator.py stops and restarts the
operator, which this module must not touch.
"""

import time
from datetime import UTC, datetime

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from conftest import GROUP, VERSION, delete, exists, wait_for

NAMESPACE = "quota-warden"
DEPLOYMENT = "quota-warden"

# matches no fixture namespace: the two e2e modules see none of each other's objects
LABEL = "e2e-deployment"
MANAGED_LABEL = {LABEL: "yes"}
# what deploy/deployment.yaml names in QUOTA_WARDEN_CONFIG
CONFIG = "default"
BASE = "e2e-deployment"
QUOTA = "quota-warden"
TARGET = "e2e-deployment-ns"
OTHER = "e2e-deployment-unmanaged"


def hard(api, namespace=TARGET):
    """spec.hard by value: the operator writes 1Gi as 1073741824."""
    try:
        live = api.read_namespaced_resource_quota(QUOTA, namespace).spec.hard or {}
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise
    return {key: parse_quantity(value) for key, value in live.items()}


def q(**values):
    return {key: parse_quantity(value) for key, value in values.items()}


def set_base(custom, **resources):
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


@pytest.fixture(scope="module")
def operator(api, custom):
    """Our config and namespaces on the installed Deployment; torn down after."""
    # the pod outlives the run, so its log holds earlier runs' failures
    start = datetime.now(UTC)
    # a previous run's namespace may still be terminating
    wait_for(lambda: not exists(api.read_namespace, TARGET), timeout=120)
    for name in (TARGET, OTHER):
        delete(api.delete_namespace, name)
    api.create_namespace({"metadata": {"name": TARGET, "labels": MANAGED_LABEL}})
    api.create_namespace({"metadata": {"name": OTHER}})
    custom.create_cluster_custom_object(
        GROUP,
        VERSION,
        "quotawardenconfigs",
        {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "QuotaWardenConfig",
            "metadata": {"name": CONFIG},
            "spec": {"managedNamespaces": {"matchLabels": MANAGED_LABEL}, "baseName": BASE},
        },
    )
    set_base(custom, cpu="2", memory="1Gi")
    yield start
    delete(custom.delete_cluster_custom_object, GROUP, VERSION, "quotabases", BASE)
    delete(custom.delete_cluster_custom_object, GROUP, VERSION, "quotawardenconfigs", CONFIG)
    for name in (TARGET, OTHER):
        delete(api.delete_namespace, name)


def running_pods(api):
    """Running and not terminating: a deleted pod keeps phase Running while it drains."""
    pods = api.list_namespaced_pod(NAMESPACE, label_selector="app.kubernetes.io/name=quota-warden")
    return [
        p
        for p in pods.items
        if p.status.phase == "Running" and p.metadata.deletion_timestamp is None
    ]


def pod(api):
    running = running_pods(api)
    assert len(running) == 1, [(p.metadata.name, p.status.phase) for p in running]
    return running[0]


def logs(api, since=None):
    """Pod log, or the part of it after `since` (a datetime)."""
    # the client has since_seconds only; round up, so the window starts before `since`
    kwargs = {}
    if since:
        kwargs["since_seconds"] = int((datetime.now(UTC) - since).total_seconds()) + 1
    return api.read_namespaced_pod_log(pod(api).metadata.name, NAMESPACE, **kwargs)


def replica_logs(api, since=None):
    """Every replica's log, one string: `logs()` reads a single pod, and peering runs two."""
    kwargs = {}
    if since:
        kwargs["since_seconds"] = int((datetime.now(UTC) - since).total_seconds()) + 1
    return "\n".join(
        api.read_namespaced_pod_log(p.metadata.name, NAMESPACE, **kwargs) for p in running_pods(api)
    )


def events(api, namespace, kind, uid, reason):
    """Events on one object, by uid: Events outlive the object and the run."""
    selector = f"involvedObject.kind={kind},involvedObject.uid={uid},reason={reason}"
    return [e.message for e in api.list_namespaced_event(namespace, field_selector=selector).items]


def namespace_events(api, namespace, reason):
    """Events about a Namespace; kopf posts them in `default`.

    By name, not uid: the body the operator posts for a Namespace carries none
    and the API server does not fill one in. Events outlive the namespace, so
    callers diff `uids` rather than trusting the count.
    """
    events = api.list_namespaced_event(
        "default",
        field_selector=f"involvedObject.kind=Namespace,involvedObject.name={namespace},"
        f"reason={reason}",
    ).items
    return events


def uids(events):
    return {e.metadata.uid for e in events}


# --- the manifests ----------------------------------------------------------


def test_the_pod_is_the_service_account_with_its_security_context(api, operator):
    body = pod(api)
    spec = body.spec
    # the identity every RBAC assertion below is about
    assert spec.service_account_name == "quota-warden"
    assert spec.security_context.run_as_non_root is True
    container = spec.containers[0]
    assert container.security_context.allow_privilege_escalation is False
    assert container.security_context.capabilities.drop == ["ALL"]
    assert body.status.container_statuses[0].ready


def test_both_probes_point_at_the_liveness_endpoint(api, operator):
    container = pod(api).spec.containers[0]
    this = {port.name: port.container_port for port in container.ports}
    for probe in (container.liveness_probe, container.readiness_probe):
        assert probe.http_get.path == "/healthz"
        # a named port; the number it resolves to is on the container
        assert this[probe.http_get.port] == 8080
    # Ready means kopf's health reporter answers, not that the process merely started
    assert pod(api).status.container_statuses[0].ready


def applied_events(api, namespace, uid):
    """`Applied` Events on one quota, by uid: the operator posts one per write it makes."""
    selector = f"involvedObject.kind=ResourceQuota,involvedObject.uid={uid},reason=Applied"
    return api.list_namespaced_event(namespace, field_selector=selector).items


def test_the_deployment_defaults_to_one_replica(api, operator):
    # raisable: peering lets only the highest priority sweep. See README, Replicas.
    scale = client.AppsV1Api().read_namespaced_deployment_scale(DEPLOYMENT, NAMESPACE)
    assert scale.spec.replicas == 1


def test_the_operator_holds_the_peering_object(api, operator):
    """RBAC: get and patch on kopf.dev/clusterkopfpeerings, and the object exists."""
    peering = client.CustomObjectsApi().get_cluster_custom_object(
        "kopf.dev", "v1", "clusterkopfpeerings", "default"
    )
    assert peering["status"], peering


def test_two_replicas_write_once_between_them(api, custom, operator):
    """The point of peering: a change reaches the quota once, not once per replica."""
    apps = client.AppsV1Api()
    try:
        apps.patch_namespaced_deployment_scale(DEPLOYMENT, NAMESPACE, {"spec": {"replicas": 2}})
        wait_for(lambda: len(running_pods(api)) == 2, timeout=120)
        # both replicas have read the listing and settled into their ranks
        time.sleep(20)
        output = replica_logs(api, since=operator)
        assert "peering priority" in output
        assert "Pausing operations in favour of" in output, output

        uid = api.read_namespaced_resource_quota(QUOTA, TARGET).metadata.uid
        before = uids(applied_events(api, TARGET, uid))
        set_base(custom, cpu="7", memory="1Gi")
        assert wait_for(lambda: hard(api) == q(cpu="7", memory="1Gi")), hard(api)
        # the write settles before anything else counts it
        time.sleep(10)
        written = [e for e in applied_events(api, TARGET, uid) if e.metadata.uid not in before]
        assert len(written) == 1, [e.message for e in written]
    finally:
        set_base(custom, cpu="2", memory="1Gi")
        wait_for(lambda: hard(api) == q(cpu="2", memory="1Gi"))
        apps.patch_namespaced_deployment_scale(DEPLOYMENT, NAMESPACE, {"spec": {"replicas": 1}})
        wait_for(lambda: len(running_pods(api)) == 1, timeout=120)


# --- the ClusterRole, one grant at a time -----------------------------------

# All of these also depend on kopf's CRD list/watch: without it no handler fires
# and every case times out.


def test_apply_writes_the_quota(api, operator):
    """resourcequotas: create and patch — server-side apply."""
    assert wait_for(lambda: hard(api) == q(cpu="2", memory="1Gi")), hard(api)


def test_a_key_added_by_someone_else_is_removed(api, operator):
    """resourcequotas: update — a removal needs a PUT, apply only merges."""
    wait_for(lambda: hard(api) == q(cpu="2", memory="1Gi"))
    api.patch_namespaced_resource_quota(
        QUOTA, TARGET, {"spec": {"hard": {"pods": "1"}}}, field_manager="e2e-hand"
    )
    assert wait_for(lambda: hard(api) == q(cpu="2", memory="1Gi")), hard(api)


def test_status_conditions_are_patched(api, custom, operator):
    """*/status: patch, on the config the operator loads first."""

    def ready():
        status = custom.get_cluster_custom_object(GROUP, VERSION, "quotawardenconfigs", CONFIG).get(
            "status", {}
        )
        return {c["type"]: c for c in status.get("conditions", [])}.get("Ready")

    assert wait_for(lambda: ready() and ready()["status"] == "True"), ready()


def test_an_expired_extension_is_deleted(api, custom, operator):
    """quotaextensions: delete — the reaper, on the cluster-scoped kind."""
    name = "e2e-deployment-expired"
    custom.create_cluster_custom_object(
        GROUP,
        VERSION,
        "quotaextensions",
        {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "QuotaExtension",
            "metadata": {"name": name},
            "spec": {
                "namespace": TARGET,
                "expirationDate": "2020-01-01",
                "resources": {"cpu": "8"},
            },
        },
    )
    assert wait_for(
        lambda: (
            not exists(custom.get_cluster_custom_object, GROUP, VERSION, "quotaextensions", name)
        )
    )
    # past its date when the first sweep saw it, so it was never applied
    assert hard(api) == q(cpu="2", memory="1Gi")


def test_an_event_is_written_on_the_quota(api, custom, operator):
    """events: create in the managed namespace."""
    wait_for(lambda: hard(api) == q(cpu="2", memory="1Gi"))
    uid = api.read_namespaced_resource_quota(QUOTA, TARGET).metadata.uid
    set_base(custom, cpu="3", memory="1Gi")
    assert wait_for(lambda: hard(api) == q(cpu="3", memory="1Gi")), hard(api)
    assert wait_for(lambda: events(api, TARGET, "ResourceQuota", uid, "Applied")), logs(api)


def test_an_event_about_a_cluster_scoped_object_lands_in_default(api, custom, operator):
    """events: create in `default` — kopf's fallback when it has no namespace."""
    before = uids(namespace_events(api, TARGET, "DryRun"))
    custom.patch_cluster_custom_object(
        GROUP, VERSION, "quotawardenconfigs", CONFIG, {"spec": {"dryRun": True}}
    )
    try:
        set_base(custom, cpu="4", memory="1Gi")

        def new():
            return uids(namespace_events(api, TARGET, "DryRun")) - before

        assert wait_for(new), logs(api)
        # dry run: reported, not written
        assert hard(api) == q(cpu="3", memory="1Gi")
    finally:
        custom.patch_cluster_custom_object(
            GROUP, VERSION, "quotawardenconfigs", CONFIG, {"spec": {"dryRun": False}}
        )
    assert wait_for(lambda: hard(api) == q(cpu="4", memory="1Gi")), hard(api)


def test_a_foreign_quota_is_reported_on_the_namespace(api, operator):
    """namespaces: list and watch — and the Warning Event that follows."""
    legacy = "e2e-deployment-legacy"
    before = uids(namespace_events(api, TARGET, "ForeignQuota"))
    api.create_namespaced_resource_quota(
        TARGET, {"metadata": {"name": legacy}, "spec": {"hard": {"cpu": "100"}}}
    )
    try:

        def new():
            return uids(namespace_events(api, TARGET, "ForeignQuota")) - before

        assert wait_for(new), logs(api)
        # reported, not deleted, and ours is unchanged
        assert hard(api) == q(cpu="4", memory="1Gi")
    finally:
        delete(api.delete_namespaced_resource_quota, legacy, TARGET)


def test_an_unmanaged_namespace_gets_nothing(api, operator):
    """The selector, as the pod sees it: nothing is written outside the match."""
    assert hard(api, OTHER) is None


def test_deleting_the_base_removes_the_quota(api, custom, operator):
    """resourcequotas: delete."""
    wait_for(lambda: hard(api) is not None)
    delete(custom.delete_cluster_custom_object, GROUP, VERSION, "quotabases", BASE)
    assert wait_for(lambda: hard(api) is None), hard(api)
    set_base(custom, cpu="2", memory="1Gi")


def test_the_operator_never_hit_a_forbidden(api, operator):
    """The cases above are the grants that were thought of; this is the ones that were not."""
    output = logs(api, since=operator)
    forbidden = [line for line in output.splitlines() if "Forbidden" in line or "403" in line]
    assert not forbidden, forbidden
    # a restart would make the cases above pass against a different process
    assert pod(api).status.container_statuses[0].restart_count == 0, output
