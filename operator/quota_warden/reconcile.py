"""One sweep over all namespaces, without kopf."""

import json
from dataclasses import dataclass, field
from typing import Literal

from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from .config import Config
from .merge import Issue, merge
from .quantity import format_quantity
from .targeting import is_managed

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
    # applied/deleted/rejected describe what a real run would do; nothing was stored
    dry_run: bool = False


def read_object(custom, plural: str, name: str) -> dict | None:
    try:
        return custom.get_cluster_custom_object(GROUP, VERSION, plural, name)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def sweep(core, base: dict | None, config: Config) -> tuple[list[Result], list[Issue]]:
    # the base is the only source and it is cluster-wide, so one merge serves every namespace
    merged, issues = merge(base or {}, [], [])
    hard = {key: format_quantity(value) for key, value in merged.items()}
    results = [
        reconcile_namespace(core, namespace, config, hard)
        for namespace in core.list_namespace().items
    ]
    return results, issues


def reconcile_namespace(core, namespace, config: Config, hard: dict[str, str]) -> Result:
    name = namespace.metadata.name
    # a terminating namespace refuses writes; its quota goes with it
    if (namespace.status and namespace.status.phase) == "Terminating":
        return Result(name, "skipped")
    if not is_managed(namespace.metadata.labels or {}, name, config.spec):
        return Result(name, "skipped")

    existing = _read(core, name, config.resource_quota_name)
    owned = existing is not None and _owned(existing)

    if not hard:
        if not owned:
            return Result(name, "unchanged")
        if config.dry_run:
            return Result(name, "deleted", dry_run=True)
        try:
            core.delete_namespaced_resource_quota(config.resource_quota_name, name)
        except ApiException as exc:
            if exc.status != 404:
                raise
        return Result(name, "deleted")

    # Compares the whole key set: a key someone else added makes every sweep
    # re-apply, harmlessly; foreign keys are not removed yet.
    if owned and _same(existing.spec.hard or {}, hard):
        return Result(name, "unchanged", hard)

    try:
        _apply(core, name, config.resource_quota_name, hard, config.dry_run)
    except ApiException as exc:
        if exc.status == 422:
            return Result(name, "rejected", hard, _message(exc), config.dry_run)
        raise
    return Result(name, "applied", hard, dry_run=config.dry_run)


def _read(core, namespace: str, name: str):
    try:
        return core.read_namespaced_resource_quota(name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return None
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
