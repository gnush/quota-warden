"""The merge of base, overrides and extensions. See SPEC.md, Merge algorithm."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .quantity import QuantityError, add, parse


@dataclass(frozen=True)
class Issue:
    kind: Literal["invalid", "duplicate"]
    source: str
    key: str
    message: str


def _resources(source: dict, issues: list[Issue]) -> dict[str, Decimal]:
    name = source.get("metadata", {}).get("name", "")
    parsed = {}
    for key, value in (source.get("spec", {}).get("resources") or {}).items():
        try:
            parsed[key] = parse(value)
        except QuantityError as exc:
            issues.append(Issue("invalid", name, key, str(exc)))
    return parsed


def merge(
    base: dict, overrides: list[dict], extensions: list[dict]
) -> tuple[dict[str, Decimal], list[Issue]]:
    issues: list[Issue] = []
    merged = _resources(base, issues)

    by_key: dict[str, list[tuple[str, Decimal]]] = {}
    for override in overrides:
        name = override.get("metadata", {}).get("name", "")
        for key, value in _resources(override, issues).items():
            by_key.setdefault(key, []).append((name, value))
    for key, values in by_key.items():
        merged[key] = max(value for _, value in values)
        if len(values) > 1:
            names = ", ".join(name for name, _ in values)
            issues.extend(
                Issue("duplicate", name, key, f"{key} also set by {names}; using the maximum")
                for name, _ in values
            )

    for extension in extensions:
        for key, value in _resources(extension, issues).items():
            merged[key] = add(merged.get(key, Decimal(0)), value)

    return merged, issues
