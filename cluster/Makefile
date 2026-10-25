# Paths are resolved from the repo root, so this works both as
# `make -f cluster/Makefile up` and `cd cluster && make up`.
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)

KIND_CLUSTER ?= quota-warden-dev
NODE_IMAGE   ?= kindest/node:v1.37.0          # daily
COMPAT_IMAGE ?= kindest/node:v1.28.15         # production-parity check

# kind names contexts `kind-<cluster>`. Every kubectl call names its context
# rather than trusting the current one: the ambient context may be the real
# cluster, and applying fixtures to that is not a mistake worth being able to make.
CONTEXT       = kind-$(KIND_CLUSTER)
COMPAT_CONTEXT = kind-$(KIND_CLUSTER)-compat

VENV   := $(ROOT)/.venv
PYTEST := $(VENV)/bin/pytest

.PHONY: up down down-compat compat e2e clean venv

# The docker container and the kubeconfig are separate: the cluster survives an
# overwritten ~/.kube/config with its context gone, and `kind create` then refuses
# ("node(s) already exist") instead of restoring it. So the context is checked on
# its own, and `export kubeconfig` is what repairs a missing one without touching
# the running cluster.
CONTEXT_PRESENT = kubectl config get-contexts $(CONTEXT) >/dev/null 2>&1

# Idempotent: `make e2e` twice must not fail on an existing cluster.
up:
	@kind get clusters | grep -qx $(KIND_CLUSTER) \
		|| kind create cluster --name $(KIND_CLUSTER) --image $(NODE_IMAGE) \
			--config $(ROOT)/cluster/kind-config.yaml
	@$(CONTEXT_PRESENT) || kind export kubeconfig --name $(KIND_CLUSTER)
	kubectl --context $(CONTEXT) apply -f $(ROOT)/cluster/fixtures/namespaces.yaml

# Same suite against the production version. Advisory: a pass here is not the
# gate, a failure is worth reading. See SPEC "Kubernetes versions".
compat:
	@kind get clusters | grep -qx $(KIND_CLUSTER)-compat \
		|| kind create cluster --name $(KIND_CLUSTER)-compat --image $(COMPAT_IMAGE) \
			--config $(ROOT)/cluster/kind-config.yaml
	@kubectl config get-contexts $(COMPAT_CONTEXT) >/dev/null 2>&1 \
		|| kind export kubeconfig --name $(KIND_CLUSTER)-compat
	kubectl --context $(COMPAT_CONTEXT) apply -f $(ROOT)/cluster/fixtures/namespaces.yaml
	$(MAKE) -f $(ROOT)/cluster/Makefile e2e KIND_CLUSTER=$(KIND_CLUSTER)-compat NO_UP=1

down:
	kind delete cluster --name $(KIND_CLUSTER)

down-compat:
	kind delete cluster --name $(KIND_CLUSTER)-compat

# `up` is an order-only prerequisite, not a recipe step: a failing create stops
# the run instead of being swallowed before pytest starts.
e2e: | $(if $(NO_UP),,up)
	E2E_CONTEXT=$(CONTEXT) $(PYTEST) $(ROOT)/tests/e2e -v -p no:cacheprovider

# `pytest` on PATH is not assumed; the suite runs from the project venv.
venv:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -e "$(ROOT)[dev]"

clean: down down-compat
