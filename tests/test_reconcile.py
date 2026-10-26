import json
from types import SimpleNamespace as NS

import pytest
from kubernetes.client.exceptions import ApiException

from quota_warden.config import Config
from quota_warden.reconcile import FIELD_MANAGER, MANAGED_BY, read_object, sweep

SELECTOR = {"matchLabels": {"managed": "yes"}}
CONFIG = Config({"managedNamespaces": SELECTOR}, "quota-warden", "default", False)
OWNED = dict([MANAGED_BY])


def namespace(name, managed=True, phase="Active"):
    labels = {"managed": "yes"} if managed else {}
    return NS(metadata=NS(name=name, labels=labels), status=NS(phase=phase))


def base(**resources):
    return {"metadata": {"name": "default"}, "spec": {"resources": resources}}


class FakeCore:
    """ResourceQuotas in a dict; records every write. A key named `bad` is rejected."""

    def __init__(self, namespaces, quotas=None):
        self.namespaces = namespaces
        self.quotas = quotas or {}
        self.writes = []
        self.dry_runs = []

    def list_namespace(self):
        return NS(items=self.namespaces)

    def read_namespaced_resource_quota(self, name, namespace):
        try:
            return self.quotas[namespace, name]
        except KeyError:
            raise ApiException(status=404) from None

    def patch_namespaced_resource_quota(self, name, namespace, body, **kwargs):
        dry_run = kwargs.pop("dry_run", None)
        assert dry_run in (None, "All")
        assert kwargs == {
            "field_manager": FIELD_MANAGER,
            "force": True,
            "_content_type": "application/apply-patch+yaml",
        }
        hard = body["spec"]["hard"]
        if "bad" in hard:
            message = json.dumps({"message": 'ResourceQuota "x" is invalid: spec.hard[bad]'})
            raise ApiException(status=422, body=message)
        if dry_run:
            self.dry_runs.append(namespace)
            return
        self.writes.append(("apply", namespace, name))
        self.quotas[namespace, name] = quota(hard, body["metadata"]["labels"])

    def delete_namespaced_resource_quota(self, name, namespace):
        self.writes.append(("delete", namespace, name))
        del self.quotas[namespace, name]


def quota(hard, labels=OWNED):
    return NS(metadata=NS(labels=labels), spec=NS(hard=dict(hard)))


def run(core, base_object):
    results, issues = sweep(core, base_object, CONFIG)
    return {r.namespace: r for r in results}, issues


def test_managed_namespace_gets_the_base():
    core = FakeCore([namespace("a")])
    results, _ = run(core, base(cpu="18", memory=130 * 2**30))
    assert results["a"].action == "applied"
    assert core.quotas["a", "quota-warden"].spec.hard == {"cpu": "18", "memory": "139586437120"}
    assert core.quotas["a", "quota-warden"].metadata.labels == OWNED


def test_unmanaged_and_terminating_namespaces_are_skipped():
    core = FakeCore([namespace("b", managed=False), namespace("c", phase="Terminating")])
    results, _ = run(core, base(cpu="1"))
    assert {r.action for r in results.values()} == {"skipped"}
    assert core.writes == []


def test_equal_value_in_another_unit_does_not_write():
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"memory": "1Gi"})})
    results, _ = run(core, base(memory="1024Mi"))
    assert results["a"].action == "unchanged"
    assert core.writes == []


@pytest.mark.parametrize(
    "live",
    [
        quota({"cpu": "2"}),  # value changed
        quota({"cpu": "1", "memory": "1Gi"}),  # key dropped from the base
        quota({"cpu": "1"}, labels={}),  # same values, label missing
    ],
)
def test_difference_applies(live):
    core = FakeCore([namespace("a")], {("a", "quota-warden"): live})
    results, _ = run(core, base(cpu="1"))
    assert results["a"].action == "applied"
    assert core.quotas["a", "quota-warden"].spec.hard == {"cpu": "1"}


def test_missing_base_deletes_the_owned_quota():
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"cpu": "1"})})
    results, _ = run(core, None)
    assert results["a"].action == "deleted"
    assert core.quotas == {}


def test_empty_merge_leaves_an_unlabeled_quota_alone():
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"cpu": "1"}, labels={})})
    results, _ = run(core, base())
    assert results["a"].action == "unchanged"
    assert core.writes == []


def test_invalid_value_is_dropped_and_reported():
    core = FakeCore([namespace("a")])
    results, issues = run(core, base(cpu="1", memory="-1"))
    assert core.quotas["a", "quota-warden"].spec.hard == {"cpu": "1"}
    assert [(i.kind, i.source, i.key) for i in issues] == [("invalid", "default", "memory")]


def test_rejected_write_keeps_the_last_quota_and_carries_the_message():
    live = quota({"cpu": "1"})
    core = FakeCore([namespace("a"), namespace("b")], {("a", "quota-warden"): live})
    results, _ = run(core, base(cpu="2", bad="1"))
    assert results["a"].action == "rejected"
    assert "spec.hard[bad]" in results["a"].error
    assert core.quotas["a", "quota-warden"] is live
    assert results["b"].action == "rejected"


def test_other_api_errors_propagate():
    core = FakeCore([namespace("a")])
    core.read_namespaced_resource_quota = lambda *_: (_ for _ in ()).throw(ApiException(500))
    with pytest.raises(ApiException):
        run(core, base(cpu="1"))


def test_read_object_propagates_non_404():
    class Broken:
        def get_cluster_custom_object(self, *_):
            raise ApiException(status=403)

    with pytest.raises(ApiException):
        read_object(Broken(), "quotabases", "default")


def test_read_object_absent_is_none():
    class Absent:
        def get_cluster_custom_object(self, *_):
            raise ApiException(status=404)

    assert read_object(Absent(), "quotabases", "default") is None


DRY = Config(CONFIG.spec, "quota-warden", "default", True)


def test_dry_run_validates_on_the_server_and_stores_nothing():
    core = FakeCore([namespace("a")])
    results, _ = sweep(core, base(cpu="1"), DRY)
    assert (results[0].action, results[0].dry_run) == ("applied", True)
    assert core.dry_runs == ["a"]
    assert core.writes == [] and core.quotas == {}


def test_dry_run_still_reports_rejection():
    core = FakeCore([namespace("a")])
    results, _ = sweep(core, base(bad="1"), DRY)
    assert (results[0].action, results[0].dry_run) == ("rejected", True)


def test_dry_run_does_not_delete():
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"cpu": "1"})})
    results, _ = sweep(core, None, DRY)
    assert (results[0].action, results[0].dry_run) == ("deleted", True)
    assert core.writes == [] and ("a", "quota-warden") in core.quotas


def test_dry_run_unchanged_is_not_flagged():
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"cpu": "1"})})
    results, _ = sweep(core, base(cpu="1"), DRY)
    assert (results[0].action, results[0].dry_run) == ("unchanged", False)
