# Quota Warden

[![ci](https://github.com/gnush/quota-warden/actions/workflows/ci.yml/badge.svg)](https://github.com/gnush/quota-warden/actions/workflows/ci.yml)

A Kubernetes operator that writes one ResourceQuota per managed namespace,
merged from three sources in the operator's namespace: a base in the
`QuotaWardenConfig`, a per-namespace `QuotaOverride`, and `QuotaExtension`s that
expire on a date. A ResourceQuota
without the operator's managed-by label is reported rather than overwritten.

Built on [kopf](https://kopf.readthedocs.io/). Requires a `QuotaWardenConfig`
before it manages anything — see [Configuration](#configuration).

## Running

```sh
kubectl apply -f peering/clusterkopfpeering.yaml -f crds/ -f namespace.yaml
QUOTA_WARDEN_NAMESPACE=quota-warden .venv/bin/kopf run --all-namespaces -m quota_warden.handlers
```

In a pod it uses the service account and its namespace; elsewhere the current
kubeconfig context, so check `kubectl config current-context` first. `peering/` is kopf's peering
CRD, which this refuses to run without — see [Replicas](#replicas).

Log level is kopf's: `--quiet` for problems only, `--verbose` for everything,
including kopf's per-event lines, which are hidden otherwise.

It does nothing until a `QuotaWardenConfig` exists, and touches no namespace
while that config is missing or invalid.

| Env var                        | Default   |                                                                                                                             |
|--------------------------------|-----------|-----------------------------------------------------------------------------------------------------------------------------|
| `QUOTA_WARDEN_RESYNC_INTERVAL` | `1h`      | full sweep without a triggering event; `<n>s`, `<n>m` or `<n>h`; read at startup. An expiry takes effect at most this late. |
| `QUOTA_WARDEN_NAMESPACE`       | pod's own | namespace holding the operator's objects; must be set when running outside a pod. |

## Deploying

```sh
kubectl apply -k peering    # skip if clusterkopfpeerings.kopf.dev exists
kubectl apply -f namespace.yaml
kubectl apply -k .
```

From a checkout; kustomize fetches nothing extra. From a release tag:

```sh
kubectl apply -k 'github.com/gnush/quota-warden/peering?ref=1.0.0'
kubectl create namespace quota-warden
kubectl apply -k 'github.com/gnush/quota-warden?ref=1.0.0'
```

`peering` is kopf's peering CRD, shared with any other kopf operator — see
[Replicas](#replicas). `.` installs quota-warden's CRDs plus `deploy/`: ServiceAccount, a cluster-wide ClusterRole, a
Role for writes to the sources in the operator namespace, and the Deployment. The namespace is in no kustomization: it holds every source,
so removing the operator keeps it, and deleting it is a separate, explicit step.
Liveness and readiness are kopf's `/healthz` on 8080. The replica count is yours
to raise — see [Replicas](#replicas).

Nothing is managed until a `QuotaWardenConfig` exists, which is left to the
deployment: `deploy/config.yaml` is an example with `dryRun: true`, not part of
the kustomization. Apply it once the selector matches what you intend.

Overlays are for what differs per cluster; the image is `kustomize edit set image`.

Log flags are kopf's (see [Running](#running)), appended to the image's
ENTRYPOINT, so the base passes none: INFO, default format. Per-cluster flags
go in an overlay. `args` is a list, which a strategic-merge patch replaces
whole, so patch by index:

```yaml
patches:
  - target: {kind: Deployment, name: quota-warden}
    patch: |-
      - op: add
        path: /spec/template/spec/containers/0/args/-
        value: --verbose
```

`--log-format` is `plain`, `full` (default) or `json`; `json` only if something
ingests the logs.

## Replicas

`replicas` defaults to 1 and can be raised, e.g. in an overlay:

```yaml
patches:
  - target: {kind: Deployment, name: quota-warden}
    patch: |-
      - op: replace
        path: /spec/replicas
        value: 2
```

kopf [peering](https://kopf.readthedocs.io/en/latest/peering.html) decides which
one works: each replica draws a random priority in the startup handler
(`--priority` cannot, since it is one value shared by every replica), and only
the highest one sweeps. The others stay up, watches warm, paused. A clean exit
hands over at once; a pod killed hard is given up on after its 60s keep-alive
lifetime, the longest a takeover takes.

Equal priorities pause *all* replicas, the first included. With a floor of 1 in
0–32767 two replicas collide about 0.003% of the time; the fix is to roll the
Deployment, which redraws. At that rate it is a stall, not a lost update.

The pause is kopf's, so it covers watch timers, daemons and streams; the sweep
loop is a kopf daemon for that reason. A replica that has just started may sweep
once before it learns of the other, which is harmless: the writes are
server-side apply with one field manager.

**The peering CRD is not optional.** `clusterkopfpeerings.kopf.dev` must be
installed: from `peering/`, or as already present for another kopf operator. It is
shared by every kopf operator in the cluster, so it is deployed apart from this one,
and removing quota-warden leaves it in place. The operator sets `peering.mandatory`. Without the CRD it exits at
startup naming the permission; without the `ClusterKopfPeering` object
`quota-warden` it creates it, and creates it only with RBAC to do so. It also
exits when an existing CRD prunes the object's `status`, where kopf keeps its
peers. One peering
per cluster: installs in two namespaces write the same quotas, so only one sweeps. Missing either, kopf would otherwise
fall back to standalone — every replica sweeping, which is the case peering
exists to prevent.

kopf marks the `QuotaWardenConfig` object with its `kopf.zalando.org/KopfFinalizerMarker`
finalizer, since the sweep daemon's lifetime is that object's. A GitOps tool that
owns the object needs to be told to ignore it, or to keep the operator running
long enough to clear it on delete.

Against the kind cluster, built from the working tree and loaded into the node:

```sh
make -f cluster/Makefile deploy      # build, load, apply, wait for rollout
make -f cluster/Makefile undeploy    # deploy/ only; CRDs and sources stay
```

## GitOps

The repo root is a kustomize base, pinned by release tag. Flux and Argo CD build
it with `path: .`; Fleet cannot, and lists `crds` and `deploy` instead. The peering
CRD is not in the base: deploy `peering/` first, as its own Kustomization, Application
or GitRepo path, unless `clusterkopfpeerings.kopf.dev` already exists. What they
can override without vendoring the manifests differs:

| Tool                    | Image                         | Log flags                                  |
|-------------------------|-------------------------------|--------------------------------------------|
| [Flux](docs/flux.md)    | `spec.images`                 | `spec.patches`                             |
| [Argo CD](docs/argo.md) | `source.kustomize.images`     | `source.kustomize.patches`                 |
| [Fleet](docs/fleet.md)  | copy `deploy/deployment.yaml` | `fleet.yaml` with your own `kustomize.dir` |

## Configuration

One `QuotaWardenConfig` in the operator's namespace drives the operator:
which namespaces are managed, which quota is written and whether it is written at all.
Editing it triggers a sweep. A missing or invalid config touches no namespace:
the operator logs `ConfigError` and, when the object exists, sets `Ready=False`.
The operator reads whichever config exists; two is a configuration error
(`Ready=False/ConfigError`) and touches nothing. Creating the first one starts
the sweep loop and deleting the last one stops it, with no restart either way.
`deploy/config.yaml` is a worked example.

| Field                   | Default        |                                                                             |
|-------------------------|----------------|-----------------------------------------------------------------------------|
| `managedNamespaces`     | unset          | label selector over Namespaces; unset manages nothing, `{}` everything      |
| `baseResources`         | unset          | the base: every managed namespace's default; see [Sources](#sources)        |
| `protectedNamespaces`   | `[]`           | never managed, on top of `kube-system`, `kube-public` and `kube-node-lease` |
| `resourceQuotaName`     | `quota-warden` | the ResourceQuota written                                                   |
| `dryRun`                | `false`        | report what would change; write and delete nothing                          |
| `foreignResourceQuotas` | `report`       | see [Sources](#sources)                                                     |
| `onUnmanage`            | `delete`       | see [Sources](#sources)                                                     |
| `expiredRetentionDays`  | `0`            | days an expired extension is kept, `-1` forever; see [Sources](#sources)    |

The selector takes `matchLabels` and `matchExpressions` with `In`, `NotIn`,
`Exists` and `DoesNotExist`, as in a NetworkPolicy. A malformed expression is
stored and surfaces as `Ready=False`, rather than being rejected by the CRD.

## Sources

One ResourceQuota per managed namespace, merged from three sources.
Examples: `deploy/config.yaml`, `cluster/fixtures/quota-{override,extension}.yaml`.

Sources are read from the operator's namespace only, so write access there is
control over every quota; tenants need none. A source anywhere else has no
effect and gets `Applied=False` (`WrongNamespace`).

| Source                       | Applies to                                                     | Per key                          |
|------------------------------|----------------------------------------------------------------|----------------------------------|
| config `spec.baseResources`  | every managed namespace                                        | the default                      |
| `QuotaOverride`              | `spec.namespace`, which must equal its name: one per namespace | replaces the base                |
| `QuotaExtension`             | `spec.namespace`, through `expirationDate` (UTC)               | added on top of base or override |

A key no source sets is absent from the quota; an empty merge removes the quota.
A source aimed at an unmanaged, protected or missing namespace is inert
(`Targeted=False`). An invalid or negative value drops that key only
(`Valid=False`).

Unset `baseResources` means no defaults; overrides and extensions still apply.
The base's `Valid` and `Applied` are on the config; while it is `Ready=False`
they are `Unknown`, and unsetting `baseResources` removes them.

An expired `QuotaExtension` stops applying at once (`Applied=False`, `Expired`)
and is deleted `expiredRetentionDays` days later, whatever namespace it targets;
under `dryRun` it is only reported. Editing `expirationDate` before then renews
it. `-1` never deletes: set it when extensions are kept in git, or the CD tool
recreates what the operator deletes.

A ResourceQuota in a managed namespace without the
`app.kubernetes.io/managed-by: quota-warden` label is foreign: it still binds by
min(). `foreignResourceQuotas` decides what happens:

| Value              | Effect                                                            |
|--------------------|-------------------------------------------------------------------|
| `report` (default) | `Exclusive=False` on the config, a Warning Event on the Namespace |
| `delete`           | deleted; under `dryRun` only reported                             |
| `ignore`           | nothing                                                           |

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

## kubectl plugin

`plugin/bin/` holds `kubectl-quotaextension-*` scripts; symlinked into a
directory on `PATH`, `kubectl quotaextension <verb>` dispatches to them:

```sh
mkdir -p ~/.local/bin
ln -s "$PWD"/plugin/bin/* ~/.local/bin/
```

kubectl looks for the files directly in a `PATH` directory, so the directory
itself cannot be linked. The scripts resolve their own symlink to find
`plugin/lib/`, so a link into the checkout is enough.

```sh
kubectl quotaextension create -n alice -e 2026-11-08 cpu=8 requests.nvidia.com/gpu=1
kubectl quotaextension list [-n alice]
kubectl quotaextension edit alice-ext-1
kubectl quotaextension delete alice-ext-1
```

Extensions live in the operator's namespace: `QUOTA_WARDEN_NAMESPACE` or
`--warden-namespace`, default `quota-warden`. `-n` is always the target.

`list` shows the operator's verdict per extension in `STATUS`, taken from the
`Applied` condition; `-` means no sweep has seen it yet. Keys offered by `create`
come from `~/.config/kubectl-quotaextension/resources`, one per line, if that
file exists; any key the operator accepts works regardless.

Shell completion works through kubectl's own plugin completion once the scripts
are on `PATH`; no extra setup.

## Tests

Requirements:

| Need            | For                            | Verified against |
|-----------------|--------------------------------|------------------|
| Python ≥ 3.11   | the operator and the e2e suite | 3.15.0           |
| Docker, running | `kind create cluster`          | 29.9.2           |
| `kind`          | the e2e cluster                | v0.33.0          |
| `kubectl`       | fixtures, context handling     | v1.37.1          |

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

`cluster/fixtures/namespaces.yaml` comments say what each namespace is for.

Unit suites need no cluster: `.venv/bin/pytest`. Lint and format:
`.venv/bin/ruff check . && .venv/bin/ruff format --check .`
