# resolved from the repo root: works as `make -f cluster/Makefile up`
# and as `cd cluster && make up`
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)

KIND_CLUSTER ?= quota-warden-dev
NODE_IMAGE   ?= kindest/node:v1.37.0          # daily
COMPAT_IMAGE ?= kindest/node:v1.28.15         # production-parity check

# kind names contexts `kind-<cluster>`
CONTEXT       = kind-$(KIND_CLUSTER)
COMPAT_CONTEXT = kind-$(KIND_CLUSTER)-compat

VENV   := $(ROOT)/.venv
PYTEST := $(VENV)/bin/pytest

.PHONY: up down down-compat compat e2e clean venv

# `export kubeconfig` repairs a missing config
CONTEXT_PRESENT = kubectl config get-contexts $(CONTEXT) >/dev/null 2>&1

# `kind create` and `export kubeconfig` repoint current-context; `kind delete`
# does not. trap, not a trailing command, so a failing target restores too.
define RESTORE_CONTEXT
set -e; \
prev="$$(kubectl config current-context 2>/dev/null || true)"; \
restore() { [ -z "$$prev" ] || kubectl config use-context "$$prev" >/dev/null 2>&1 || true; }; \
trap restore EXIT
endef

# Idempotent: `make e2e` twice must not fail on an existing cluster
up:
	@$(RESTORE_CONTEXT); \
	kind get clusters | grep -qx $(KIND_CLUSTER) \
		|| kind create cluster --name $(KIND_CLUSTER) --image $(NODE_IMAGE) \
			--config $(ROOT)/cluster/kind-config.yaml; \
	$(CONTEXT_PRESENT) || kind export kubeconfig --name $(KIND_CLUSTER)
	kubectl --context $(CONTEXT) apply -f $(ROOT)/cluster/fixtures/namespaces.yaml

# a pass here is not the gate, a failure is worth reading
compat:
	@$(RESTORE_CONTEXT); \
	kind get clusters | grep -qx $(KIND_CLUSTER)-compat \
		|| kind create cluster --name $(KIND_CLUSTER)-compat --image $(COMPAT_IMAGE) \
			--config $(ROOT)/cluster/kind-config.yaml; \
	kubectl config get-contexts $(COMPAT_CONTEXT) >/dev/null 2>&1 \
		|| kind export kubeconfig --name $(KIND_CLUSTER)-compat; \
	kubectl --context $(COMPAT_CONTEXT) apply -f $(ROOT)/cluster/fixtures/namespaces.yaml
	$(MAKE) -f $(ROOT)/cluster/Makefile e2e KIND_CLUSTER=$(KIND_CLUSTER)-compat NO_UP=1

down:
	kind delete cluster --name $(KIND_CLUSTER)

down-compat:
	kind delete cluster --name $(KIND_CLUSTER)-compat

# `up` is an order-only prerequisite:
# a failing create stops the run instead of being swallowed before pytest starts
e2e: | $(if $(NO_UP),,up)
	E2E_CONTEXT=$(CONTEXT) $(PYTEST) $(ROOT)/tests/e2e -v -p no:cacheprovider

venv:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -e "$(ROOT)[dev]"

clean: down down-compat
