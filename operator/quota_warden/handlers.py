"""kopf wiring: every watch event requests one coalesced sweep.

Run: `kopf run --all-namespaces -m quota_warden.handlers`.
"""

import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime

import kopf
from kubernetes import client
from kubernetes import config as kube_config
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from .conditions import Condition, Event, update
from .config import ConfigError, config_name, load, resync_interval
from .merge import expires_at
from .quantity import format_quantity
from .reconcile import GROUP, expired, list_objects, read_object, reap, sweep

logger = logging.getLogger(__name__)

# kopf's peering, not leader election of our own: it pauses the lower-ranked
# replica's timers, daemons and watch streams, which is all a sweep touches.
PEERING_NAME = "default"
PEERING_OBJECT = "clusterkopfpeerings"
# 32768: kopf's documented $RANDOM range. A collision pauses both replicas --
# a stall, which the next rollout clears -- so the draw need only be unlikely.
PRIORITIES = 32768


@dataclass
class State:
    """Carried across sweeps, so a standing situation is reported once, not every sweep."""

    dry_run: dict[str, tuple] = field(default_factory=dict)  # namespace -> last outcome
    logged: dict[str, str] = field(default_factory=dict)  # topic -> last message
    would_reap: set[str] = field(default_factory=set)  # extensions reported under dryRun
    foreign: set[tuple[str, str]] = field(default_factory=set)  # (namespace, name) reported
    retained: set[tuple[str, str]] = field(default_factory=set)  # (namespace, name) reported

    def log(self, topic: str, level: int, message: str) -> None:
        if self.logged.get(topic) == message:
            logger.debug(message)
            return
        self.logged[topic] = message
        logger.log(level, message)


def run_once(
    core, custom, name: str, state: State | None = None, now: datetime | None = None
) -> list[Event]:
    """One sweep, conditions included. Blocking; returns the Events to post."""
    state = state or State()
    now = now or datetime.now(UTC)
    body = read_object(custom, "quotawardenconfigs", name)
    try:
        config = load(body)
    except ConfigError as exc:
        # fail-safe: existing quotas stay as they are
        state.log("config", logging.ERROR, f"config {name!r}: {exc}; touching no namespace")
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
    state.log(
        "config", logging.INFO, f"config {name!r} loaded" + (", dry run" if config.dry_run else "")
    )

    base = read_object(custom, "quotabases", config.base_name)
    state.log(
        "base",
        logging.INFO,
        f"QuotaBase {config.base_name!r} found"
        if base
        else f"QuotaBase {config.base_name!r} not found",
    )
    overrides = list_objects(custom, "quotaoverrides")
    extensions = list_objects(custom, "quotaextensions")
    results, issues = sweep(core, base, overrides, extensions, config, now)
    # one patch: a second update from the same stale body would undo the first
    events = update(custom, "quotawardenconfigs", body, [ready, _exclusive(results, config)])
    for r in results:
        for quota in r.foreign_deleted:
            logger.info("%s: deleted foreign ResourceQuota %s", r.namespace, quota)
        if r.dry_run:
            continue
        if r.action in ("applied", "deleted"):
            logger.info("%s: %s %s", r.namespace, r.action, r.hard)
        for orphan in r.orphans:
            logger.info("%s: deleted orphaned %s", r.namespace, orphan)
    events += _write_events(results, config)
    events += _foreign_events(results, state.foreign, config)
    events += _unmanage_events(results, state.retained)
    events += _dry_run_events(results, state.dry_run, config)
    if base is not None:
        events += update(
            custom, "quotabases", base, _source_conditions(base, issues, results, config.dry_run)
        )
    skipped = {r.namespace: r.reason for r in results if r.action == "skipped"}
    listed = {r.namespace for r in results}
    for override in overrides:
        wanted = _source_conditions(override, issues, results, config.dry_run)
        wanted = _targeted(override, wanted, skipped, listed, "override")
        events += update(custom, "quotaoverrides", override, wanted)
    reaped, reap_events = _reap(custom, expired(extensions, now), state, config.dry_run)
    events += reap_events
    for extension in extensions:
        # deleted: a status patch would 404 and its events name a gone object
        if extension["metadata"]["name"] in reaped:
            continue
        wanted = _source_conditions(extension, issues, results, config.dry_run)
        wanted = _targeted(extension, wanted, skipped, listed, "extension")
        wanted = _expiry(extension, wanted, now)
        events += update(custom, "quotaextensions", extension, wanted)
    return events


