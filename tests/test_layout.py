"""What each deployable path builds to; the GitOps docs list these paths."""

import pathlib
import shutil
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).parent.parent
PEERING_CRD = "clusterkopfpeerings.kopf.dev"

pytestmark = pytest.mark.skipif(not shutil.which("kubectl"), reason="needs kubectl kustomize")


def build(path):
    out = subprocess.run(
        ["kubectl", "kustomize", str(ROOT / path)], check=True, capture_output=True, text=True
    ).stdout
    return [(o["kind"], o["metadata"]["name"]) for o in yaml.safe_load_all(out) if o]


def test_peering_builds_only_the_peering_crd():
    assert build("peering") == [("CustomResourceDefinition", PEERING_CRD)]


@pytest.mark.parametrize("path", [".", "deploy"])
def test_the_operator_paths_leave_the_peering_crd_out(path):
    # another kopf operator may own it; shipping it here fails under Fleet
    assert ("CustomResourceDefinition", PEERING_CRD) not in build(path)


def test_crds_dir_has_no_peering_crd():
    # Fleet's `crds` path is the directory, not a kustomization
    names = [
        yaml.safe_load(p.read_text())["metadata"]["name"] for p in (ROOT / "crds").glob("*.yaml")
    ]
    assert PEERING_CRD not in names
