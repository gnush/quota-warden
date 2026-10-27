# Fleet

Fleet cannot install the repo root. Its kustomize post-renderer builds an
in-memory filesystem and drops the root when the kustomize dir is `.`, so a root
`kustomization.yaml` fails with `file 'crds/quotabase.yaml' is not in or below ''`
([rancher/fleet#1816](https://github.com/rancher/fleet/issues/1816), closed
unfixed). List the two directories instead; each becomes its own bundle:

```yaml
apiVersion: fleet.cattle.io/v1alpha1
kind: GitRepo
metadata:
  name: quota-warden
  namespace: fleet-local
spec:
  repo: https://github.com/gnush/quota-warden
  revision: 1.0.0
  paths:
    - crds
    - deploy
```

`revision` is the release tag; bumping it is the upgrade. The result is what
`kubectl apply -k .` installs: four CRDs, then namespace, ServiceAccount,
ClusterRole, ClusterRoleBinding, Deployment. Add `spec.targets` to place it
somewhere other than the `default` cluster group.

The base applies no `QuotaWardenConfig`: apply one of your own after the first
sync (`deploy/config.yaml` is an example with `dryRun: true`).

There is no `fleet.yaml` here, and one would not help: Fleet scans the paths
above in this repo, so a file in your repo is never read. Anything past the base
means vendoring the manifests into your repo:

| What | How |
|---|---|
| image | no `fleet.yaml` field; copy `deploy/deployment.yaml` |
| log flags | `fleet.yaml` with `kustomize.dir` at your overlay, over the vendored copy |
| namespace | same, via `targetCustomizations[].kustomize.dir` |

`--standalone` means one replica, so do not scale the Deployment.
