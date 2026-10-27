import asyncio
import logging
from datetime import UTC, datetime

import pytest
from kubernetes.client.exceptions import ApiException
from test_reconcile import OWNED, FakeCore, from_base, namespace, quota

from quota_warden.handlers import State, Sweeper, quota_changed, run_once

SELECTOR = {"matchLabels": {"managed": "yes"}}


class FakeCustom:
    def __init__(self, *objects):
        self.objects = {(o["plural"], o["metadata"]["name"]): o for o in objects}
        self.patches = 0
        self.deleted = []

    def get_cluster_custom_object(self, group, version, plural, name):
        try:
            return self.objects[plural, name]
        except KeyError:
            raise ApiException(status=404) from None

    def list_cluster_custom_object(self, group, version, plural):
        return {"items": [o for (p, _), o in self.objects.items() if p == plural]}

    def patch_cluster_custom_object_status(self, group, version, plural, name, body, **_):
        self.patches += 1
        self.objects[plural, name]["status"] = body["status"]

    def delete_cluster_custom_object(self, group, version, plural, name, body):
        current = self.objects.get((plural, name))
        if current is None:
            raise ApiException(status=404)
        wanted = body.preconditions.resource_version
        if wanted is not None and wanted != current["metadata"].get("resourceVersion"):
            raise ApiException(status=409)
        del self.objects[plural, name]
        self.deleted.append(name)

    def conditions(self, plural, name):
        return {
            c["type"]: (c["status"], c["message"])
            for c in self.objects[plural, name].get("status", {}).get("conditions", [])
        }


def config(**spec):
    defaults = {
        "resourceQuotaName": "quota-warden",
        "dryRun": False,
        "foreignResourceQuotas": "report",
        "onUnmanage": "delete",
    }
    return {
        "plural": "quotawardenconfigs",
        "metadata": {"name": "default"},
        "spec": {**defaults, **spec},
    }


def base(name="default", **resources):
    return {"plural": "quotabases", "metadata": {"name": name}, "spec": {"resources": resources}}


def test_missing_config_touches_nothing():
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"cpu": "1"})})
    assert run_once(core, FakeCustom(base(cpu="2")), "default") == []
    assert core.writes == []


@pytest.mark.parametrize(
    "spec",
    [{}, {"managedNamespaces": {"matchExpressions": [{"key": "k", "operator": "Bogus"}]}}],
)
def test_config_error_sets_the_condition_and_touches_no_namespace(spec):
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"cpu": "1"})})
    custom = FakeCustom(config(**spec))
    events = run_once(core, custom, "default")
    assert core.writes == []
    assert custom.conditions("quotawardenconfigs", "default")["Ready"][0] == "False"
    assert [(e.type, e.reason) for e in events] == [("Warning", "ConfigError")]


def test_fixing_the_config_clears_the_condition():
    custom = FakeCustom(config(), base(cpu="1"))
    core = FakeCore([namespace("a")])
    run_once(core, custom, "default")
    custom.objects["quotawardenconfigs", "default"]["spec"]["managedNamespaces"] = SELECTOR
    events = run_once(core, custom, "default")
    assert custom.conditions("quotawardenconfigs", "default")["Ready"] == ("True", "")
    assert ("Normal", "Loaded") in [(e.type, e.reason) for e in events]
    assert core.quotas["a", "quota-warden"].spec.hard == {"cpu": "1"}


def test_sweep_sets_base_conditions_and_a_second_run_writes_nothing():
    custom = FakeCustom(config(managedNamespaces=SELECTOR), base(cpu="1"))
    core = FakeCore([namespace("a")])
    run_once(core, custom, "default")
    assert custom.conditions("quotabases", "default") == {
        "Valid": ("True", ""),
        "Applied": ("True", ""),
    }
    writes, patches = len(core.writes), custom.patches
    assert run_once(core, custom, "default") == []
    assert (len(core.writes), custom.patches) == (writes, patches)


