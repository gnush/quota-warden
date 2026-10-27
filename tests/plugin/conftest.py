"""The plugin scripts, run against a stub kubectl.

Unit-suite only: no cluster. The stub records its argv and the manifest on
stdin, and answers the reads and writes the scripts make.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

BIN = Path(__file__).parents[2] / "plugin" / "bin"

STUB = """#!/usr/bin/env bash
# A kubectl that records what it was asked to do.
printf '%s\\n' "$*" >> "$STUB_LOG.args"
if [[ " $* " == *" get namespace "* ]]; then
  exit "${STUB_NAMESPACE_EXIT:-0}"
fi
if [[ " $* " == *" get quotaextensions "* ]]; then
  # the listing asks for the Applied condition, the name lookup does not
  if [[ "$*" == *"Applied"* ]]; then
    printf '%s\\n' "${STUB_ROWS:-}"
  else
    printf '%s\\n' "${STUB_EXTENSIONS:-}"
  fi
  exit 0
fi
if [[ " $* " == *" apply -f -"* ]]; then
  cat >> "$STUB_LOG.stdin"
  exit "${STUB_APPLY_EXIT:-0}"
fi
if [[ " $* " == *" edit "* ]]; then
  exit "${STUB_EDIT_EXIT:-0}"
fi
if [[ " $* " == *" delete "* ]]; then
  exit "${STUB_DELETE_EXIT:-0}"
fi
exit 0
"""

ENV_NAMES = (
    "STUB_EXTENSIONS",
    "STUB_ROWS",
    "STUB_NAMESPACE_EXIT",
    "STUB_APPLY_EXIT",
    "STUB_EDIT_EXIT",
    "STUB_DELETE_EXIT",
)


@pytest.fixture
def run(tmp_path):
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    stub = stub_dir / "kubectl"
    stub.write_text(STUB)
    stub.chmod(0o755)
    log = tmp_path / "log"
    log.mkdir()

    def run(script, *args, env=None, stdin=None):
        environ = {
            **os.environ,
            "PATH": f"{stub_dir}:{os.environ['PATH']}",
            "STUB_LOG": log / "run",
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        }
        for name in ENV_NAMES:
            if env and name in env:
                environ[name] = env[name]
            else:
                environ.pop(name, None)
        result = subprocess.run(
            [BIN / script, *args],
            env=environ,
            capture_output=True,
            text=True,
            input=stdin,
        )
        args_log = log / "run.args"
        manifest_log = log / "run.stdin"
        return Result(
            result,
            args_log.read_text().splitlines() if args_log.exists() else [],
            manifest_log.read_text() if manifest_log.exists() else "",
        )

    return run


class Result:
    def __init__(self, proc, args, stdin):
        self.returncode = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.args = args
        self.stdin = stdin

    @property
    def manifest(self):
        return yaml.safe_load(self.stdin)

    @property
    def table(self):
        """The listing as a list of rows, header first."""
        return [line.split() for line in self.stdout.splitlines() if line.strip()]