def _reap(
    custom, extensions: list[dict], state: State, dry_run: bool
) -> tuple[set[str], list[Event]]:
    """Names deleted, and the dry-run Events."""
    if not dry_run:
        state.would_reap.clear()
        deleted = reap(custom, extensions)
        for name in deleted:
            logger.info("deleted expired QuotaExtension %s", name)
        return set(deleted), []
    events = []
    names = {e["metadata"]["name"] for e in extensions}
    state.would_reap &= names
    for extension in extensions:
        name = extension["metadata"]["name"]
        if name in state.would_reap:
            continue
        state.would_reap.add(name)
        message = "dry run: would delete the expired extension"
        logger.info("QuotaExtension %s: %s", name, message)
        events.append(Event(extension, "Normal", "DryRun", message))
    return set(), events


def _targeted(source: dict, wanted, skipped, listed, kind: str) -> list[Condition]:
    target = source.get("spec", {}).get("namespace")
    reason = skipped.get(target) or ("" if target in listed else "NamespaceNotFound")
    if not reason:
        return wanted + [Condition("Targeted", "True", "Managed")]
    message = f"namespace {target!r}: {reason}; the {kind} is inert"
    return [c for c in wanted if c.type != "Applied"] + [
        Condition("Targeted", "False", reason, message),
        Condition("Applied", "False", "NotTargeted", message),
    ]


def _expiry(extension: dict, wanted, now) -> list[Condition]:
    """Overrides Valid and Applied for a date the CRD should have rejected, or a past one."""
    value = extension.get("spec", {}).get("expirationDate")
    expires = expires_at(extension)
    if expires is None:
        message = f"expirationDate {value!r} is not a YYYY-MM-DD date; the extension is inert"
        valid = next(c for c in wanted if c.type == "Valid")
        if valid.status == "False":
            message += f"; {valid.message}"
        replaced = [
            Condition("Valid", "False", "InvalidExpirationDate", message),
            Condition("Applied", "False", "InvalidExpirationDate", message),
        ]
    elif now >= expires:
        # seen only while the delete is pending or suppressed by dryRun
        message = f"expired at {expires:%Y-%m-%dT%H:%M:%SZ}"
        replaced = [Condition("Applied", "False", "Expired", message)]
    else:
        return wanted
    types = {c.type for c in replaced}
    return [c for c in wanted if c.type not in types] + replaced


def _source_conditions(source: dict, issues, results, dry_run: bool) -> list[Condition]:
    name = source["metadata"]["name"]
    invalid = "; ".join(
        f"{i.key}: {i.message}" for i in issues if i.kind == "invalid" and i.source == name
    )
    rejected = "; ".join(
        f"{r.namespace}: {r.error}" for r in results if r.action == "rejected" and name in r.blame
    )
    blocked = "; ".join(
        f"{r.namespace}: a foreign ResourceQuota holds the name; nothing is written"
        for r in results
        if r.action == "blocked" and name in r.blame
    )
    return [
        Condition("Valid", "False", "InvalidValues", invalid)
        if invalid
        else Condition("Valid", "True", "Parsed"),
        Condition("Applied", "False", "Rejected", rejected)
        if rejected
        else Condition("Applied", "False", "Blocked", blocked)
        if blocked
        else Condition("Applied", "True", "DryRun", "dryRun is set; nothing is written")
        if dry_run
        else Condition("Applied", "True", "Applied"),
    ]


