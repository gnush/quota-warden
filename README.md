# Quota Warden

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

Unit suites need no cluster: `.venv/bin/pytest`.
