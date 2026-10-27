"""kubectl quotaextension delete."""

import pytest


def test_delete_asks_then_deletes(run):
    result = run("kubectl-quotaextension-delete", "alice-ext-1", stdin="y\n")
    assert result.returncode == 0, result.stderr
    assert result.args == ["delete quotaextension alice-ext-1"]
    assert "Delete alice-ext-1?" in result.stdout


def test_delete_deletes_several_at_once(run):
    result = run("kubectl-quotaextension-delete", "-y", "a", "b")
    assert result.args == ["delete quotaextension a b"]


@pytest.mark.parametrize("answer", ["", "n\n", "no\n", "yes please\n"])
def test_delete_cancels_on_anything_but_yes(run, answer):
    result = run("kubectl-quotaextension-delete", "alice-ext-1", stdin=answer)
    assert result.returncode == 1
    assert "cancelled" in result.stdout
    assert not result.args


@pytest.mark.parametrize(
    "args, message",
    [([], "at least one NAME"), (["--nope", "a"], "Unknown option")],
)
def test_delete_refuses_before_asking(run, args, message):
    result = run("kubectl-quotaextension-delete", *args, stdin="y\n")
    assert result.returncode == 1
    assert message in result.stderr
    assert not result.args


def test_delete_passes_global_flags_through(run):
    result = run("kubectl-quotaextension-delete", "--context", "kind-test", "-y", "a")
    assert result.args == ["--context kind-test delete quotaextension a"]


def test_delete_fails_when_kubectl_does(run):
    result = run("kubectl-quotaextension-delete", "-y", "a", env={"STUB_DELETE_EXIT": "1"})
    assert result.returncode == 1