def _exclusive(results, config) -> Condition:
    if config.foreign_resource_quotas == "ignore":
        return Condition("Exclusive", "True", "Ignored", "foreignResourceQuotas is ignore")
    found = [f"{r.namespace}/{q}" for r in results for q in r.foreign]
    if not found:
        return Condition("Exclusive", "True", "NoForeignQuotas")
    # they bind alongside the owned quota by min()
    verb = "would be deleted" if config.foreign_resource_quotas == "delete" else "bind by min()"
    return Condition("Exclusive", "False", "ForeignQuotas", f"{', '.join(found)}: {verb}")


def _foreign_events(results, reported: set, config) -> list[Event]:
    """Warning on the Namespace once per foreign quota; again only after it has gone."""
    events = []
    present = set()
    for r in results:
        namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": r.namespace}}
        for quota in r.foreign_deleted:
            events.append(
                Event(namespace, "Normal", "ForeignQuotaDeleted", f"deleted ResourceQuota {quota}")
            )
        # under dryRun with delete these are reported by the dry-run Event instead
        if r.dry_run and config.foreign_resource_quotas == "delete":
            continue
        for quota in r.foreign:
            present.add((r.namespace, quota))
            if (r.namespace, quota) in reported:
                continue
            message = f"ResourceQuota {quota} is not managed by quota-warden and binds by min()"
            if quota == config.resource_quota_name:
                message += "; it holds the operator's name, so nothing is written"
            events.append(Event(namespace, "Warning", "ForeignQuota", message))
            logger.warning("%s: %s", r.namespace, message)
    reported.clear()
    reported.update(present)
    return events


def _unmanage_events(results, reported: set) -> list[Event]:
    """Warning on the Namespace: losing management unquotas it, or freezes its quota."""
    events = []
    present = set()
    for r in results:
        namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": r.namespace}}
        cause = f"namespace no longer managed ({r.reason})"
        # under dryRun the dry-run Event reports it
        if r.released and not r.dry_run:
            message = (
                f"{cause}: deleted ResourceQuota {', '.join(r.released)}; "
                "its overrides and extensions are kept"
            )
            logger.warning("%s: %s", r.namespace, message)
            events.append(Event(namespace, "Warning", "Unmanaged", message))
        new = [q for q in r.retained if (r.namespace, q) not in reported]
        present |= {(r.namespace, q) for q in r.retained}
        if new:
            message = f"{cause}: ResourceQuota {', '.join(new)} retained and still binds"
            logger.warning("%s: %s", r.namespace, message)
            events.append(Event(namespace, "Warning", "Unmanaged", message))
    reported.clear()
    reported.update(present)
    return events


def _write_events(results, config) -> list[Event]:
    """On the quota itself, so the namespace owner sees them without cluster-wide rights."""
    events = []
    for r in results:
        if r.dry_run or r.action not in ("applied", "deleted"):
            continue
        quota = {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {
                "name": config.resource_quota_name,
                "namespace": r.namespace,
                "uid": r.uid,
            },
        }
        if r.action == "deleted":
            events.append(Event(quota, "Normal", "Deleted", "no source sets any key"))
        else:
            events.append(Event(quota, "Normal", "Applied", _changes(r.previous, r.hard)))
    return events


def _changes(previous: dict[str, str], hard: dict[str, str]) -> str:
    def plain(value):
        # the live side is in the API server's canonical form (1Gi), ours is plain
        # (1073741824); one notation for both
        return format_quantity(parse_quantity(value))

    parts = []
    for key in sorted(previous.keys() | hard.keys()):
        old, new = previous.get(key), hard.get(key)
        if old is None:
            parts.append(f"{key} {plain(new)} (new)")
        elif new is None:
            parts.append(f"{key} {plain(old)} (removed)")
        elif parse_quantity(old) != parse_quantity(new):
            parts.append(f"{key} {plain(old)}→{plain(new)}")
    # values equal, so the write was for the provenance annotation
    return ", ".join(parts) or "sources changed, values unchanged"


