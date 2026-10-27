"""kubectl quotaextension list."""

import pytest

HEADER = ["NAME", "NAMESPACE", "EXPIRES", "STATUS"]


def test_list_prints_a_row_per_extension_sorted_by_namespace(run):
    result = run(
        "kubectl-quotaextension-list",
        env={
            "STUB_ROWS": "bob-ext-1\tbob\t2026-12-01\tApplied\n"
            "alice-ext-2\talice\t2026-11-09\tApplied\n"
            "alice-ext-1\talice\t2026-11-08\tApplied"
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.table == [
        HEADER,
        ["alice-ext-1", "alice", "2026-11-08", "Applied"],
        ["alice-ext-2", "alice", "2026-11-09", "Applied"],
        ["bob-ext-1", "bob", "2026-12-01", "Applied"],
    ]


def test_list_filters_by_namespace(run):
    result = run(
        "kubectl-quotaextension-list",
        "-n",
        "alice",
        env={
            "STUB_ROWS": "alice-ext-1\talice\t2026-11-08\tApplied\n"
            "bob-ext-1\tbob\t2026-12-01\tApplied"
        },
    )
    assert [row[0] for row in result.table[1:]] == ["alice-ext-1"]


def test_list_shows_a_dash_before_the_first_sweep(run):
    result = run(
        "kubectl-quotaextension-list",
        env={"STUB_ROWS": "alice-ext-1\talice\t2026-11-08\t"},
    )
    assert result.table == [HEADER, ["alice-ext-1", "alice", "2026-11-08", "-"]]


def test_list_reports_an_empty_cluster(run):
    result = run("kubectl-quotaextension-list")
    assert result.returncode == 0
    assert result.stdout.strip() == "No QuotaExtensions"


def test_list_reports_an_empty_filtered_result(run):
    result = run(
        "kubectl-quotaextension-list",
        "-n",
        "carol",
        env={"STUB_ROWS": "alice-ext-1\talice\t2026-11-08\tApplied"},
    )
    assert result.returncode == 0
    assert "No QuotaExtensions for namespace 'carol'" in result.stdout


def test_list_passes_global_flags_through(run):
    result = run(
        "kubectl-quotaextension-list",
        "--context",
        "kind-test",
        env={"STUB_ROWS": "alice-ext-1\talice\t2026-11-08\tApplied"},
    )
    assert result.returncode == 0, result.stderr
    assert all("--context kind-test" in line for line in result.args)


@pytest.mark.parametrize("args", [["alice"], ["-x"]])
def test_list_refuses_arguments_it_does_not_take(run, args):
    result = run("kubectl-quotaextension-list", *args)
    assert result.returncode == 1
    assert not result.args
