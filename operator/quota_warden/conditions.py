"""status.conditions on our CRs, written only when they change."""

from dataclasses import dataclass
from datetime import UTC, datetime

from .reconcile import GROUP, VERSION


@dataclass(frozen=True)
class Condition:
    type: str
    status: str  # "True" | "False"
    reason: str
    message: str = ""


@dataclass(frozen=True)
class Event:
    body: dict
    type: str  # "Normal" | "Warning"
    reason: str
    message: str


def merge_conditions(
    current: list[dict], wanted: list[Condition], now: datetime
) -> tuple[list[dict], list[Condition]]:
    """New conditions list and the wanted conditions whose status changed.

    Types not in `wanted` are kept. lastTransitionTime moves only on a status change.
    """
    by_type = {c.get("type"): c for c in current}
    result = [c for c in current if c.get("type") not in {w.type for w in wanted}]
    transitions = []
    for w in wanted:
        old = by_type.get(w.type)
        changed = old is None or old.get("status") != w.status
        if changed:
            transitions.append(w)
        result.append(
            {
                "type": w.type,
                "status": w.status,
                "reason": w.reason,
                "message": w.message,
                "lastTransitionTime": (
                    now.strftime("%Y-%m-%dT%H:%M:%SZ") if changed else old["lastTransitionTime"]
                ),
            }
        )
    return result, transitions


def update(custom, plural: str, body: dict, wanted: list[Condition], now=None) -> list[Event]:
    """Patch status.conditions if anything differs; return Events for the transitions."""
    current = (body.get("status") or {}).get("conditions") or []
    new, transitions = merge_conditions(current, wanted, now or datetime.now(UTC))
    if sorted(new, key=lambda c: c["type"]) != sorted(current, key=lambda c: c.get("type", "")):
        custom.patch_cluster_custom_object_status(
            GROUP,
            VERSION,
            plural,
            body["metadata"]["name"],
            {"status": {"conditions": new}},
            # CRDs reject strategic merge, which the client may pick for a dict body
            _content_type="application/merge-patch+json",
        )
    return [
        Event(
            body,
            "Normal" if c.status == "True" else "Warning",
            c.reason,
            f"{c.type}={c.status}" + (f": {c.message}" if c.message else ""),
        )
        for c in transitions
    ]
