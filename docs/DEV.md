# Development

## Releasing

The image tag in `deploy/deployment.yaml` must match the git tag, and must be
bumped in a commit that the tag points at:

1. Set `image: ghcr.io/gnush/quota-warden:<version>` in `deploy/deployment.yaml`.
2. Set `version` in `pyproject.toml` to the same `<version>`.
3. Commit.
4. `git tag <version>` on that commit and push the tag.

Do not tag first. `kubectl apply -k 'github.com/gnush/quota-warden?ref=<version>'`
serves the manifests from the tagged commit, so a tag cut before the bump
installs the new CRDs with the previous release's image, and nothing fails
loudly.
