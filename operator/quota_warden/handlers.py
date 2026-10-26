"""kopf wiring: every watch event requests one coalesced sweep.

Run: `kopf run --all-namespaces -m quota_warden.handlers`.
"""

import asyncio
import logging
import os

import kopf
from kubernetes import client
from kubernetes import config as kube_config

from .conditions import Condition, Event, update
from .config import ConfigError, config_name, load, resync_interval
from .reconcile import GROUP, read_object, sweep

logger = logging.getLogger(__name__)


def run_once(core, custom, name: str, reported: dict | None = None) -> list[Event]:
    """One sweep, conditions included. Blocking; returns the Events to post.

    `reported` carries the last dry-run outcome per namespace across sweeps, so a
    pending change is announced once, not on every sweep.
    """
    body = read_object(custom, "quotawardenconfigs", name)
    try:
        config = load(body)
    except ConfigError as exc:
        # fail-safe: existing quotas stay as they are
        logger.error("config %r: %s; touching no namespace", name, exc)
        if body is None:
            return []
        return update(
            custom,
            "quotawardenconfigs",
            body,
            [Condition("Ready", "False", "ConfigError", str(exc))],
        )
    ready = (
        Condition("Ready", "True", "DryRun", "dryRun is set; nothing is written")
        if config.dry_run
        else Condition("Ready", "True", "Loaded")
    )
    events = update(custom, "quotawardenconfigs", body, [ready])

    base = read_object(custom, "quotabases", config.base_name)
    if base is None:
        logger.info("QuotaBase %r not found; removing owned quotas", config.base_name)
    results, issues = sweep(core, base, config)
    for r in results:
        if r.dry_run:
            continue
        if r.action in ("applied", "deleted"):
            logger.info("%s: %s %s", r.namespace, r.action, r.hard)
        for orphan in r.orphans:
            logger.info("%s: deleted orphaned %s", r.namespace, orphan)
    events += _dry_run_events(results, {} if reported is None else reported, config)
    if base is None:
        return events

    invalid = "; ".join(f"{i.key}: {i.message}" for i in issues)
    rejected = "; ".join(f"{r.namespace}: {r.error}" for r in results if r.action == "rejected")
    return events + update(
        custom,
        "quotabases",
        base,
        [
            Condition("Valid", "False", "InvalidValues", invalid)
            if invalid
            else Condition("Valid", "True", "Parsed"),
            Condition("Applied", "False", "Rejected", rejected)
            if rejected
            else Condition("Applied", "True", "Applied"),
        ],
    )


def _dry_run_events(results, reported: dict, config) -> list[Event]:
    events = []
    for r in results:
        if not r.dry_run:
            # a change that recurs after being resolved is reported again
            reported.pop(r.namespace, None)
            continue
        outcome = (r.action, tuple(sorted(r.hard.items())), r.error, r.orphans)
        if reported.get(r.namespace) == outcome:
            continue
        reported[r.namespace] = outcome
        name = config.resource_quota_name
        values = ", ".join(f"{k}={v}" for k, v in sorted(r.hard.items()))
        parts = {
            "applied": [f"would apply {name}: {values}"],
            "deleted": [f"would delete {name}"],
            "rejected": [f"{name} would be rejected: {r.error}"],
        }.get(r.action, [])
        if r.orphans:
            parts.append(f"would delete orphaned {', '.join(r.orphans)}")
        message = "dry run: " + "; ".join(parts)
        logger.info("%s: %s", r.namespace, message)
        namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": r.namespace}}
        kind = "Warning" if r.action == "rejected" else "Normal"
        events.append(Event(namespace, kind, "DryRun", message))
    return events


class Sweeper:
    """Serialises sweeps; requests made during a sweep collapse into one rerun."""

    def __init__(self, run, interval: float, debounce: float = 1.0):
        self._run = run
        self._interval = interval
        self._debounce = debounce
        self._requested = asyncio.Event()
        self._requested.set()
        self._task = None

    def request(self) -> None:
        # Starts the loop on first use, from a handler: kopf.event needs the
        # posting context, which startup handlers lack.
        if self._task is None:
            self._task = asyncio.create_task(self.loop())
        self._requested.set()

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._requested.wait(), self._interval)
                # kopf delivers the initial listing one object at a time; without
                # the wait each one lands after the previous sweep, N sweeps of N
                await asyncio.sleep(self._debounce)
            except TimeoutError:
                pass
            self._requested.clear()
            try:
                await self._run()
            except Exception:
                # one failed sweep must not end the loop; the next request or resync retries
                logger.exception("sweep failed")


@kopf.on.startup()
async def startup(memo: kopf.Memo, **_):
    try:
        interval = resync_interval(os.environ)
    except ConfigError as exc:
        raise kopf.PermanentError(str(exc)) from exc
    try:
        kube_config.load_incluster_config()
    except kube_config.ConfigException:
        kube_config.load_kube_config()
    core, custom, name = client.CoreV1Api(), client.CustomObjectsApi(), config_name(os.environ)

    reported = {}

    async def run():
        for e in await asyncio.to_thread(run_once, core, custom, name, reported):
            kopf.event(e.body, type=e.type, reason=e.reason, message=e.message)

    memo.sweeper = Sweeper(run, interval.total_seconds())


@kopf.on.cleanup()
async def cleanup(memo: kopf.Memo, **_):
    memo.sweeper.stop()


# on.event, not on.create/update: those write progress annotations onto every
# object they handle, Namespaces included.
@kopf.on.event("namespaces")
@kopf.on.event(GROUP, "quotabases")
@kopf.on.event(GROUP, "quotawardenconfigs")
# Async: kopf runs sync handlers in threads, and asyncio.Event is not thread-safe.
# Per-object memos are shallow copies, so state lives in the shared Sweeper.
async def request_sweep(memo: kopf.Memo, **_):
    memo.sweeper.request()
