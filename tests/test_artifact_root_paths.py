#!/usr/bin/env python3
# tests/test_artifact_root_paths.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Every tool finds its data under ARTIFACT_ROOT the same way.

A directory's own variable wins; otherwise it is a fixed subdirectory of
ARTIFACT_ROOT, which defaults to /work/migrate-v2v. The playbooks, the bin/
Python tools, and the vault helpers must agree.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import catalog_common  # noqa: E402
import vault_common  # noqa: E402

def load_script(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

install_ovftool = load_script("install-ovftool.py", "arp_install_ovftool")
install_openvddk = load_script("install-openvddk.py", "arp_install_openvddk")

# (name, own variable, path under the artifact root)
DIRECTORIES = (
    ("vault_dir", "MIGRATE_V2V_VAULT_DIR", "vaults"),
    ("catalog_dir", "MIGRATE_V2V_CATALOG_DIR", "catalogs"),
    ("ovftool_install_dir", "OVFTOOL_INSTALL_DIR", "tools/ovftool-5.1.0"),
    ("openvddk_install_dir", "OPENVDDK_INSTALL_DIR", "tools/openvddk"),
)

def python_paths():
    return {
        "vault_dir": vault_common.default_vault_path("x.yml").parent,
        "catalog_dir": catalog_common.default_catalog_path("x.json").parent,
        "ovftool_install_dir": install_ovftool.install_dir(),
        "openvddk_install_dir": install_openvddk.install_dir(None),
    }

class PythonToolPathTests(unittest.TestCase):

    def paths(self, env):
        with mock.patch.dict(os.environ, env, clear=True):
            return python_paths()

    def test_default_root(self):
        paths = self.paths({})
        for name, _, sub in DIRECTORIES:
            self.assertEqual(paths[name], Path("/work/migrate-v2v", sub), name)

    def test_artifact_root_moves_every_directory(self):
        paths = self.paths({"ARTIFACT_ROOT": "/srv/v2v"})
        for name, _, sub in DIRECTORIES:
            self.assertEqual(paths[name], Path("/srv/v2v", sub), name)

    def test_own_variable_wins_over_artifact_root(self):
        env = {"ARTIFACT_ROOT": "/srv/v2v"}
        env.update({var: f"/elsewhere/{name}" for name, var, _ in DIRECTORIES})
        paths = self.paths(env)
        for name, _, _ in DIRECTORIES:
            self.assertEqual(paths[name], Path("/elsewhere", name), name)

    def test_openvddk_argument_wins(self):
        with mock.patch.dict(os.environ, {"ARTIFACT_ROOT": "/srv/v2v"}, clear=True):
            self.assertEqual(install_openvddk.install_dir("/opt/kit"), Path("/opt/kit"))

PLAYBOOK = """
- hosts: localhost
  connection: local
  gather_facts: false
  vars_files:
    - {paths}
  tasks:
    - ansible.builtin.copy:
        dest: {out}
        content: "{{{{ {{'artifact_root': artifact_root, 'vault_dir': vault_dir,
                     'catalog_dir': catalog_dir,
                     'ovftool_install_dir': ovftool_install_dir,
                     'openvddk_install_dir': openvddk_install_dir}} | to_json }}}}"
"""

@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is required")
class PlaybookPathTests(unittest.TestCase):

    def resolve(self, env, extra_vars=()):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, "paths.json")
            playbook = Path(tmp, "paths.yml")
            playbook.write_text(PLAYBOOK.format(
                paths=ROOT / "playbooks/vars/paths.yml", out=out))
            args = ["ansible-playbook", "-i", "localhost,", str(playbook)]
            for value in extra_vars:
                args += ["-e", value]
            base = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
            result = subprocess.run(args, env={**base, **env}, capture_output=True,
                                    text=True, check=False, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return {k: Path(v) for k, v in json.loads(out.read_text()).items()}

    def assert_matches_python(self, env, root):
        paths = self.resolve(env)
        self.assertEqual(paths.pop("artifact_root"), Path(root))
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(paths, python_paths())

    def test_default_root_matches_python_tools(self):
        self.assert_matches_python({}, "/work/migrate-v2v")

    def test_artifact_root_matches_python_tools(self):
        self.assert_matches_python({"ARTIFACT_ROOT": "/srv/v2v"}, "/srv/v2v")

    def test_own_variable_matches_python_tools(self):
        self.assert_matches_python(
            {"ARTIFACT_ROOT": "/srv/v2v", "MIGRATE_V2V_VAULT_DIR": "/elsewhere/vaults"},
            "/srv/v2v")

    def test_extra_var_moves_every_directory(self):
        paths = self.resolve({"ARTIFACT_ROOT": "/srv/v2v"}, ["artifact_root=/mnt/run"])
        for name, _, sub in DIRECTORIES:
            self.assertEqual(paths[name], Path("/mnt/run", sub), name)

class VaultHelperPathTests(unittest.TestCase):
    """edit-vault.sh resolves the same vault the Python tools read."""

    def vault_used(self, env):
        with tempfile.TemporaryDirectory() as tmp:
            stub_bin = Path(tmp, "bin")
            stub_bin.mkdir()
            record = Path(tmp, "argv")
            stub = stub_bin / "ansible-vault"
            stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\n')
            stub.chmod(0o755)
            run_env = {"PATH": f"{stub_bin}:/usr/bin:/bin", "HOME": tmp,
                       "DIB7_VENV_BIN": str(stub_bin),
                       "VAULT_PASSWORD_FILE": "/dev/null", **env}
            subprocess.run([str(ROOT / "bin/aws-vault.sh")], env=run_env,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           check=False, timeout=60)
            argv = record.read_text().split("\n") if record.exists() else []
            return next((Path(a) for a in argv if a.endswith("aws.yml")), None)

    def test_vault_helper_follows_artifact_root(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(self.vault_used({"ARTIFACT_ROOT": root}),
                             Path(root, "vaults/aws.yml"))
            other = Path(root, "other")
            self.assertEqual(
                self.vault_used({"ARTIFACT_ROOT": root, "MIGRATE_V2V_VAULT_DIR": str(other)}),
                other / "aws.yml")

if __name__ == "__main__":
    unittest.main()