def test_invalid_and_rejected_name_the_key_and_the_namespace():
    custom = FakeCustom(config(managedNamespaces=SELECTOR), base(cpu="-1", bad="1"))
    run_once(FakeCore([namespace("a")]), custom, "default")
    conditions = custom.conditions("quotabases", "default")
    assert conditions["Valid"][0] == "False" and "cpu" in conditions["Valid"][1]
    assert conditions["Applied"][0] == "False" and conditions["Applied"][1].startswith("a: ")


def override(name, target, **resources):
    return {
        "plural": "quotaoverrides",
        "metadata": {"name": name},
        "spec": {"namespace": target, "resources": resources},
    }


def extension(name, target, expires, **resources):
    return {
        "plural": "quotaextensions",
        "metadata": {"name": name},
        "spec": {"namespace": target, "expirationDate": expires, "resources": resources},
    }


@pytest.mark.parametrize(
    ("now", "gpu"),
    [
        (datetime(2026, 11, 8, 23, 59, 59, tzinfo=UTC), "9"),
        (datetime(2026, 11, 9, tzinfo=UTC), "1"),
    ],
)
def test_extension_counts_until_expiry(now, gpu):
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR),
        base(gpu="1"),
        extension("e", "a", "2026-11-08", gpu="8"),
    )
    core = FakeCore([namespace("a")])
    run_once(core, custom, "default", now=now)
    assert core.quotas["a", "quota-warden"].spec.hard == {"gpu": gpu}


NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def test_extension_gets_conditions_like_an_override():
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR),
        base(cpu="1"),
        extension("e", "a", "2026-11-08", gpu="8", memory="x"),
    )
    run_once(FakeCore([namespace("a")]), custom, "default", now=NOW)
    conditions = custom.conditions("quotaextensions", "e")
    assert conditions["Valid"][0] == "False" and "memory" in conditions["Valid"][1]
    assert conditions["Applied"] == ("True", "")
    assert conditions["Targeted"] == ("True", "")


def test_extension_rejection_is_marked_on_the_extension():
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR), extension("e", "a", "2026-11-08", bad="1")
    )
    run_once(FakeCore([namespace("a")]), custom, "default", now=NOW)
    assert custom.conditions("quotaextensions", "e")["Applied"][0] == "False"


def test_extension_on_an_untargeted_namespace_is_inert():
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR), extension("e", "a", "2026-11-08", gpu="8")
    )
    run_once(FakeCore([namespace("a", managed=False)]), custom, "default", now=NOW)
    conditions = custom.conditions("quotaextensions", "e")
    assert (
        conditions["Targeted"][0] == "False" and "extension is inert" in conditions["Targeted"][1]
    )
    assert conditions["Applied"][0] == "False"


@pytest.mark.parametrize("expires", [None, "2026-11", "next week"])
def test_extension_with_a_bad_date_is_invalid_and_inert(expires):
    e = extension("e", "a", expires, gpu="8", memory="x")
    custom = FakeCustom(config(managedNamespaces=SELECTOR), base(gpu="1"), e)
    core = FakeCore([namespace("a")])
    events = run_once(core, custom, "default", now=NOW)
    conditions = custom.conditions("quotaextensions", "e")
    assert conditions["Valid"][0] == "False"
    assert "expirationDate" in conditions["Valid"][1] and "memory" in conditions["Valid"][1]
    assert conditions["Applied"][0] == "False"
    assert core.quotas["a", "quota-warden"].spec.hard == {"gpu": "1"}
    assert ("Warning", "InvalidExpirationDate") in [(e.type, e.reason) for e in events]


def test_expired_extension_is_deleted_and_the_quota_drops_back():
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR),
        base(gpu="1"),
        extension("old", "a", "2026-11-07", gpu="8"),
        extension("live", "a", "2026-11-08", gpu="2"),
    )
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"gpu": "11"})})
    events = run_once(core, custom, "default", now=NOW)
    assert custom.deleted == ["old"]
    assert core.quotas["a", "quota-warden"].spec.hard == {"gpu": "3"}
    # no status patch, no events for the deleted object
    assert all(e.body["metadata"]["name"] != "old" for e in events)


