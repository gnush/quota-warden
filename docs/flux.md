# Flux

The repo root is the kustomization Flux builds, so `path: .` installs what
`kubectl apply -k .` does: CRDs, namespace, ServiceAccount, ClusterRole,
Deployment. Pin a release tag for the same reason as in [Releasing](DEV.md#releasing).

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: quota-warden
  namespace: flux-system
spec:
  url: https://github.com/gnush/quota-warden
  ref:
    tag: 1.0.0
  interval: 1h
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: quota-warden
  namespace: flux-system
spec:
  sourceRef:
    kind: GitRepository
    name: quota-warden
  path: .
  prune: true
  interval: 10m
```

`prune: true` so removing a file from a release deletes it. The bundle has no
custom resources, so nothing waits on CRD establishment; the operator still does
nothing until you apply a `QuotaWardenConfig` of your own (`deploy/config.yaml`
is an example, not part of the base).

A different image per cluster goes in `spec.images`, not in this repo — the
image tag is the release, and an overlay here could only guess at it:

```yaml
spec:
  images:
    - name: ghcr.io/gnush/quota-warden
      newName: registry.internal/quota-warden
      newTag: 1.0.0-internal.1
```

Log flags go in `spec.patches`, same JSON patch as the README overlay
(../README.md#deploying).
