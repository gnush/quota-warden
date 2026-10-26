from datetime import datetime, timedelta, timezone

import pytest

from quota_warden.merge import is_active

UTC = timezone.utc
MIDNIGHT_AFTER = datetime(2026, 11, 9, tzinfo=UTC)


def extension(expiration_date):
    spec = {"namespace": "a", "resources": {"cpu": "8"}}
    if expiration_date is not None:
        spec["expirationDate"] = expiration_date
    return {"metadata": {"name": "a-boost"}, "spec": spec}


@pytest.mark.parametrize(
    "now,active",
    [
        (datetime(2026, 11, 1, tzinfo=UTC), True),
        (datetime(2026, 11, 8, tzinfo=UTC), True),
        (MIDNIGHT_AFTER - timedelta(seconds=1), True),
        (MIDNIGHT_AFTER, False),
        (MIDNIGHT_AFTER + timedelta(seconds=1), False),
    ],
)
def test_boundary(now, active):
    assert is_active(extension("2026-11-08"), now) is active


def test_boundary_is_utc():
    # 00:30 on the 9th in +01:00 is still the 8th in UTC
    now = datetime(2026, 11, 9, 0, 30, tzinfo=timezone(timedelta(hours=1)))
    assert is_active(extension("2026-11-08"), now)


@pytest.mark.parametrize(
    "value",
    [None, "", "2026-13-01", "2026-02-30", "20261108", "2026-W45-7", "2026-11-08T00:00Z", 20261108],
)
def test_missing_or_unparseable_is_inactive(value):
    assert not is_active(extension(value), datetime(2000, 1, 1, tzinfo=UTC))


def test_no_spec_is_inactive():
    assert not is_active({}, datetime(2000, 1, 1, tzinfo=UTC))