def test_expiry_is_observed_without_any_change():
    # the resync path: same objects, only the clock moved
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR),
        base(gpu="1"),
        extension("e", "a", "2026-11-08", gpu="8"),
    )
    core = FakeCore([namespace("a")])
    run_once(core, custom, "default", now=NOW)
    assert core.quotas["a", "quota-warden"].spec.hard == {"gpu": "9"}
    run_once(core, custom, "default", now=datetime(2026, 11, 9, tzinfo=UTC))
    assert custom.deleted == ["e"]
    assert core.quotas["a", "quota-warden"].spec.hard == {"gpu": "1"}


def test_expired_extension_on_an_untargeted_namespace_is_deleted():
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR), extension("e", "gone", "2026-11-07", gpu="8")
    )
    run_once(FakeCore([]), custom, "default", now=NOW)
    assert custom.deleted == ["e"]


@pytest.mark.parametrize("expires", [None, "next week"])
def test_extension_with_a_bad_date_is_never_deleted(expires):
    custom = FakeCustom(config(managedNamespaces=SELECTOR), extension("e", "a", expires, gpu="8"))
    run_once(FakeCore([namespace("a")]), custom, "default", now=NOW)
    assert custom.deleted == []


def test_extension_changed_since_the_listing_is_not_deleted():
    e = extension("e", "a", "2026-11-07", gpu="8")
    e["metadata"]["resourceVersion"] = "1"
    custom = FakeCustom(config(managedNamespaces=SELECTOR), e)
    listed = custom.list_cluster_custom_object

    def list_then_renew(group, version, plural):
        items = [
            dict(o, metadata=dict(o["metadata"])) for o in listed(group, version, plural)["items"]
        ]
        if plural == "quotaextensions":
            custom.objects["quotaextensions", "e"]["metadata"]["resourceVersion"] = "2"
        return {"items": items}

    custom.list_cluster_custom_object = list_then_renew
    run_once(FakeCore([namespace("a")]), custom, "default", now=NOW)
    assert custom.deleted == [] and ("quotaextensions", "e") in custom.objects


def test_dry_run_reports_the_expired_extension_once_and_keeps_it():
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR, dryRun=True), extension("e", "a", "2026-11-07", gpu="8")
    )
    core = FakeCore([namespace("a")])
    state = State()
    events = run_once(core, custom, "default", state, now=NOW)
    assert custom.deleted == []
    assert ("e", "DryRun", "dry run: would delete the expired extension") in [
        (e.body["metadata"]["name"], e.reason, e.message) for e in events
    ]
    assert custom.conditions("quotaextensions", "e")["Applied"] == (
        "False",
        "expired at 2026-11-08T00:00:00Z",
    )
    again = run_once(core, custom, "default", state, now=NOW)
    assert [e for e in again if e.body["metadata"]["name"] == "e"] == []


def test_override_issues_stay_off_the_base():
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR), base(cpu="1"), override("o", "a", cpu="x")
    )
    run_once(FakeCore([namespace("a")]), custom, "default")
    assert custom.conditions("quotabases", "default")["Valid"] == ("True", "")


def test_override_gets_its_own_conditions():
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR),
        base(cpu="1"),
        override("o", "a", cpu="2", memory="x"),
    )
    run_once(FakeCore([namespace("a")]), custom, "default")
    o = custom.conditions("quotaoverrides", "o")
    assert o["Valid"][0] == "False" and "memory" in o["Valid"][1]
    assert o["Applied"] == ("True", "")


def test_rejection_marks_only_the_source_of_the_key():
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR), base(cpu="1"), override("o", "a", bad="1")
    )
    run_once(FakeCore([namespace("a")]), custom, "default")
    assert custom.conditions("quotaoverrides", "o")["Applied"][0] == "False"
    assert custom.conditions("quotabases", "default")["Applied"] == ("True", "")


