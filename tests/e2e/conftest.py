"""Kind cluster access for the e2e suite.

The context comes from `make e2e`, never from the ambient kubeconfig — see the
Makefile. A missing E2E_CONTEXT is an error rather than a silent fallback to
whatever cluster `kubectl` happens to point at.
"""

import os
import time

import pytest
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

LABEL = "app.kubernetes.io/part-of"
MANAGED_LABEL = {LABEL: "kubeflow-profile"}

GROUP = "quota-warden.gnush.github.io"
VERSION = "v1alpha1"

# Makefile pins
SUPPORTED_VERSIONS = ("v1.28.", "v1.37.")


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(0.2)


def exists(fn, *args):
    try:
        fn(*args)
    except ApiException as exc:
        if exc.status == 404:
            return False
        raise
    return True


def delete(fn, *args):
    try:
        fn(*args)
    except ApiException as exc:
        if exc.status != 404:
            raise


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
