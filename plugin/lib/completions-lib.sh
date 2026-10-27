#!/usr/bin/env bash
#
# Completion helpers. Each complete-* script sources ../lib/quotaextension-lib.sh
# and this file, prints "<candidate>\t<description>" lines, then ":<n>" where n
# is the index of the kubectl plugin protocol's word position.

complete_namespaces() {
  kubectl get namespaces \
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
