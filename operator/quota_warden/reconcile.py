"""One sweep over all namespaces, without kopf."""

import json
import re
from dataclasses import dataclass, field, replace
from typing import Literal

from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from .config import Config
from .merge import Issue, merge
from .quantity import format_quantity
from .targeting import is_managed, is_protected

GROUP = "quota-warden.gnush.github.io"
VERSION = "v1alpha1"
FIELD_MANAGER = "quota-warden"
# Deletion is limited to quotas carrying this label, so an unrelated quota that
# happens to share resourceQuotaName is never removed.
MANAGED_BY = ("app.kubernetes.io/managed-by", "quota-warden")


@dataclass(frozen=True)
class Result:
    namespace: str
    action: Literal["applied", "deleted", "unchanged", "skipped", "rejected"]
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


def read_object(custom, plural: str, name: str) -> dict | None:
    try:
        return custom.get_cluster_custom_object(GROUP, VERSION, plural, name)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def list_objects(custom, plural: str) -> list[dict]:
    return custom.list_cluster_custom_object(GROUP, VERSION, plural)["items"]


def sweep(
    core, base: dict | None, overrides: list[dict], config: Config
) -> tuple[list[Result], list[Issue]]:
    by_namespace: dict[str, list[dict]] = {}
    for override in overrides:
        by_namespace.setdefault(override.get("spec", {}).get("namespace"), []).append(override)

    results = []
    # dict, not list: the base is parsed once per namespace and would repeat its issues
    issues: dict[Issue, None] = dict.fromkeys(merge(base or {}, [], [])[1])
    for namespace in core.list_namespace().items:
        sources = by_namespace.get(namespace.metadata.name, [])
        merged, found = merge(base or {}, sources, [])
        hard = {key: format_quantity(value) for key, value in merged.items()}
        result = reconcile_namespace(core, namespace, config, hard)
        if result.action == "rejected":
            result = replace(result, blame=_blame(result.error, base, sources))
        issues.update(dict.fromkeys(found))
        results.append(result)
    # inert, but their values are still checked
    listed = {r.namespace for r in results}
    for target, sources in by_namespace.items():
        if target not in listed:
            issues.update(dict.fromkeys(merge({}, sources, [])[1]))
    return results, list(issues)


# the API server names each bad key as spec.hard[<key>]
_REJECTED_KEY = re.compile(r"spec\.hard\[([^\]]+)\]")


def _blame(error: str, base: dict | None, overrides: list[dict]) -> tuple[str, ...]:
    """The sources that set the rejected keys; every contributor if none can be named."""

    def name(source):
        return source.get("metadata", {}).get("name", "")

    def keys(source):
        return (source.get("spec", {}).get("resources") or {}).keys()

    blamed = []
    for key in dict.fromkeys(_REJECTED_KEY.findall(error)):
        setters = [name(o) for o in overrides if key in keys(o)]
        if not setters and base is not None and key in keys(base):
            setters = [name(base)]
        blamed += setters
    if not blamed:
        blamed = [name(s) for s in ([base] if base else []) + overrides]
    return tuple(dict.fromkeys(blamed))


def reconcile_namespace(core, namespace, config: Config, hard: dict[str, str]) -> Result:
    name = namespace.metadata.name
    # a terminating namespace refuses writes; its quota goes with it
    if (namespace.status and namespace.status.phase) == "Terminating":
        return Result(name, "skipped", reason="Terminating")
    if not is_managed(namespace.metadata.labels or {}, name, config.spec):
        reason = "Protected" if is_protected(name, config.spec) else "Unmanaged"
        return Result(name, "skipped", reason=reason)

    quotas = core.list_namespaced_resource_quota(name).items
    existing = next((q for q in quotas if q.metadata.name == config.resource_quota_name), None)
    orphans = tuple(
        q.metadata.name
        for q in quotas
        if q.metadata.name != config.resource_quota_name and _owned(q)
    )
    if not config.dry_run:
        for orphan in orphans:
            _delete(core, name, orphan)
    result = _reconcile_owned(core, name, existing, config, hard)
    if not orphans:
        return result
    return replace(result, orphans=orphans, dry_run=result.dry_run or config.dry_run)


def _reconcile_owned(core, name: str, existing, config: Config, hard: dict[str, str]) -> Result:
    owned = existing is not None and _owned(existing)

    if not hard:
        if not owned:
            return Result(name, "unchanged")
        if config.dry_run:
            return Result(name, "deleted", dry_run=True)
        _delete(core, name, config.resource_quota_name)
        return Result(name, "deleted")

    # Compares the whole key set: a key someone else added makes every sweep
    # re-apply, harmlessly; foreign keys are not removed yet.
    if owned and _same(existing.spec.hard or {}, hard):
        return Result(name, "unchanged", hard)

    try:
        _apply(core, name, config.resource_quota_name, hard, config.dry_run)
    except ApiException as exc:
        if exc.status == 422:
            return Result(name, "rejected", hard, _message(exc), dry_run=config.dry_run)
        raise
    return Result(name, "applied", hard, dry_run=config.dry_run)


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


def _apply(core, namespace: str, name: str, hard: dict[str, str], dry_run: bool) -> None:
    key, value = MANAGED_BY
    body = {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": name, "namespace": namespace, "labels": {key: value}},
        "spec": {"hard": hard},
    }
    # server-side dry run: validated and admitted in full, so rejections still show
    options = {"dry_run": "All"} if dry_run else {}
    core.patch_namespaced_resource_quota(
        name,
        namespace,
        body,
        field_manager=FIELD_MANAGER,
        force=True,
        _content_type="application/apply-patch+yaml",
        **options,
    )


def _message(exc: ApiException) -> str:
    try:
        return json.loads(exc.body)["message"]
    except (TypeError, ValueError, KeyError):
        return str(exc.reason)