def test_override_without_a_base_gets_conditions():
    custom = FakeCustom(config(managedNamespaces=SELECTOR), override("o", "a", cpu="1"))
    core = FakeCore([namespace("a")])
    run_once(core, custom, "default")
    assert core.quotas["a", "quota-warden"].spec.hard == {"cpu": "1"}
    assert custom.conditions("quotaoverrides", "o")["Applied"] == ("True", "")


@pytest.mark.parametrize(
    "namespaces, target, reason",
    [
        ([namespace("a", managed=False)], "a", "Unmanaged"),
        ([namespace("kube-system")], "kube-system", "Protected"),
        ([], "a", "NamespaceNotFound"),
    ],
)
def test_override_on_an_untargeted_namespace_is_inert(namespaces, target, reason):
    custom = FakeCustom(config(managedNamespaces=SELECTOR), override("o", target, cpu="1"))
    core = FakeCore(namespaces)
    events = run_once(core, custom, "default")
    conditions = custom.conditions("quotaoverrides", "o")
    assert conditions["Targeted"][0] == "False" and reason in conditions["Targeted"][1]
    assert conditions["Applied"][0] == "False"
    assert core.writes == []
    assert ("Warning", reason) in [(e.type, e.reason) for e in events]


def test_targeted_clears_once_the_namespace_is_managed():
    custom = FakeCustom(config(managedNamespaces=SELECTOR), override("o", "a", cpu="1"))
    core = FakeCore([])
    run_once(core, custom, "default")
    core.namespaces.append(namespace("a"))
    run_once(core, custom, "default")
    conditions = custom.conditions("quotaoverrides", "o")
    assert conditions["Targeted"] == ("True", "") and conditions["Applied"] == ("True", "")
    assert core.quotas["a", "quota-warden"].spec.hard == {"cpu": "1"}


def test_the_base_is_whichever_quotabase_exists():
    custom = FakeCustom(config(managedNamespaces=SELECTOR), base(name="anything", cpu="1"))
    core = FakeCore([namespace("a")])
    run_once(core, custom, "default")
    assert core.quotas["a", "quota-warden"].spec.hard == {"cpu": "1"}


def test_two_bases_touch_nothing_and_set_a_condition():
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR),
        base(name="one", cpu="1"),
        base(name="two", cpu="2"),
    )
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"cpu": "1"})})
    events = run_once(core, custom, "default")
    assert core.writes == []
    assert core.quotas["a", "quota-warden"].spec.hard == {"cpu": "1"}
    condition = custom.conditions("quotawardenconfigs", "default")["Ready"]
    assert condition[0] == "False" and "one, two" in condition[1]
    assert [(e.type, e.reason) for e in events] == [("Warning", "BaseError")]


def test_dry_run_reports_each_pending_change_once():
    custom = FakeCustom(config(managedNamespaces=SELECTOR, dryRun=True), base(cpu="1"))
    core = FakeCore([namespace("a")])
    state = State()
    events = run_once(core, custom, "default", state)
    assert custom.conditions("quotawardenconfigs", "default")["Ready"][0] == "True"
    dry = [e for e in events if e.body.get("kind") == "Namespace"]
    assert [(e.body["metadata"]["name"], e.type) for e in dry] == [("a", "Normal")]
    assert dry[0].message == "dry run: would apply quota-warden: cpu=1"
    assert core.writes == [] and core.quotas == {}
    assert run_once(core, custom, "default", state) == []
    custom.objects["quotabases", "default"]["spec"]["resources"] = {"cpu": "2"}
    assert [e.message for e in run_once(core, custom, "default", state)] == [
        "dry run: would apply quota-warden: cpu=2"
    ]


