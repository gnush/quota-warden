"""Which namespaces are managed."""

# Added to protectedNamespaces in code, so a user-set list cannot drop them.
ALWAYS_PROTECTED = frozenset({"kube-system", "kube-public", "kube-node-lease"})


class SelectorError(ValueError):
    pass


def _matches_expression(labels: dict[str, str], expression: dict) -> bool:
    key = expression.get("key")
    operator = expression.get("operator")
    values = expression.get("values") or []
    if operator in ("In", "NotIn"):
        if not values:
            raise SelectorError(f"{operator} on {key!r} needs values")
        if operator == "In":
            return key in labels and labels[key] in values
        return key not in labels or labels[key] not in values
    if operator in ("Exists", "DoesNotExist"):
        if values:
            raise SelectorError(f"{operator} on {key!r} takes no values")
        return (key in labels) == (operator == "Exists")
    raise SelectorError(f"unknown operator {operator!r} on {key!r}")


def validate_selector(selector: dict) -> None:
    """Raises SelectorError if any expression is malformed, whatever the labels."""
    for expression in selector.get("matchExpressions") or []:
        _matches_expression({}, expression)


def is_managed(namespace_labels: dict[str, str], name: str, config: dict) -> bool:
    """`config` is QuotaWardenConfig.spec. Raises SelectorError on a malformed selector."""
    selector = config.get("managedNamespaces")
    # unset fails closed; {} selects everything, as in Kubernetes
    if selector is None:
        return False
    # evaluated before the protected check so a malformed selector raises for every namespace
    expressions = [
        _matches_expression(namespace_labels, e) for e in selector.get("matchExpressions") or []
    ]
    if name in ALWAYS_PROTECTED or name in (config.get("protectedNamespaces") or []):
        return False
    match_labels = selector.get("matchLabels") or {}
    return all(expressions) and all(namespace_labels.get(k) == v for k, v in match_labels.items())
