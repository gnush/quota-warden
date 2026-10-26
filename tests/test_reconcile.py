import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest
from kubernetes.client.exceptions import ApiException

from quota_warden.config import Config
from quota_warden.reconcile import FIELD_MANAGER, MANAGED_BY, read_object, sweep

SELECTOR = {"matchLabels": {"managed": "yes"}}
CONFIG = Config(
    {"managedNamespaces": SELECTOR}, "quota-warden", "default", False, "report", "delete"
)
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

    def list_namespaced_resource_quota(self, namespace):
        items = []
        for (ns, name), q in self.quotas.items():
            if ns == namespace:
                q.metadata.name = name
                items.append(q)
        return NS(items=items)

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


def override(name, target, **resources):
    return {"metadata": {"name": name}, "spec": {"namespace": target, "resources": resources}}


NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def extension(name, target, expires="2026-11-08", **resources):
    spec = {"namespace": target, "expirationDate": expires, "resources": resources}
    return {"metadata": {"name": name}, "spec": spec}


def run(core, base_object, overrides=(), extensions=(), now=NOW):
    results, issues = sweep(core, base_object, list(overrides), list(extensions), CONFIG, now)
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
    core.list_namespaced_resource_quota = lambda *_: (_ for _ in ()).throw(ApiException(500))
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


DRY = Config(CONFIG.spec, "quota-warden", "default", True, "report", "delete")


def test_dry_run_validates_on_the_server_and_stores_nothing():
    core = FakeCore([namespace("a")])
    results, _ = sweep(core, base(cpu="1"), [], [], DRY, NOW)
    assert (results[0].action, results[0].dry_run) == ("applied", True)
    assert core.dry_runs == ["a"]
    assert core.writes == [] and core.quotas == {}


def test_dry_run_still_reports_rejection():
    core = FakeCore([namespace("a")])
    results, _ = sweep(core, base(bad="1"), [], [], DRY, NOW)
    assert (results[0].action, results[0].dry_run) == ("rejected", True)


def test_dry_run_does_not_delete():
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"cpu": "1"})})
    results, _ = sweep(core, None, [], [], DRY, NOW)
    assert (results[0].action, results[0].dry_run) == ("deleted", True)
    assert core.writes == [] and ("a", "quota-warden") in core.quotas


def test_dry_run_unchanged_is_not_flagged():
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"cpu": "1"})})
    results, _ = sweep(core, base(cpu="1"), [], [], DRY, NOW)
    assert (results[0].action, results[0].dry_run) == ("unchanged", False)


def test_labeled_quota_under_another_name_is_deleted():
    core = FakeCore(
        [namespace("a")],
        {
            ("a", "old-name"): quota({"cpu": "1"}),
            ("a", "foreign"): quota({"cpu": "1"}, labels={}),
        },
    )
    results, _ = run(core, base(cpu="1"))
    assert results["a"].orphans == ("old-name",)
    assert set(core.quotas) == {("a", "foreign"), ("a", "quota-warden")}


def test_orphans_in_unmanaged_namespaces_are_left():
    core = FakeCore([namespace("b", managed=False)], {("b", "old-name"): quota({"cpu": "1"})})
    run(core, base(cpu="1"))
    assert core.writes == []


def test_dry_run_reports_orphans_without_deleting():
    core = FakeCore(
        [namespace("a")],
        {("a", "quota-warden"): quota({"cpu": "1"}), ("a", "old-name"): quota({"cpu": "1"})},
    )
    results, _ = sweep(core, base(cpu="1"), [], [], DRY, NOW)
    assert (results[0].action, results[0].orphans, results[0].dry_run) == (
        "unchanged",
        ("old-name",),
        True,
    )
    assert core.writes == []


def test_override_applies_only_to_its_namespace():
    core = FakeCore([namespace("a"), namespace("b")])
    run(core, base(cpu="18", memory="1"), [override("a-o", "a", cpu="120")])
    assert core.quotas["a", "quota-warden"].spec.hard == {"cpu": "120", "memory": "1"}
    assert core.quotas["b", "quota-warden"].spec.hard == {"cpu": "18", "memory": "1"}


def test_override_lowers_below_the_base():
    # replace per key, not max(): an override is how a namespace gets less
    core = FakeCore([namespace("a")])
    run(core, base(cpu="18", memory="1"), [override("a-o", "a", cpu="2")])
    assert core.quotas["a", "quota-warden"].spec.hard == {"cpu": "2", "memory": "1"}


def test_override_without_a_base_defines_the_quota():
    core = FakeCore([namespace("a"), namespace("b")])
    run(core, None, [override("a-o", "a", cpu="2")])
    assert core.quotas == {("a", "quota-warden"): core.quotas["a", "quota-warden"]}
    assert core.quotas["a", "quota-warden"].spec.hard == {"cpu": "2"}


