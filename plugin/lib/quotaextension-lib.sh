#!/usr/bin/env bash
#
# Shared by every kubectl-quotaextension command and its completions.

# The operator's coordinates; see crds/quotaextension.yaml.
GROUP=quota-warden.gnush.github.io
VERSION=v1alpha1
PLURAL=quotaextensions

die() {
  echo "error: $*" >&2
  exit 1
}

# kubectl hands its global flags to plugins, so these arrive unchanged; they are
# forwarded to every kubectl call rather than parsed. An array, so a value with
# spaces survives.
KUBECTL_ARGS=()

# Pull --context, --kubeconfig and --request-timeout out of the arguments. Sets
# KUBECTL_ARGS and REMAINING to what is left for the command's own parser.
parse_global_flags() {
  REMAINING=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --context|--kubeconfig|--request-timeout)
        [[ $# -ge 2 ]] || die "$1 needs a value"
        KUBECTL_ARGS+=("$1" "$2")
        shift 2
        ;;
      --context=*|--kubeconfig=*|--request-timeout=*)
        KUBECTL_ARGS+=("$1")
        shift
        ;;
      *)
        REMAINING+=("$1")
        shift
        ;;
    esac
  done
}

extension_names() {
  kubectl "${KUBECTL_ARGS[@]}" get "$PLURAL" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null
}

namespace_exists() {
  kubectl "${KUBECTL_ARGS[@]}" get namespace "$1" >/dev/null 2>&1
}

# The keys offered in help and completion. The operator accepts any key, so this
# is a hint list, never a validation.
resources_path() {
  printf '%s\n' "${KUBECTL_QUOTAEXTENSION_RESOURCES:-${XDG_CONFIG_HOME:-$HOME/.config}/kubectl-quotaextension/resources}"
}

resource_keys() {
  local path
  path="$(resources_path)"
  if [[ -f "$path" ]]; then
    sed -e 's/#.*//' -e 's/[[:space:]]//g' "$path" | grep -v '^$'
  else
    printf '%s\n' cpu memory requests.storage persistentvolumeclaims
  fi
}

# A key as the API stores it: no quotes, no YAML syntax.
valid_key() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]
}

# An object name: RFC 1123 subdomain, which the API enforces.
valid_name() {
  [[ "$1" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] && [[ ${#1} -le 253 ]]
}

# The shape the CRD accepts. A day that does not exist is the API's to reject.
valid_date() {
  [[ "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]
}
