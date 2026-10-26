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

# Local tag only: the image is loaded into the node, never pulled. Not `latest`,
# which would set imagePullPolicy Always and send the kubelet to a registry.
IMAGE     ?= quota-warden:dev
NAMESPACE  = quota-warden

.PHONY: up down down-compat compat e2e e2e-deploy deploy undeploy clean venv

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

# objects created right after `apply` 404 until the CRD is Established
define INSTALL
kubectl --context $(1) apply -f $(ROOT)/crds/
kubectl --context $(1) wait --for condition=established --timeout=60s -f $(ROOT)/crds/
kubectl --context $(1) apply -f $(ROOT)/cluster/fixtures/namespaces.yaml
endef

# Idempotent: `make e2e` twice must not fail on an existing cluster
up:
	@$(RESTORE_CONTEXT); \
	kind get clusters | grep -qx $(KIND_CLUSTER) \
		|| kind create cluster --name $(KIND_CLUSTER) --image $(NODE_IMAGE) \
			--config $(ROOT)/cluster/kind-config.yaml; \
	$(CONTEXT_PRESENT) || kind export kubeconfig --name $(KIND_CLUSTER)
	$(call INSTALL,$(CONTEXT))

# a pass here is not the gate, a failure is worth reading
compat:
	@$(RESTORE_CONTEXT); \
	kind get clusters | grep -qx $(KIND_CLUSTER)-compat \
		|| kind create cluster --name $(KIND_CLUSTER)-compat --image $(COMPAT_IMAGE) \
			--config $(ROOT)/cluster/kind-config.yaml; \
	kubectl config get-contexts $(COMPAT_CONTEXT) >/dev/null 2>&1 \
		|| kind export kubeconfig --name $(KIND_CLUSTER)-compat
	$(call INSTALL,$(COMPAT_CONTEXT))
	$(MAKE) -f $(ROOT)/cluster/Makefile e2e KIND_CLUSTER=$(KIND_CLUSTER)-compat NO_UP=1

down:
	kind delete cluster --name $(KIND_CLUSTER)

down-compat:
	kind delete cluster --name $(KIND_CLUSTER)-compat

# `up` is an order-only prerequisite:
# a failing create stops the run instead of being swallowed before pytest starts
e2e: | $(if $(NO_UP),,up)
	E2E_CONTEXT=$(CONTEXT) $(PYTEST) $(ROOT)/tests/e2e -v -p no:cacheprovider \
		--ignore=$(ROOT)/tests/e2e/test_deployment.py

# The operator in a pod under its ServiceAccount, which is where deploy/rbac.yaml
# is exercised. An overlay cannot live inside the directory it builds from, so the
# release tag is rewritten in the stream; applying it first would start a pod
# against an image the node does not have.
deploy:
	docker build -t $(IMAGE) $(ROOT)
	kind load docker-image $(IMAGE) --name $(KIND_CLUSTER)
	kubectl kustomize $(ROOT) | sed 's#image: ghcr.io/gnush/quota-warden:.*#image: $(IMAGE)#' \
		| kubectl --context $(CONTEXT) apply -f -
	# the tag never changes, so an edited tree leaves the spec identical and no
	# rollout starts: the old image would keep running against the new code
	kubectl --context $(CONTEXT) -n $(NAMESPACE) rollout restart deploy/quota-warden
	kubectl --context $(CONTEXT) -n $(NAMESPACE) rollout status deploy/quota-warden --timeout=120s

# deploy/ only: `delete -k $(ROOT)` would also drop the CRDs and every source
# in them, which is not what tearing down the operator means
undeploy:
	kubectl --context $(CONTEXT) delete -k $(ROOT)/deploy --ignore-not-found

# The in-pod suite, against the installed Deployment; needs `deploy` first
e2e-deploy: | $(if $(NO_UP),,up)
	$(MAKE) -f $(ROOT)/cluster/Makefile deploy
	E2E_CONTEXT=$(CONTEXT) $(PYTEST) $(ROOT)/tests/e2e/test_deployment.py -v -p no:cacheprovider

venv:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -e "$(ROOT)[dev]"

clean: down down-compat
