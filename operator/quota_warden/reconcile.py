"""One sweep over all namespaces, without kopf."""

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from kubernetes.client import V1DeleteOptions, V1Preconditions
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from .config import Config
from .merge import Issue, expires_at, is_active, merge, provenance
from .quantity import format_quantity
from .targeting import is_managed, is_protected

GROUP = "quota-warden.gnush.github.io"
VERSION = "v1alpha1"
FIELD_MANAGER = "quota-warden"
# Deletion is limited to quotas carrying this label, so an unrelated quota that
# happens to share resourceQuotaName is never removed.
MANAGED_BY = ("app.kubernetes.io/managed-by", "quota-warden")
PROVENANCE = f"{GROUP}/provenance"


@dataclass(frozen=True)
class Result:
    namespace: str
    # blocked: a foreign quota holds resourceQuotaName
    action: Literal["applied", "deleted", "unchanged", "skipped", "rejected", "blocked"]
    hard: dict[str, str] = field(default_factory=dict)
    error: str = ""
    # labeled quotas under another name, e.g. left behind by a resourceQuotaName change
    orphans: tuple[str, ...] = ()
    # action and orphans describe what a real run would do; nothing was stored
    dry_run: bool = False
    # sources a rejection is attributed to
    blame: tuple[str, ...] = ()
    # why a namespace was skipped: Unmanaged, Protected or Terminating
    reason: str = ""
    # spec.hard of the owned quota before an applied or deleted write
    previous: dict[str, str] = field(default_factory=dict)
    # of the quota written or deleted; Events need it to show in kubectl describe
    uid: str = ""
    # unlabeled quotas left in place: reported, or under dryRun would be deleted
    foreign: tuple[str, ...] = ()
    foreign_deleted: tuple[str, ...] = ()
    # owned quotas in a namespace no longer managed: deleted (under dryRun: would be), or kept
    released: tuple[str, ...] = ()
    retained: tuple[str, ...] = ()


def read_object(custom, plural: str, name: str) -> dict | None:
    try:
        return custom.get_cluster_custom_object(GROUP, VERSION, plural, name)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def list_objects(custom, plural: str) -> list[dict]:
    return custom.list_cluster_custom_object(GROUP, VERSION, plural)["items"]


def base_of(bases: list[dict]) -> tuple[dict | None, str]:
    """The one QuotaBase, or None and why. A singleton, so two is a configuration error."""
    if len(bases) > 1:
        names = ", ".join(sorted(b["metadata"]["name"] for b in bases))
        return None, f"{len(bases)} QuotaBase objects ({names}); there must be at most one"
    return (bases[0], "") if bases else (None, "")


def sweep(
    core,
    base: dict | None,
    overrides: list[dict],
    extensions: list[dict],
    config: Config,
    now: datetime,
) -> tuple[list[Result], list[Issue]]:
    # the CRD makes an override's name its namespace, so there is at most one per namespace
    by_override = {o.get("spec", {}).get("namespace"): o for o in overrides}
    by_extensions: dict[str, list[dict]] = {}
    for extension in extensions:
        if is_active(extension, now):
            target = extension.get("spec", {}).get("namespace")
            by_extensions.setdefault(target, []).append(extension)

    results = []
    # dict, not list: the base is parsed once per namespace and would repeat its issues
    issues: dict[Issue, None] = dict.fromkeys(merge(base or {}, None, [])[1])
    # one call instead of one per unmanaged namespace, of which there may be hundreds
    key, value = MANAGED_BY
    labelled = core.list_resource_quota_for_all_namespaces(label_selector=f"{key}={value}")
    owned: dict[str, list[str]] = {}
    for quota in labelled.items:
        owned.setdefault(quota.metadata.namespace, []).append(quota.metadata.name)
    for namespace in core.list_namespace().items:
        ns_override = by_override.get(namespace.metadata.name)
        ns_extensions = by_extensions.get(namespace.metadata.name, [])
        merged, found = merge(base or {}, ns_override, ns_extensions)
        hard = {key: format_quantity(value) for key, value in merged.items()}
        sources = provenance(base or {}, ns_override, ns_extensions)
        # fixed serialisation: an unchanged sweep must compare equal and write nothing
        annotation = json.dumps(sources, sort_keys=True, separators=(",", ":"))
        result = reconcile_namespace(
            core, namespace, config, hard, annotation, tuple(owned.get(namespace.metadata.name, ()))
        )
        if result.action in ("rejected", "blocked"):
            # a blocked result has no error naming a key, so every contributor is blamed
            blame = _blame(result.error, base, ns_override, ns_extensions)
            result = replace(result, blame=blame)
        issues.update(dict.fromkeys(found))
        results.append(result)
    # inert, but their values are still checked
    listed = {r.namespace for r in results}
    for target in by_override.keys() - listed:
        issues.update(dict.fromkeys(merge({}, by_override[target], [])[1]))
    for target in by_extensions.keys() - listed:
        issues.update(dict.fromkeys(merge({}, None, by_extensions[target])[1]))
    inactive = [e for e in extensions if not is_active(e, now)]
    issues.update(dict.fromkeys(merge({}, None, inactive)[1]))
    return results, list(issues)


