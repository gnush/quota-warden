"""kubectl quotaextension create."""

import pytest


def test_create_writes_a_cluster_scoped_extension(run):
    result = run(
        "kubectl-quotaextension-create",
        "-n",
        "alice",
        "-e",
        "2026-11-08",
        "cpu=8",
        "requests.nvidia.com/gpu=1",
    )
    assert result.returncode == 0, result.stderr
    assert result.manifest == {
        "apiVersion": "quota-warden.gnush.github.io/v1alpha1",
        "kind": "QuotaExtension",
        "metadata": {"name": "alice-ext-1"},
        "spec": {
            "namespace": "alice",
            "expirationDate": "2026-11-08",
            "resources": {"cpu": "8", "requests.nvidia.com/gpu": "1"},
        },
    }


def test_create_numbers_names_around_existing_ones(run):
    result = run(
        "kubectl-quotaextension-create",
        "-n",
        "alice",
        "-e",
        "2026-11-08",
        "cpu=1",
        env={"STUB_EXTENSIONS": "alice-ext-1\nbob-ext-1\nalice-ext-3"},
    )
    assert result.manifest["metadata"]["name"] == "alice-ext-2"


def test_create_takes_an_explicit_name(run):
    result = run(
        "kubectl-quotaextension-create",
        "-n",
        "alice",
        "-e",
        "2026-11-08",
        "--name",
        "alice-gpu-boost",
        "cpu=1",
    )
    assert result.manifest["metadata"]["name"] == "alice-gpu-boost"


@pytest.mark.parametrize(
    "args, message",
    [
        (["-e", "2026-11-08", "cpu=1"], "-n is required"),
        (["-n", "alice", "cpu=1"], "-e is required"),
        (["-n", "alice", "-e", "2026-11-08"], "no resources given"),
        (["-n", "alice", "-e", "08.11.2026", "cpu=1"], "is not YYYY-MM-DD"),
        (["-n", "alice", "-e", "2026-11-08", "cpu"], "is not KEY=VALUE"),
        (["-n", "alice", "-e", "2026-11-08", "cpu="], "has no value"),
        (["-n", "alice", "-e", "2026-11-08", "cpu=1", "cpu=2"], "given twice"),
        (["-n", "alice", "-e", "2026-11-08", "=1"], "is not a resource key"),
    ],
)
def test_create_refuses_before_calling_apply(run, args, message):
    result = run("kubectl-quotaextension-create", *args)
    assert result.returncode == 1
    assert message in result.stderr
    assert not any("apply" in line for line in result.args)


def test_create_refuses_a_missing_namespace(run):
    result = run(
        "kubectl-quotaextension-create",
        "-n",
        "nope",
        "-e",
        "2026-11-08",
        "cpu=1",
        env={"STUB_NAMESPACE_EXIT": "1"},
    )
    assert result.returncode == 1
    assert "namespace 'nope' does not exist" in result.stderr


def test_create_passes_global_flags_through(run):
    result = run(
        "kubectl-quotaextension-create",
        "--context",
        "kind-test",
        "--kubeconfig=/tmp/kc",
        "-n",
        "alice",
        "-e",
        "2026-11-08",
        "cpu=1",
    )
    assert result.returncode == 0, result.stderr
    assert all("--context kind-test --kubeconfig=/tmp/kc" in line for line in result.args)


def test_create_fails_when_apply_fails(run):
    result = run(
        "kubectl-quotaextension-create",
        "-n",
        "alice",
        "-e",
        "2026-11-08",
        "cpu=1",
        env={"STUB_APPLY_EXIT": "1"},
    )
    assert result.returncode == 1
    assert "created" not in result.stdout
