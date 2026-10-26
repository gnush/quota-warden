import pytest

from quota_warden.targeting import SelectorError, is_managed, validate_selector

KUBEFLOW = {"matchLabels": {"app.kubernetes.io/part-of": "kubeflow-profile"}}


def labels(name, **extra):
    return {"kubernetes.io/metadata.name": name, **extra}


def managed(name, selector, protected=None, **extra):
    config = {"managedNamespaces": selector}
    if protected is not None:
        config["protectedNamespaces"] = protected
    return is_managed(labels(name, **extra), name, config)


def test_unset_selector_matches_nothing():
    assert not is_managed(labels("a"), "a", {})
    assert not managed("a", None)


@pytest.mark.parametrize("selector", [{}, {"matchLabels": {}, "matchExpressions": []}])
def test_empty_selector_matches_everything(selector):
    assert managed("a", selector)


def test_selector_match():
    assert is_managed(
        {"app.kubernetes.io/part-of": "kubeflow-profile"}, "a", {"managedNamespaces": KUBEFLOW}
    )


def test_selector_no_match():
    assert not managed("a", KUBEFLOW)
    assert not is_managed(
        {"app.kubernetes.io/part-of": "other"}, "a", {"managedNamespaces": KUBEFLOW}
    )


def test_match_by_metadata_name():
    selector = {
        "matchExpressions": [
            {"key": "kubernetes.io/metadata.name", "operator": "In", "values": ["a", "b"]}
        ]
    }
    assert managed("a", selector)
    assert not managed("c", selector)


def test_match_labels_and_expressions_both_required():
    selector = {
        **KUBEFLOW,
        "matchExpressions": [{"key": "team", "operator": "Exists"}],
    }
    part_of = {"app.kubernetes.io/part-of": "kubeflow-profile"}
    assert is_managed({**part_of, "team": "x"}, "a", {"managedNamespaces": selector})
    assert not is_managed(part_of, "a", {"managedNamespaces": selector})


@pytest.mark.parametrize(
    "operator,values,present,absent",
    [
        ("In", ["x"], True, False),
        ("NotIn", ["x"], False, True),
        ("Exists", None, True, False),
        ("DoesNotExist", None, False, True),
    ],
)
def test_operators(operator, values, present, absent):
    expression = {"key": "team", "operator": operator}
    if values is not None:
        expression["values"] = values
    selector = {"matchExpressions": [expression]}
    assert managed("a", selector, team="x") is present
    assert managed("a", selector) is absent


def test_protected_wins_over_match():
    assert not managed("a", {}, protected=["a"])


@pytest.mark.parametrize("name", ["kube-system", "kube-public", "kube-node-lease"])
@pytest.mark.parametrize("protected", [None, [], ["cattle-system"]])
def test_kube_namespaces_always_protected(name, protected):
    assert not managed(name, {}, protected=protected)


def test_user_protected_adds_to_kube_namespaces():
    assert not managed("cattle-system", {}, protected=["cattle-system"])
    assert managed("default", {}, protected=["cattle-system"])


@pytest.mark.parametrize(
    "expression",
    [
        {"key": "team", "operator": "In"},
        {"key": "team", "operator": "NotIn", "values": []},
        {"key": "team", "operator": "Exists", "values": ["x"]},
        {"key": "team", "operator": "Equals", "values": ["x"]},
    ],
)
def test_malformed_expression_raises(expression):
    with pytest.raises(SelectorError):
        managed("a", {"matchExpressions": [expression]})


def test_malformed_selector_raises_for_protected_namespace():
    with pytest.raises(SelectorError):
        managed("kube-system", {"matchExpressions": [{"key": "team", "operator": "Equals"}]})


def test_validate_selector_is_independent_of_labels():
    validate_selector({"matchExpressions": [{"key": "a", "operator": "Exists"}]})
    validate_selector({})
    with pytest.raises(SelectorError):
        validate_selector({"matchExpressions": [{"key": "a", "operator": "Bogus"}]})