def test_dry_run_event_names_orphans():
    custom = FakeCustom(config(managedNamespaces=SELECTOR, dryRun=True), base(cpu="1"))
    core = FakeCore(
        [namespace("a")],
        {
            ("a", "quota-warden"): quota({"cpu": "1"}, OWNED, from_base(cpu="1")),
            ("a", "old"): quota({"cpu": "1"}),
        },
    )
    events = run_once(core, custom, "default")
    assert [e.message for e in events if e.body.get("kind") == "Namespace"] == [
        "dry run: would delete orphaned old"
    ]


def test_dry_run_sets_applied_reason():
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR, dryRun=True), base(cpu="1"), override("o", "a", cpu="2")
    )
    run_once(FakeCore([namespace("a")]), custom, "default")
    for plural, name in (("quotabases", "default"), ("quotaoverrides", "o")):
        applied = custom.objects[plural, name]["status"]["conditions"]
        assert {c["type"]: c["reason"] for c in applied}["Applied"] == "DryRun"


def test_dry_run_sets_ready_reason():
    custom = FakeCustom(config(managedNamespaces=SELECTOR, dryRun=True))
    run_once(FakeCore([]), custom, "default")
    ready = custom.objects["quotawardenconfigs", "default"]["status"]["conditions"][0]
    assert (ready["status"], ready["reason"]) == ("True", "DryRun")


def test_standing_config_error_logs_once_then_debug(caplog):
    caplog.set_level(logging.DEBUG, logger="quota_warden")
    custom = FakeCustom(config())
    state = State()
    for _ in range(3):
        run_once(FakeCore([]), custom, "default", state)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "managedNamespaces is unset" in errors[0].message
    assert sum(r.levelno == logging.DEBUG for r in caplog.records) == 2

    caplog.clear()
    custom.objects["quotawardenconfigs", "default"]["spec"]["managedNamespaces"] = SELECTOR
    run_once(FakeCore([]), custom, "default", state)
    infos = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert "config 'default' loaded" in infos
    assert "no QuotaBase; no defaults" in infos


class Stopped:
    """kopf's `stopped` kwarg: bool, set once, and `wait()` as kopf returns it.

    kopf hands back an awaitable checker, not a coroutine, so awaiting it in a
    `create_task` raises -- which a plain `asyncio.Event` here would not catch.
    """

    def __init__(self):
        self.set = False
        self._event = asyncio.Event()

    def __bool__(self):
        return self.set

    def stop(self):
        self.set = True
        self._event.set()

    def wait(self, timeout=None):
        event = self._event

        class Promise:
            def __await__(inner):
                return inner._wait().__await__()

            async def _wait(inner):
                # kopf swallows the timeout and returns the checker
                try:
                    await asyncio.wait_for(event.wait(), timeout=timeout)
                except TimeoutError:
                    pass

        return Promise()


def drive(script, debounce=0.0, interval=3600.0, fail_on=None):
    """Run `script(sweeper, runs)` with the loop started as the daemon starts it."""

    async def main():
        runs = []

        async def run():
            runs.append(len(runs))
            if len(runs) == fail_on:
                raise RuntimeError("boom")

        sweeper = Sweeper(run, interval=interval, debounce=debounce)
        stopped = Stopped()
        task = asyncio.create_task(sweeper.loop(stopped))
        try:
            await script(sweeper, runs)
        finally:
            task.cancel()

    asyncio.run(main())


async def settle(rounds=30):
    # each loop iteration spans several awaits, each task start needing a turn
    for _ in range(rounds):
        await asyncio.sleep(0)


def test_the_loop_runs_a_sweep_for_the_request_made_at_startup():
    async def script(sweeper, runs):
        await settle()
        assert runs == [0]

    drive(script)


def test_sweeper_collapses_requests_made_during_a_sweep():
    async def main():
        runs = []
        gate = asyncio.Event()

        async def run():
            runs.append(1)
            await gate.wait()

        sweeper = Sweeper(run, interval=3600, debounce=0)
        task = asyncio.create_task(sweeper.loop(Stopped()))
        await settle()
        for _ in range(3):
            sweeper.request()
        gate.set()
        await settle()
        task.cancel()
        assert runs == [1, 1]

    asyncio.run(main())


