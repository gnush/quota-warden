import pytest

COMPLETERS = (
    "kubectl_complete-quotaextension-delete",
    "kubectl_complete-quotaextension-edit",
)


@pytest.mark.parametrize("script", COMPLETERS)
def test_names_from_default_warden_namespace(run, script):
    result = run(script, "", env={"STUB_EXTENSIONS": "ext-a"})
    assert "ext-a" in result.stdout.splitlines()
    assert any("-n quota-warden get quotaextensions" in a for a in result.args)


@pytest.mark.parametrize("script", COMPLETERS)
def test_names_from_env_warden_namespace(run, script):
    result = run(script, "", env={"QUOTA_WARDEN_NAMESPACE": "from-env"})
    assert any("-n from-env get quotaextensions" in a for a in result.args)


@pytest.mark.parametrize("script", COMPLETERS)
@pytest.mark.parametrize("flag", (["--warden-namespace", "typed"], ["--warden-namespace=typed"]))
def test_names_from_typed_warden_namespace(run, script, flag):
    result = run(
        script,
        *flag,
        "",
        env={"QUOTA_WARDEN_NAMESPACE": "from-env", "STUB_EXTENSIONS": "ext-a"},
    )
    assert "ext-a" in result.stdout.splitlines()
    assert any("-n typed get quotaextensions" in a for a in result.args)
    assert not any("from-env" in a for a in result.args)


@pytest.mark.parametrize("script", COMPLETERS)
def test_names_with_typed_context(run, script):
    result = run(script, "--context", "staging", "")
    assert any(
        a.startswith("--context staging -n quota-warden get quotaextensions") for a in result.args
    )


@pytest.mark.parametrize(
    "script",
    (
        "kubectl_complete-quotaextension-create",
        "kubectl_complete-quotaextension-list",
        *COMPLETERS,
    ),
)
def test_warden_namespace_value_completes_namespaces(run, script):
    result = run(script, "--warden-namespace", "")
    assert result.stdout.splitlines()[-1] == ":4"
    assert any("get namespaces" in a for a in result.args)


@pytest.mark.parametrize(
    "script",
    ("kubectl_complete-quotaextension-create", "kubectl_complete-quotaextension-list"),
)
def test_target_namespaces_with_typed_context(run, script):
    result = run(script, "--context", "staging", "-n", "")
    assert any(a.startswith("--context staging get namespaces") for a in result.args)


def test_create_flags_after_global_flag(run):
    result = run("kubectl_complete-quotaextension-create", "--warden-namespace", "qw", "")
    assert "--expiration\tExpiration date (YYYY-MM-DD)" in result.stdout