def expired(extensions: list[dict], now: datetime) -> list[dict]:
    """Past their expirationDate; one without a valid date never expires."""
    return [e for e in extensions if (t := expires_at(e)) is not None and now >= t]


def reap(custom, extensions: list[dict]) -> list[str]:
    """Delete the given extensions; returns the names actually deleted."""
    deleted = []
    for extension in extensions:
        meta = extension["metadata"]
        # an extension edited since the listing, e.g. renewed, is left to the next sweep
        options = V1DeleteOptions(
            preconditions=V1Preconditions(
                uid=meta.get("uid"), resource_version=meta.get("resourceVersion")
            )
        )
        try:
            custom.delete_cluster_custom_object(
                GROUP, VERSION, "quotaextensions", meta["name"], body=options
            )
        except ApiException as exc:
            if exc.status in (404, 409):
                continue
            raise
        deleted.append(meta["name"])
    return deleted


# the API server names each bad key as spec.hard[<key>]
_REJECTED_KEY = re.compile(r"spec\.hard\[([^\]]+)\]")


def _blame(
    error: str, base: dict | None, override: dict | None, extensions: list[dict]
) -> tuple[str, ...]:
    """The sources that set the rejected keys; every contributor if none can be named."""

    def name(source):
        return source.get("metadata", {}).get("name", "")

    def keys(source):
        return (source.get("spec", {}).get("resources") or {}).keys()

    blamed = []
    for key in dict.fromkeys(_REJECTED_KEY.findall(error)):
        layer = override if override is not None and key in keys(override) else base
        setters = [name(layer)] if layer is not None and key in keys(layer) else []
        # extensions add on top of whichever replace layer set the key
        blamed += setters + [name(e) for e in extensions if key in keys(e)]
    if not blamed:
        blamed = [name(s) for s in [base, override, *extensions] if s is not None]
    return tuple(dict.fromkeys(blamed))


def reconcile_namespace(
    core,
    namespace,
    config: Config,
    hard: dict[str, str],
    annotation: str = "{}",
    owned: tuple[str, ...] = (),
) -> Result:
    name = namespace.metadata.name
    # a terminating namespace refuses writes; its quota goes with it
    if (namespace.status and namespace.status.phase) == "Terminating":
        return Result(name, "skipped", reason="Terminating")
    if not is_managed(namespace.metadata.labels or {}, name, config.spec):
        reason = "Protected" if is_protected(name, config.spec) else "Unmanaged"
        return _unmanage(core, Result(name, "skipped", reason=reason), config, owned)

    quotas = core.list_namespaced_resource_quota(name).items
    existing = next((q for q in quotas if q.metadata.name == config.resource_quota_name), None)
    orphans = tuple(
        q.metadata.name
        for q in quotas
        if q.metadata.name != config.resource_quota_name and _owned(q)
    )
    # unlabeled, so written by someone else; one under our name is foreign too
    foreign = tuple(q.metadata.name for q in quotas if not _owned(q))
    deleting = config.foreign_resource_quotas == "delete"
    if not config.dry_run:
        for orphan in orphans:
            _delete(core, name, orphan)
        if deleting:
            for quota in foreign:
                _delete(core, name, quota)
    squatted = existing is not None and not _owned(existing)
    if squatted and deleting:
        # deleted, or under dryRun the apply a real run would make after deleting it
        existing = None
    if squatted and not deleting and hard:
        # taking it over would delete its values, which report and ignore rule out
        result = Result(name, "blocked", hard)
    else:
        result = _reconcile_owned(core, name, existing, config, hard, annotation)

    if deleting and not config.dry_run:
        result = replace(result, foreign_deleted=foreign)
    elif config.foreign_resource_quotas != "ignore":
        # under dryRun with delete: what a real run would delete
        result = replace(result, foreign=foreign)
    if orphans or (deleting and foreign):
        result = replace(result, orphans=orphans, dry_run=result.dry_run or config.dry_run)
    return result