def test_a_stopped_loop_sweeps_nothing_more():
    # what a peering pause sets: the daemon's stopper
    async def main():
        runs = []

        async def run():
            runs.append(1)

        sweeper = Sweeper(run, interval=0.01, debounce=0)
        stopped = Stopped()
        task = asyncio.create_task(sweeper.loop(stopped))
        await asyncio.sleep(0.05)
        before = len(runs)
        assert before > 0
        stopped.stop()
        await asyncio.sleep(0.05)
        assert len(runs) == before
        task.cancel()

    asyncio.run(main())


def test_a_stop_wakes_the_loop_out_of_a_long_interval():
    # otherwise kopf force-cancels the daemon, logging "did not exit in time"
    async def main():
        runs = []

        async def run():
            runs.append(1)

        sweeper = Sweeper(run, interval=3600, debounce=0)
        stopped = Stopped()
        task = asyncio.create_task(sweeper.loop(stopped))
        await settle()
        stopped.stop()
        # returns on its own well inside the flush: no cancellation needed
        await asyncio.wait_for(task, timeout=1)
        assert len(runs) == 1  # the startup request; none after the stop

    asyncio.run(main())


def test_sweeper_survives_a_failed_sweep():
    async def script(sweeper, runs):
        sweeper.request()
        await settle()
        sweeper.request()
        await settle()
        assert runs == [0, 1]

    drive(script, fail_on=1)


def test_sweeper_resyncs_without_requests():
    async def script(sweeper, runs):
        await asyncio.sleep(0.1)
        assert len(runs) > 2

    drive(script, interval=0.01)


def test_sweeper_debounce_merges_a_burst_that_arrives_after_the_first_request():
    async def script(sweeper, runs):
        for _ in range(10):
            sweeper.request()
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.2)
        assert runs == [0]

    drive(script, debounce=0.05)


def quota_events(events):
    return [
        (e.body["metadata"]["namespace"], e.body["metadata"]["uid"], e.reason, e.message)
        for e in events
        if e.body.get("kind") == "ResourceQuota"
    ]


def test_writes_are_evented_on_the_quota_with_what_changed():
    live = {("a", "quota-warden"): quota({"cpu": "1", "memory": "1Gi", "pods": "1"})}
    core = FakeCore([namespace("a")], live)
    custom = FakeCustom(config(managedNamespaces=SELECTOR), base(cpu="2", memory="1024Mi", gpu="1"))
    events = run_once(core, custom, "default")
    assert quota_events(events) == [
        ("a", "uid-1", "Applied", "cpu 1→2, gpu 1 (new), pods 1 (removed)")
    ]
    assert quota_events(run_once(core, custom, "default")) == []


def test_a_provenance_only_write_says_so():
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"cpu": "1"})})
    custom = FakeCustom(config(managedNamespaces=SELECTOR), base(cpu="1"))
    events = run_once(core, custom, "default")
    assert quota_events(events) == [("a", "uid-1", "Applied", "sources changed, values unchanged")]


def test_deleting_the_quota_is_evented():
    core = FakeCore([namespace("a")], {("a", "quota-warden"): quota({"cpu": "1"})})
    events = run_once(core, FakeCustom(config(managedNamespaces=SELECTOR)), "default")
    assert quota_events(events) == [("a", "uid-1", "Deleted", "no source sets any key")]


def test_dry_run_posts_no_quota_events():
    custom = FakeCustom(config(managedNamespaces=SELECTOR, dryRun=True), base(cpu="1"))
    assert quota_events(run_once(FakeCore([namespace("a")]), custom, "default")) == []


def namespace_events(events, reason):
    return [
        (e.body["metadata"]["name"], e.type, e.message)
        for e in events
        if e.body.get("kind") == "Namespace" and e.reason == reason
    ]