def test_removing_the_last_override_without_a_base_deletes_the_quota():
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"cpu": "2"})})
    run(core, None, [])
    assert core.quotas == {}


def test_issues_are_reported_once_including_inert_overrides():
    core = FakeCore([namespace("a"), namespace("b"), namespace("c", managed=False)])
    overrides = [
        override("a-o", "a", cpu="x"),
        override("c-o", "c", cpu="y"),
        override("gone-o", "gone", cpu="z"),
    ]
    _, issues = run(core, base(memory="z"), overrides)
    assert sorted((i.source, i.key) for i in issues) == [
        ("a-o", "cpu"),
        ("c-o", "cpu"),
        ("default", "memory"),
        ("gone-o", "cpu"),
    ]


def test_skipped_namespaces_carry_the_reason():
    core = FakeCore(
        [
            namespace("u", managed=False),
            namespace("kube-system"),
            namespace("t", phase="Terminating"),
        ]
    )
    results, _ = run(core, base(cpu="1"))
    assert {n: r.reason for n, r in results.items()} == {
        "u": "Unmanaged",
        "kube-system": "Protected",
        "t": "Terminating",
    }
    assert core.writes == []


def test_rejection_is_blamed_on_the_override_that_set_the_key():
    core = FakeCore([namespace("a")])
    results, _ = run(core, base(cpu="1", bad="1"), [override("o", "a", bad="2")])
    assert results["a"].blame == ("o",)


def test_rejection_of_a_base_key_is_blamed_on_the_base():
    core = FakeCore([namespace("a")])
    results, _ = run(core, base(bad="1"), [override("o", "a", cpu="2")])
    assert results["a"].blame == ("default",)


def test_rejection_naming_no_key_is_blamed_on_every_contributor():
    class Unnamed(FakeCore):
        def patch_namespaced_resource_quota(self, *args, **kwargs):
            raise ApiException(status=422, body=json.dumps({"message": "invalid"}))

    results, _ = run(Unnamed([namespace("a")]), base(cpu="1"), [override("o", "a", cpu="2")])
    assert results["a"].blame == ("default", "o")


def test_extension_adds_on_top_of_the_override():
    core = FakeCore([namespace("a"), namespace("b")])
    run(
        core,
        base(cpu="18", gpu="1"),
        [override("a-o", "a", gpu="3")],
        [extension("a-e", "a", gpu="8")],
    )
    assert core.quotas["a", "quota-warden"].spec.hard == {"cpu": "18", "gpu": "11"}
    assert core.quotas["b", "quota-warden"].spec.hard == {"cpu": "18", "gpu": "1"}


def test_extensions_on_one_key_sum():
    core = FakeCore([namespace("a")])
    extensions = [extension("e1", "a", gpu="8"), extension("e2", "a", gpu="2")]
    run(core, base(gpu="1"), [], extensions)
    assert core.quotas["a", "quota-warden"].spec.hard == {"gpu": "11"}


def test_extension_without_base_or_override_defines_the_key():
    core = FakeCore([namespace("a")])
    run(core, None, [], [extension("e", "a", gpu="8")])
    assert core.quotas["a", "quota-warden"].spec.hard == {"gpu": "8"}


def test_expired_extension_is_left_out():
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"gpu": "9"})})
    later = datetime(2026, 11, 9, tzinfo=UTC)
    run(core, base(gpu="1"), [], [extension("e", "a", gpu="8")], now=later)
    assert core.quotas["a", "quota-warden"].spec.hard == {"gpu": "1"}


def test_extension_with_a_bad_date_is_left_out():
    core = FakeCore([namespace("a")])
    run(core, base(gpu="1"), [], [extension("e", "a", expires="soon", gpu="8")])
    assert core.quotas["a", "quota-warden"].spec.hard == {"gpu": "1"}


def test_issues_include_inert_and_inactive_extensions():
    core = FakeCore([namespace("a"), namespace("c", managed=False)])
    extensions = [
        extension("a-e", "a", cpu="x"),
        extension("c-e", "c", cpu="y"),
        extension("gone-e", "gone", cpu="z"),
        extension("old-e", "a", expires="2026-01-01", cpu="w"),
    ]
    _, issues = run(core, base(), [], extensions)
    assert sorted(i.source for i in issues) == ["a-e", "c-e", "gone-e", "old-e"]


def test_rejection_is_blamed_on_the_replace_layer_and_the_extension():
    core = FakeCore([namespace("a")])
    results, _ = run(
        core,
        base(bad="1"),
        [override("o", "a", cpu="2")],
        [extension("e", "a", bad="1"), extension("other", "a", cpu="1")],
    )
    assert results["a"].blame == ("default", "e")
