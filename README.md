# Quota Warden

## Running

```sh
kubectl apply -f crds/
.venv/bin/kopf run --all-namespaces --standalone -m quota_warden.handlers
```

In a pod it uses the service account; elsewhere the current kubeconfig context,
so check `kubectl config current-context` first. `--standalone`: no peering is
set up, so run a single replica.

Log level is kopf's: `--quiet` for problems only, `--verbose` for everything,
including kopf's per-event lines, which are hidden otherwise.

It does nothing until a `QuotaWardenConfig` exists, and touches no namespace
while that config is missing or invalid.

| Env var | Default | |
|---|---|---|
| `QUOTA_WARDEN_CONFIG` | `default` | name of the `QuotaWardenConfig` to read |
| `QUOTA_WARDEN_RESYNC_INTERVAL` | `1h` | full sweep without a triggering event; `<n>s`, `<n>m` or `<n>h`; read at startup. An expiry takes effect at most this late. |

## Sources

One ResourceQuota per managed namespace, merged from three cluster-scoped kinds.
Examples: `cluster/fixtures/quota-{base,override,extension}.yaml`.

| Kind | Applies to | Per key |
|---|---|---|
| `QuotaBase` | every managed namespace; the one named by `baseName` | the default |
| `QuotaOverride` | `spec.namespace`, which must equal its name: one per namespace | replaces the base |
| `QuotaExtension` | `spec.namespace`, through `expirationDate` (UTC) | added on top of base or override |

A key no source sets is absent from the quota; an empty merge removes the quota.
A source aimed at an unmanaged, protected or missing namespace is inert
(`Targeted=False`). An invalid or negative value drops that key only
(`Valid=False`).

Expired `QuotaExtension`s are deleted by the operator, whatever namespace they
target; under `dryRun` they are only reported.

## Tests

Requirements:

| Need | For | Verified against |
|---|---|---|
| Python ≥ 3.11 | the operator and the e2e suite | 3.15.0 |
| Docker, running | `kind create cluster` | 29.9.2 |
| `kind` | the e2e cluster | v0.33.0 |
| `kubectl` | fixtures, context handling | v1.37.1 |

One-time setup:

```sh
make -f cluster/Makefile venv      # .venv with kopf, kubernetes, pytest
```

The committed suite is `tests/e2e`, driven by the Makefile:

```sh
make -f cluster/Makefile e2e       # kind v1.37.0, the daily loop
make -f cluster/Makefile compat    # same suite on v1.28.15, production parity
make -f cluster/Makefile down      # delete the daily cluster
make -f cluster/Makefile clean     # delete both
```

`compat` is advisory. Node images and the `E2E_CONTEXT` contract are documented
in `cluster/Makefile`; `cluster/fixtures/namespaces.yaml` comments say what each
namespace is for.

Unit suites need no cluster: `.venv/bin/pytest`. Lint and format:
`.venv/bin/ruff check . && .venv/bin/ruff format --check .`
