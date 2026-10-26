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

## Deploying

```sh
kubectl apply -k .
```

From a checkout; kustomize fetches nothing extra. From a release tag:

```sh
kubectl apply -k 'github.com/gnush/quota-warden?ref=1.0.0'
```

Installs the CRDs plus `deploy/`: namespace `quota-warden`, ServiceAccount, a
cluster-wide ClusterRole, and the Deployment. One replica, `Recreate`, because
`--standalone` has no peering. Liveness and readiness are kopf's
`/healthz` on 8080.

Nothing is managed until a `QuotaWardenConfig` exists, which is left to the
deployment: `deploy/config.yaml` is an example with `dryRun: true`, not part of
the kustomization. Apply it once the selector matches what you intend.

Overlays are for what differs per cluster; the image is `kustomize edit set image`.

Against the kind cluster, built from the working tree and loaded into the node:

```sh
make -f cluster/Makefile deploy      # build, load, apply, wait for rollout
make -f cluster/Makefile undeploy    # deploy/ only; CRDs and sources stay
```

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

A ResourceQuota in a managed namespace without the
`app.kubernetes.io/managed-by: quota-warden` label is foreign: it still binds by
min(). `foreignResourceQuotas` decides what happens:

| Value | Effect |
|---|---|
| `report` (default) | `Exclusive=False` on the config, a Warning Event on the Namespace |
| `delete` | deleted; under `dryRun` only reported |
| `ignore` | nothing |

A foreign quota named `resourceQuotaName` blocks the namespace outside `delete`:
nothing is written there, and its sources get `Applied=False` (`Blocked`).
Edits to any ResourceQuota trigger a sweep, so a hand edit is reverted within seconds.

A namespace that stops being managed (label removed, selector narrowed, or added to
`protectedNamespaces`) loses its quota under `onUnmanage: delete` (default), or keeps
it frozen under `retain`. Either way a Warning Event goes on the Namespace. Overrides
and extensions are kept, so managing the namespace again restores the quota.

The quota's `quota-warden.gnush.github.io/provenance` annotation maps each key to
the sources that set it. Every write and delete is an Event on the quota
(`kubectl describe resourcequota quota-warden -n <ns>`) naming the keys that changed.

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
make -f cluster/Makefile e2e           # kind v1.37.0, the daily loop
make -f cluster/Makefile e2e-deploy    # the same operator in a pod, under its ServiceAccount
make -f cluster/Makefile compat        # the daily loop on v1.28.15, production parity
make -f cluster/Makefile down          # delete the daily cluster
make -f cluster/Makefile clean         # delete both
```

`e2e` runs `kopf run` on the host, as the kubeconfig's user. `e2e-deploy`
installs the Deployment and runs the operator from the image instead, which is
the only place `deploy/rbac.yaml` is exercised — a missing verb only fails there.
It builds the image, so it takes a minute.

`compat` is advisory and does not include `e2e-deploy`. Node images and the
`E2E_CONTEXT` contract are documented in `cluster/Makefile`;
`cluster/fixtures/namespaces.yaml` comments say what each namespace is for.

Unit suites need no cluster: `.venv/bin/pytest`. Lint and format:
`.venv/bin/ruff check . && .venv/bin/ruff format --check .`
