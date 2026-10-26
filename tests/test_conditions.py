from datetime import UTC, datetime

from quota_warden.conditions import Condition, merge_conditions, update

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 1, 2, tzinfo=UTC)


def stored(type_, status, reason="R", message="", at="2026-01-01T00:00:00Z"):
    return {
        "type": type_,
        "status": status,
        "reason": reason,
        "message": message,
        "lastTransitionTime": at,
    }


class FakeCustom:
    def __init__(self):
        self.patches = []

    def patch_cluster_custom_object_status(self, group, version, plural, name, body, **kwargs):
        assert kwargs == {"_content_type": "application/merge-patch+json"}
        self.patches.append((plural, name, body))


def body(conditions=None):
    status = {"conditions": conditions} if conditions is not None else {}
    return {"metadata": {"name": "default"}, "status": status}


def test_first_condition_is_a_transition():
    new, transitions = merge_conditions([], [Condition("Ready", "True", "Loaded")], T0)
    assert new == [stored("Ready", "True", "Loaded")]
    assert [t.type for t in transitions] == ["Ready"]


def test_message_change_keeps_the_transition_time():
    current = [stored("Ready", "False", "ConfigError", "old")]
    new, transitions = merge_conditions(
        current, [Condition("Ready", "False", "ConfigError", "new")], T1
    )
    assert new == [stored("Ready", "False", "ConfigError", "new")]
    assert transitions == []


def test_status_change_moves_the_transition_time():
    new, transitions = merge_conditions(
        [stored("Ready", "False")], [Condition("Ready", "True", "Loaded")], T1
    )
    assert new[0]["lastTransitionTime"] == "2026-01-02T00:00:00Z"
    assert len(transitions) == 1


def test_other_types_are_kept():
    new, _ = merge_conditions([stored("Other", "True")], [Condition("Ready", "True", "R")], T0)
    assert {c["type"] for c in new} == {"Other", "Ready"}


def test_update_writes_nothing_when_unchanged():
    custom = FakeCustom()
    events = update(
        custom,
        "quotabases",
        body([stored("Valid", "True", "Parsed")]),
        [Condition("Valid", "True", "Parsed")],
        T1,
    )
    assert custom.patches == []
    assert events == []


def test_update_patches_and_warns_on_false():
    custom = FakeCustom()
    b = body()
    events = update(
        custom, "quotabases", b, [Condition("Valid", "False", "InvalidValues", "x")], T0
    )
    assert custom.patches == [
        (
            "quotabases",
            "default",
            {"status": {"conditions": [stored("Valid", "False", "InvalidValues", "x")]}},
        )
    ]
    assert [(e.body, e.type, e.reason, e.message) for e in events] == [
        (b, "Warning", "InvalidValues", "Valid=False: x")
    ]
