# Fleet

The repo root has a `kustomization.yaml`, so Fleet builds it with kustomize and
installs what `kubectl apply -k .` does: CRDs, namespace, ServiceAccount,
ClusterRole, Deployment.

```yaml
apiVersion: fleet.cattle.io/v1alpha1
kind: GitRepo
metadata:
  name: quota-warden
  namespace: fleet-default
spec:
  repo: https://github.com/gnush/quota-warden
  revision: 1.0.0
```

`revision` is the release tag; bumping it is the upgrade. No `fleet.yaml` is
needed. Targets default to the `default` cluster group; add `spec.targets` to
place it elsewhere.

The base applies no `QuotaWardenConfig`: apply one of your own after the first
sync (`deploy/config.yaml` is an example with `dryRun: true`).

Fleet runs kustomize as a post-renderer over the files it scanned from the
GitRepo path, not as a downloader, so a `kustomization.yaml` here cannot point
at this repo's git URL. Anything past the base means vendoring the manifests
into your repo:

| What | How |
|---|---|
| image | no `fleet.yaml` field, and no remote base to wrap; copy `deploy/deployment.yaml` |
| log flags | `fleet.yaml` with `kustomize.dir` at your overlay, over the vendored copy |
| namespace | same, via `targetCustomizations[].kustomize.dir` |

`--standalone` means one replica, so do not scale the Deployment.