def _dry_run_events(results, reported: dict, config) -> list[Event]:
    events = []
    for r in results:
        if not r.dry_run:
            # a change that recurs after being resolved is reported again
            reported.pop(r.namespace, None)
            continue
        outcome = (
            r.action,
            tuple(sorted(r.hard.items())),
            r.error,
            r.orphans,
            r.foreign,
            r.released,
        )
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
        if r.foreign and config.foreign_resource_quotas == "delete":
            parts.append(f"would delete foreign {', '.join(r.foreign)}")
        if r.released:
            parts.append(
                f"would delete {', '.join(r.released)}: namespace no longer managed ({r.reason})"
            )
        message = "dry run: " + "; ".join(parts)
        logger.info("%s: %s", r.namespace, message)
        namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": r.namespace}}
        kind = "Warning" if r.action == "rejected" else "Normal"
        events.append(Event(namespace, kind, "DryRun", message))
    return events


class Sweeper:
    """Serialises sweeps; requests made during a sweep collapse into one rerun.

    The loop runs inside a kopf daemon, not a bare asyncio task: peering pauses
    pipelined tasks only, and a paused replica that kept resyncing would sweep
    concurrently with the active one.
    """

    def __init__(self, run, interval: float, debounce: float = 1.0):
        self._run = run
        self._interval = interval
        self._debounce = debounce
        self._requested = asyncio.Event()
        self._requested.set()

    def request(self) -> None:
        self._requested.set()

    async def loop(self, stopped) -> None:
        """One long-lived daemon: `stopped` is set on pause, shutdown and object deletion."""
        while not stopped:
            if (
                await _first(
                    None, {"stop": _until(stopped), "done": self._requested.wait()}, self._interval
                )
                == "stop"
            ):
                return
            # kopf delivers the initial listing one object at a time; without the
            # wait each one lands after the previous sweep, N sweeps of N
            if await _first(None, {"stop": _until(stopped)}, self._debounce) == "stop":
                return
            self._requested.clear()
            try:
                await self._run()
            except Exception:
                # one failed sweep must not end the loop; the next request or resync retries
                logger.exception("sweep failed")


async def _first(fallback, coros: dict, timeout: float | None):
    """The key of the first coroutine to finish, `fallback` on timeout.

    Every wait in the loop goes through this: kopf's default cancellation_timeout
    is None, so a daemon still asleep at the interval is force-cancelled and
    logged as "did not exit in time" on every pause and shutdown.
    """
    tasks = {asyncio.create_task(coro): key for key, coro in coros.items()}
    if timeout is not None:
        tasks[asyncio.create_task(asyncio.sleep(timeout))] = None
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
    # a stop wins a tie: the sweep it would have started must not run
    keys = {tasks[task] for task in done}
    return "stop" if "stop" in keys else next(iter(keys), fallback)


async def _until(stopped) -> None:
    # `stopped.wait()` yields kopf's own promise object, not a coroutine
    while not stopped:
        await stopped.wait(0.5)


@kopf.on.startup()
async def startup(settings: kopf.OperatorSettings, memo: kopf.Memo, **_):
    try:
        interval = resync_interval(os.environ)
    except ConfigError as exc:
        raise kopf.PermanentError(str(exc)) from exc
    try:
        kube_config.load_incluster_config()
    except kube_config.ConfigException:
        kube_config.load_kube_config()
    core, custom, name = client.CoreV1Api(), client.CustomObjectsApi(), config_name(os.environ)

    # One line per watch event ("Handler 'request_sweep' succeeded") says nothing:
    # the handler only requests a sweep. --verbose/--debug keep it.
    if logging.getLogger().getEffectiveLevel() > logging.DEBUG:
        logging.getLogger("kopf.objects").setLevel(logging.WARNING)

    state = State()

    async def run():
        for e in await asyncio.to_thread(run_once, core, custom, name, state):
            kopf.event(e.body, type=e.type, reason=e.reason, message=e.message)

    memo.sweeper = Sweeper(run, interval.total_seconds())
    memo.quotas = {}

    await asyncio.to_thread(configure_peering, settings)


