"""kubectl quotaextension edit."""

import pytest


def test_edit_opens_the_named_extension_in_the_editor(run):
    result = run("kubectl-quotaextension-edit", "alice-ext-1")
    assert result.returncode == 0, result.stderr
    assert result.args == ["edit quotaextension alice-ext-1"]


@pytest.mark.parametrize(
    "args, message",
    [
        ([], "NAME is required"),
        (["a", "b"], "one name at a time"),
        (["--nope"], "Unknown option"),
    ],
)
def test_edit_refuses_before_calling_kubectl(run, args, message):
    result = run("kubectl-quotaextension-edit", *args)
    assert result.returncode == 1
    assert message in result.stderr
    assert not result.args


def test_edit_passes_global_flags_through(run):
    result = run(
        "kubectl-quotaextension-edit",
        "--context",
        "kind-test",
        "alice-ext-1",
    )
    assert result.args == ["--context kind-test edit quotaextension alice-ext-1"]


def test_edit_fails_when_kubectl_does(run):
    result = run("kubectl-quotaextension-edit", "missing", env={"STUB_EDIT_EXIT": "1"})
    assert result.returncode == 1
