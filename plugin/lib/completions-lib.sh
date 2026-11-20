#!/usr/bin/env bash
#
# Completion helpers. Each complete-* script sources ../lib/quotaextension-lib.sh
# and this file, prints "<candidate>\t<description>" lines, then ":<n>" where n
# is the index of the kubectl plugin protocol's word position.

# kubectl passes the typed words with the one being completed last. Applies the
# global flags already typed, so suggestions come from the cluster and operator
# namespace the command would use, and sets WORDS to the rest. If the word
# before the current one is a global flag, completes its value and exits.
parse_completion_args() {
  WORDS=()
  [[ $# -gt 0 ]] || return 0
  local current="${!#}" previous=""
  [[ $# -ge 2 ]] && previous="${*: -2:1}"
  case "$previous" in
    --warden-namespace) complete_namespaces; echo ":4"; exit 0 ;;
    --context|--kubeconfig|--request-timeout) echo ":4"; exit 0 ;;
  esac
  parse_global_flags "${@:1:$#-1}"
  WORDS=("${REMAINING[@]}" "$current")
}

complete_namespaces() {
  kubectl "${KUBECTL_ARGS[@]}" get namespaces \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null
}

# Today and the common durations. GNU date and BSD date take different flags.
complete_expiration_dates() {
  if date -d "+1 day" +%Y-%m-%d >/dev/null 2>&1; then
    printf '%s\n' \
      "$(date +%Y-%m-%d)" \
      "$(date -d "+1 day" +%Y-%m-%d)" \
      "$(date -d "+7 days" +%Y-%m-%d)" \
      "$(date -d "+30 days" +%Y-%m-%d)" \
      "$(date -d "+90 days" +%Y-%m-%d)"
  else
    printf '%s\n' \
      "$(date +%Y-%m-%d)" \
      "$(date -v+1d +%Y-%m-%d)" \
      "$(date -v+7d +%Y-%m-%d)" \
      "$(date -v+30d +%Y-%m-%d)" \
      "$(date -v+90d +%Y-%m-%d)"
  fi
}

# key=value, since a bare key is never a complete argument.
complete_resource_keys() {
  local key
  resource_keys | while IFS= read -r key; do
    printf '%s=\n' "$key"
  done
}

complete_extension_names() {
  extension_names
}