def test_foreign_quota_is_a_condition_and_one_warning():
    core = FakeCore([namespace("a")], {("a", "legacy"): quota({"cpu": "9"}, labels={})})
    custom = FakeCustom(config(managedNamespaces=SELECTOR), base(cpu="1"))
    state = State()
    events = run_once(core, custom, "default", state)
    assert custom.conditions("quotawardenconfigs", "default")["Exclusive"] == (
        "False",
        "a/legacy: bind by min()",
    )
    assert [(n, t) for n, t, _ in namespace_events(events, "ForeignQuota")] == [("a", "Warning")]
    assert namespace_events(run_once(core, custom, "default", state), "ForeignQuota") == []

    del core.quotas["a", "legacy"]
    run_once(core, custom, "default", state)
    assert custom.conditions("quotawardenconfigs", "default")["Exclusive"][0] == "True"
    core.quotas["a", "legacy"] = quota({"cpu": "9"}, labels={})
    assert len(namespace_events(run_once(core, custom, "default", state), "ForeignQuota")) == 1


def test_blocked_namespace_marks_its_sources():
    live = {("a", "quota-warden"): quota({"cpu": "9"}, labels={})}
    custom = FakeCustom(config(managedNamespaces=SELECTOR), base(cpu="1"))
    events = run_once(FakeCore([namespace("a")], live), custom, "default")
    applied = custom.conditions("quotabases", "default")["Applied"]
    assert applied[0] == "False" and applied[1].startswith("a: a foreign ResourceQuota")
    assert "nothing is written" in namespace_events(events, "ForeignQuota")[0][2]


def test_deleted_foreign_quota_is_evented():
    core = FakeCore([namespace("a")], {("a", "legacy"): quota({"cpu": "9"}, labels={})})
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR, foreignResourceQuotas="delete"), base(cpu="1")
    )
    events = run_once(core, custom, "default")
    assert namespace_events(events, "ForeignQuotaDeleted") == [
        ("a", "Normal", "deleted ResourceQuota legacy")
    ]
    assert namespace_events(events, "ForeignQuota") == []
    assert custom.conditions("quotawardenconfigs", "default")["Exclusive"][0] == "True"


def test_dry_run_delete_reports_what_it_would_delete():
    core = FakeCore([namespace("a")], {("a", "legacy"): quota({"cpu": "9"}, labels={})})
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR, foreignResourceQuotas="delete", dryRun=True),
        base(cpu="1"),
    )
    events = run_once(core, custom, "default")
    assert "would delete foreign legacy" in namespace_events(events, "DryRun")[0][2]
    assert namespace_events(events, "ForeignQuota") == []
    assert custom.conditions("quotawardenconfigs", "default")["Exclusive"] == (
        "False",
        "a/legacy: would be deleted",
    )
    assert ("a", "legacy") in core.quotas


def test_ignore_mode_reports_nothing():
    core = FakeCore([namespace("a")], {("a", "legacy"): quota({"cpu": "9"}, labels={})})
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR, foreignResourceQuotas="ignore"), base(cpu="1")
    )
    events = run_once(core, custom, "default")
    assert namespace_events(events, "ForeignQuota") == []
    assert custom.conditions("quotawardenconfigs", "default")["Exclusive"][0] == "True"


def rq_body(uid="u", used="1", hard="2", labels=None):
    return {
        "metadata": {"uid": uid, "labels": labels},
        "spec": {"hard": {"pods": hard}},
        "status": {"used": {"pods": used}},
    }


def test_quota_events_count_only_what_a_sweep_reads():
    seen = {}
    assert quota_changed(seen, None, rq_body())
    assert not quota_changed(seen, "MODIFIED", rq_body(used="2"))
    assert quota_changed(seen, "MODIFIED", rq_body(hard="3"))
    assert quota_changed(seen, "MODIFIED", rq_body(hard="3", labels={"x": "y"}))
    assert quota_changed(seen, "DELETED", rq_body(hard="3", labels={"x": "y"}))
    assert seen == {}
    assert quota_changed(seen, "ADDED", rq_body(uid="v"))


