"""QuotaWardenConfig and environment settings."""

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from .targeting import SelectorError, validate_selector

DEFAULT_CONFIG_NAME = "default"
DEFAULT_RESYNC_INTERVAL = "1h"

_DURATION = re.compile(r"([1-9]\d*)([smh])")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours"}


class ConfigError(ValueError):
    """The config is unusable; the sweep touches no namespace."""


@dataclass(frozen=True)
class Config:
    spec: dict
    resource_quota_name: str
    dry_run: bool
    foreign_resource_quotas: Literal["report", "delete", "ignore"]
    on_unmanage: Literal["delete", "retain"]


def load(body: dict | None) -> Config:
    """`body` is the QuotaWardenConfig object, None if it does not exist."""
    if body is None:
        raise ConfigError("QuotaWardenConfig not found")
    spec = body.get("spec") or {}
    selector = spec.get("managedNamespaces")
    # is_managed treats unset as "manage nothing"; the config still reports it
    if selector is None:
        raise ConfigError("managedNamespaces is unset; managing nothing")
    try:
        validate_selector(selector)
    except SelectorError as exc:
        raise ConfigError(f"managedNamespaces: {exc}") from exc
    # defaulted by the CRD, so absence means the CRD is not the one this build expects
    try:
        return Config(
            spec,
            spec["resourceQuotaName"],
            spec["dryRun"],
            spec["foreignResourceQuotas"],
            spec["onUnmanage"],
        )
    except KeyError as exc:
        raise ConfigError(f"{exc.args[0]} missing; is the CRD up to date?") from None


def config_name(environ) -> str:
    return environ.get("QUOTA_WARDEN_CONFIG") or DEFAULT_CONFIG_NAME


def resync_interval(environ) -> timedelta:
    """QUOTA_WARDEN_RESYNC_INTERVAL: one positive integer with unit s, m or h."""
    value = environ.get("QUOTA_WARDEN_RESYNC_INTERVAL") or DEFAULT_RESYNC_INTERVAL
    match = _DURATION.fullmatch(value)
    if not match:
        raise ConfigError(f"QUOTA_WARDEN_RESYNC_INTERVAL={value!r}: expected e.g. 30m or 1h")
    return timedelta(**{_UNITS[match[2]]: int(match[1])})
