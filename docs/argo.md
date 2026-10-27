# Argo CD

Argo CD builds the repo root with kustomize, so `path: .` installs what
`kubectl apply -k .` does: CRDs, namespace, ServiceAccount, ClusterRole,
Deployment.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: quota-warden
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/gnush/quota-warden
    path: .
    targetRevision: 1.0.0
  destination:
    server: https://kubernetes.default.svc
  syncPolicy:
    automated:
      prune: true
```

`targetRevision` is the release tag; bumping it is the upgrade. Prune so a file
removed from a release is deleted.

The base applies no `QuotaWardenConfig`: apply one of your own after the first
sync (`deploy/config.yaml` is an example with `dryRun: true`).

A different image or log flags go in the Application, not in this repo:

```yaml
spec:
  source:
    kustomize:
      images:
        - ghcr.io/gnush/quota-warden=registry.internal/quota-warden:1.0.0-internal.1
      patches:
        - target:
            kind: Deployment
            name: quota-warden
          patch: |-
            - op: add
              path: /spec/template/spec/containers/0/args/-
              value: --verbose
```

Both are fields of the `source` above; the patch is the README overlay's, inline.