def configure_peering(settings) -> None:
    """Our place in kopf's peering, and the object its keep-alives patch.

    `--priority` cannot do this: it is one value in the pod template every
    replica shares, and equal priorities pause *every* replica, the first
    included.
    """
    settings.peering.name = PEERING_NAME
    settings.peering.priority = random.randrange(PRIORITIES)
    # without this a missing peering object silently means standalone: two
    # replicas sweeping, the very thing peering is here to stop
    settings.peering.mandatory = True
    ensure_peering()
    logger.info("peering priority %d", settings.peering.priority)


def ensure_peering() -> None:
    """Create the ClusterKopfPeering if absent.

    `mandatory` pauses every watch stream until it exists, and kopf never
    creates it.
    """
    body = {
        "apiVersion": "kopf.dev/v1",
        "kind": "ClusterKopfPeering",
        "metadata": {"name": PEERING_NAME},
    }
    try:
        client.CustomObjectsApi().create_cluster_custom_object(
            "kopf.dev", "v1", PEERING_OBJECT, body
        )
        logger.info("created ClusterKopfPeering %s", PEERING_NAME)
    except ApiException as exc:
        # already there: whoever deployed it owns it, and deleting it is not ours
        if exc.status != 409:
            raise kopf.PermanentError(f"ClusterKopfPeering {PEERING_NAME}: {exc.reason}") from exc


# on.event, not on.create/update: those write progress annotations onto every
# object they handle, Namespaces included.
def quota_changed(seen: dict[str, str], event_type: str | None, body) -> bool:
    """Whether a ResourceQuota event can change a sweep's outcome.

    status.used moves with every pod in the namespace; only what the sweep reads counts.
    """
    meta = body["metadata"]
    if event_type == "DELETED":
        seen.pop(meta["uid"], None)
        return True
    # default=dict: kopf hands out Mapping views, not dicts
    read = json.dumps(
        [meta.get("labels"), meta.get("annotations"), body.get("spec")],
        sort_keys=True,
        default=dict,
    )
    if seen.get(meta["uid"]) == read:
        return False
    seen[meta["uid"]] = read
    return True


@kopf.on.event("resourcequotas")
async def quota_event(memo: kopf.Memo, body, type, **_):
    if quota_changed(memo.quotas, type, body):
        memo.sweeper.request()


@kopf.on.event("namespaces")
@kopf.on.event(GROUP, "quotabases")
@kopf.on.event(GROUP, "quotaoverrides")
@kopf.on.event(GROUP, "quotaextensions")
@kopf.on.event(GROUP, "quotawardenconfigs")
# Async: kopf runs sync handlers in threads, and asyncio.Event is not thread-safe.
# Per-object memos are shallow copies, so state lives in the shared Sweeper.
async def request_sweep(memo: kopf.Memo, **_):
    memo.sweeper.request()


def _is_configured_config(name: str, **_):
    # strictly sync, so no environ lookup is cached: it is read once per object event
    return name == config_name(os.environ)


# A daemon, not a task beside startup: peering stops daemons on a pause, and a
# paused replica must not keep resyncing -- that is the concurrent sweep that
# peering exists to prevent. One per object, so only the configured one runs.
# requires_finalizer leaves kopf's marker on that object; it is on the config,
# not on every Namespace, and it is what makes the daemon's lifetime that object's.
@kopf.daemon(GROUP, "quotawardenconfigs", when=_is_configured_config)
async def sweep_loop(stopped, memo: kopf.Memo, **_):
    await memo.sweeper.loop(stopped)
