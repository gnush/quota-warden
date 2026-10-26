from decimal import Decimal

from quota_warden.merge import Issue, merge
from quota_warden.quantity import parse


def source(name, **resources):
    return {"metadata": {"name": name}, "spec": {"resources": resources}}


def q(**resources):
    return {key.replace("_", "."): parse(value) for key, value in resources.items()}


BASE = source("default", cpu="18", memory="130Gi", **{"requests.storage": "300Gi"})


def test_empty():
    assert merge({}, None, []) == ({}, [])


def test_base_alone():
    assert merge(BASE, None, []) == (q(cpu="18", memory="130Gi", requests_storage="300Gi"), [])


def test_override_replaces_per_key_and_omitted_keys_fall_through():
    override = source("a-override", cpu="120", **{"requests.storage": "425Ti"})
    merged, issues = merge(BASE, override, [])
    assert merged == q(cpu="120", memory="130Gi", requests_storage="425Ti")
    assert issues == []


def test_override_may_lower():
    merged, _ = merge(BASE, source("a-override", cpu="2"), [])
    assert merged["cpu"] == Decimal(2)


def test_extensions_sum_on_top_of_absolute():
    override = source("a-override", cpu="120")
    extensions = [source("a-boost-1", cpu="8"), source("a-boost-2", cpu="500m")]
    merged, _ = merge(BASE, override, extensions)
    assert merged["cpu"] == Decimal("128.5")


def test_extension_on_absent_key_is_the_extension_value():
    merged, _ = merge(BASE, None, [source("a-gpu", **{"requests.nvidia.com/gpu": "8"})])
    assert merged["requests.nvidia.com/gpu"] == Decimal(8)


def test_worked_example():
    base = source("default", gpu="1", storage="300Gi", cpu="18")
    override = source("a-override", gpu="3", storage="425Ti")
    merged, _ = merge(base, override, [source("a-boost", gpu="8")])
    assert merged == q(gpu="11", storage="425Ti", cpu="18")


def test_mixed_units_add():
    merged, _ = merge(
        source("default", memory="1024Gi", cpu="500m"),
        None,
        [source("a-boost", memory="1Ti", cpu="1")],
    )
    assert merged == q(memory="2Ti", cpu="1.5")


def test_keys_are_opaque():
    merged, _ = merge(
        source("default", cpu="1"), None, [source("a-boost", **{"requests.cpu": "2"})]
    )
    assert merged == q(cpu="1", requests_cpu="2")


def test_invalid_value_dropped_for_that_key_only():
    override = source("a-override", cpu="lots", memory="-1Gi", pods="0")
    merged, issues = merge(BASE, override, [source("a-boost", cpu=1.5)])
    assert merged == q(cpu="18", memory="130Gi", requests_storage="300Gi", pods="0")
    assert {(i.kind, i.source, i.key) for i in issues} == {
        ("invalid", "a-override", "cpu"),
        ("invalid", "a-override", "memory"),
        ("invalid", "a-boost", "cpu"),
    }
    assert all(isinstance(i, Issue) and i.message for i in issues)


def test_source_without_resources():
    assert merge({"metadata": {"name": "default"}, "spec": {}}, None, []) == ({}, [])