def test_unmanage_deletes_with_a_warning_and_keeps_the_sources():
    core = FakeCore([namespace("a", managed=False)], {("a", "quota-warden"): quota({"cpu": "2"})})
    custom = FakeCustom(
        config(managedNamespaces=SELECTOR), base(cpu="1"), override("a", "a", cpu="2")
    )
    events = run_once(core, custom, "default")
    assert namespace_events(events, "Unmanaged") == [
        (
            "a",
            "Warning",
            "namespace no longer managed (Unmanaged): deleted ResourceQuota quota-warden; "
            "its overrides and extensions are kept",
        )
    ]
    assert core.quotas == {}
    assert ("quotaoverrides", "a") in custom.objects
    assert namespace_events(run_once(core, custom, "default"), "Unmanaged") == []


def test_unmanage_retain_warns_once():
    core = FakeCore([namespace("a", managed=False)], {("a", "quota-warden"): quota({"cpu": "2"})})
    custom = FakeCustom(config(managedNamespaces=SELECTOR, onUnmanage="retain"), base(cpu="1"))
    state = State()
    [(_, kind, message)] = namespace_events(run_once(core, custom, "default", state), "Unmanaged")
    assert kind == "Warning" and message.endswith("quota-warden retained and still binds")
    assert namespace_events(run_once(core, custom, "default", state), "Unmanaged") == []
    assert ("a", "quota-warden") in core.quotas


def test_unmanage_under_dry_run_is_reported_not_done():
    core = FakeCore([namespace("a", managed=False)], {("a", "quota-warden"): quota({"cpu": "2"})})
    custom = FakeCustom(config(managedNamespaces=SELECTOR, dryRun=True), base(cpu="1"))
    events = run_once(core, custom, "default")
    assert namespace_events(events, "Unmanaged") == []
    assert namespace_events(events, "DryRun") == [
        (
            "a",
            "Normal",
            "dry run: would delete quota-warden: namespace no longer managed (Unmanaged)",
        )
    ]
    assert ("a", "quota-warden") in core.quotas


def configure_peering(settings, monkeypatch, priority=7, status=None):
    """What startup does to the peering settings, without the API clients."""
    import quota_warden.handlers as handlers

    created = []

    class Coordination:
        def create_cluster_custom_object(self, group, version, plural, body):
            created.append((group, version, plural, body))
            if status is not None:
                raise ApiException(status=status)

    monkeypatch.setattr(handlers.client, "CustomObjectsApi", lambda: Coordination())
    monkeypatch.setattr(
        handlers, "random", type("R", (), {"randrange": staticmethod(lambda n: priority)})
    )
    handlers.configure_peering(settings)
    return created


class Peering:
    def __init__(self):
        self.name = None
        self.priority = None
        self.mandatory = None


class Settings:
    def __init__(self):
        self.peering = Peering()


def test_peering_is_mandatory_and_distinct_per_replica(monkeypatch):
    settings = Settings()
    created = configure_peering(settings, monkeypatch)

    # without it a missing peering object is silently standalone: two replicas sweep
    assert settings.peering.mandatory is True
    assert settings.peering.name == "default"
    assert settings.peering.priority == 7
    group, version, plural, body = created[0]
    assert (group, version, plural) == ("kopf.dev", "v1", "clusterkopfpeerings")
    assert body["metadata"]["name"] == "default"


def test_priorities_are_drawn_from_kopfs_documented_range(monkeypatch):
    import quota_warden.handlers as handlers

    assert handlers.PRIORITIES == 32768


def test_an_existing_peering_object_is_left_alone(monkeypatch):
    # a 409 is another owner's object, not a failure
    configure_peering(Settings(), monkeypatch, status=409)


def test_a_missing_peering_crd_is_reported(monkeypatch):
    import kopf

    import quota_warden.handlers as handlers

    with pytest.raises(kopf.PermanentError):
        configure_peering(Settings(), monkeypatch, status=404)
    assert handlers.PEERING_NAME == "default"
