"""Kind cluster access for the e2e suite.

The context comes from `make e2e`, never from the ambient kubeconfig — see the
Makefile. A missing E2E_CONTEXT is an error rather than a silent fallback to
whatever cluster `kubectl` happens to point at.
"""

import os

import pytest
from kubernetes import client, config

LABEL = "app.kubernetes.io/part-of"
MANAGED_LABEL = {LABEL: "kubeflow-profile"}

# Makefile pins
SUPPORTED_VERSIONS = ("v1.28.", "v1.37.")


@pytest.fixture(scope="session")
def api():
    context = os.environ.get("E2E_CONTEXT")
    if not context:
        pytest.fail("E2E_CONTEXT is unset; run the suite via `make e2e`")

    config.load_kube_config(context=context)

    # Fail loudly if the cluster is not one the tests were written for
    version = client.VersionApi().get_code()
    if not version.git_version.startswith(SUPPORTED_VERSIONS):
        pytest.fail(
            f"unsupported server version {version.git_version}; "
            f"expected one of {SUPPORTED_VERSIONS}"
        )

    return client.CoreV1Api()


@pytest.fixture(scope="session")
def custom(api):
    # depends on `api` for the kubeconfig load and version check
    return client.CustomObjectsApi()