def _unmanage(core, result: Result, config: Config, owned: tuple[str, ...]) -> Result:
    """Owned quotas left in a namespace that stopped matching; the sources stay untouched."""
    if not owned:
        return result
    if config.on_unmanage == "retain":
        return replace(result, retained=owned)
    if not config.dry_run:
        for quota in owned:
            _delete(core, result.namespace, quota)
    return replace(result, released=owned, dry_run=config.dry_run)


def _reconcile_owned(
    core, name: str, existing, config: Config, hard: dict[str, str], annotation: str
) -> Result:
    owned = existing is not None and _owned(existing)
    live = (existing.spec.hard or {}) if owned else {}

    if not hard:
        if not owned:
            return Result(name, "unchanged")
        if config.dry_run:
            return Result(name, "deleted", dry_run=True)
        _delete(core, name, config.resource_quota_name)
        return Result(name, "deleted", previous=live, uid=existing.metadata.uid)

    live_annotation = (existing.metadata.annotations or {}).get(PROVENANCE) if owned else None
    if owned and _same(live, hard) and live_annotation == annotation:
        return Result(name, "unchanged", hard)

    try:
        # apply cannot remove a key another field manager owns, e.g. one added by kubectl edit
        if live.keys() - hard.keys():
            written = _replace(core, name, existing, hard, annotation, config.dry_run)
        else:
            written = _apply(
                core, name, config.resource_quota_name, hard, annotation, config.dry_run
            )
    except ApiException as exc:
        if exc.status == 422:
            return Result(name, "rejected", hard, _message(exc), dry_run=config.dry_run)
        # edited since it was listed; the next sweep sees the new state
        if exc.status == 409:
            return Result(name, "unchanged", hard)
        raise
    if config.dry_run:
        return Result(name, "applied", hard, dry_run=True)
    return Result(name, "applied", hard, previous=live, uid=written.metadata.uid)


def _delete(core, namespace: str, name: str) -> None:
    try:
        core.delete_namespaced_resource_quota(name, namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise


def _owned(quota) -> bool:
    key, value = MANAGED_BY
    return (quota.metadata.labels or {}).get(key) == value


def _same(live: dict[str, str], hard: dict[str, str]) -> bool:
    # by value: the API server stores 1024Mi as 1Gi
    return live.keys() == hard.keys() and all(
        parse_quantity(live[key]) == parse_quantity(hard[key]) for key in hard
    )


def _apply(core, namespace: str, name: str, hard: dict[str, str], annotation: str, dry_run: bool):
    key, value = MANAGED_BY
    body = {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {key: value},
            "annotations": {PROVENANCE: annotation},
        },
        "spec": {"hard": hard},
    }
    # server-side dry run: validated and admitted in full, so rejections still show
    options = {"dry_run": "All"} if dry_run else {}
    return core.patch_namespaced_resource_quota(
        name,
        namespace,
        body,
        field_manager=FIELD_MANAGER,
        force=True,
        _content_type="application/apply-patch+yaml",
        **options,
    )


def _replace(core, namespace: str, existing, hard: dict[str, str], annotation: str, dry_run: bool):
    """PUT `hard` over the listed object; its resourceVersion makes a concurrent edit a 409."""
    meta = existing.metadata
    body = {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {
            "name": meta.name,
            "namespace": namespace,
            "labels": meta.labels,
            "annotations": {**(meta.annotations or {}), PROVENANCE: annotation},
            "resourceVersion": meta.resource_version,
        },
        "spec": {"hard": hard},
    }
    options = {"dry_run": "All"} if dry_run else {}
    return core.replace_namespaced_resource_quota(
        meta.name,
        namespace,
        body,
        field_manager=FIELD_MANAGER,
        **options,
    )


def _message(exc: ApiException) -> str:
    try:
        return json.loads(exc.body)["message"]
    except (TypeError, ValueError, KeyError):
        return str(exc.reason)
