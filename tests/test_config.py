from datetime import timedelta

import pytest

from quota_warden.config import ConfigError, config_name, load, resync_interval

# as stored: the CRD fills the defaults
DEFAULTS = {
    "resourceQuotaName": "quota-warden",
    "dryRun": False,
    "foreignResourceQuotas": "report",
    "onUnmanage": "delete",
}


def body(**spec):
    return {"spec": {**DEFAULTS, **spec}}


def test_load():
    config = load(body(managedNamespaces={}, dryRun=True))
    assert (config.resource_quota_name, config.dry_run) == ("quota-warden", True)
    assert config.spec["managedNamespaces"] == {}


def test_load_reads_the_sole_authority_settings():
    config = load(body(managedNamespaces={}, foreignResourceQuotas="delete", onUnmanage="retain"))
    assert (config.foreign_resource_quotas, config.on_unmanage) == ("delete", "retain")


def test_missing_config_is_an_error():
    with pytest.raises(ConfigError, match="not found"):
        load(None)


def test_unset_selector_is_an_error():
    with pytest.raises(ConfigError, match="managedNamespaces is unset"):
        load(body())


@pytest.mark.parametrize(
    "expression",
    [
        {"key": "a", "operator": "Bogus"},
        {"key": "a", "operator": "In"},
        {"key": "a", "operator": "Exists", "values": ["x"]},
    ],
)
def test_malformed_selector_is_an_error_carrying_the_reason(expression):
    with pytest.raises(ConfigError, match=r"managedNamespaces: .*'a'"):
        load(body(managedNamespaces={"matchExpressions": [expression]}))


def test_field_the_crd_should_default_is_an_error():
    with pytest.raises(ConfigError, match="resourceQuotaName missing"):
        spec = {k: v for k, v in DEFAULTS.items() if k != "resourceQuotaName"}
        load({"spec": {**spec, "managedNamespaces": {}}})


def test_config_name():
    assert config_name({}) == "default"
    assert config_name({"QUOTA_WARDEN_CONFIG": "other"}) == "other"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, timedelta(hours=1)),
        ("", timedelta(hours=1)),
        ("90s", timedelta(seconds=90)),
        ("30m", timedelta(minutes=30)),
        ("2h", timedelta(hours=2)),
    ],
)
def test_resync_interval(value, expected):
    environ = {} if value is None else {"QUOTA_WARDEN_RESYNC_INTERVAL": value}
    assert resync_interval(environ) == expected


@pytest.mark.parametrize("value", ["0s", "1", "1d", "1h30m", "-1h", "1.5h", " 1h"])
def test_resync_interval_rejects(value):
    with pytest.raises(ConfigError, match="QUOTA_WARDEN_RESYNC_INTERVAL"):
        resync_interval({"QUOTA_WARDEN_RESYNC_INTERVAL": value})
