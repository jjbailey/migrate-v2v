#!/usr/bin/env python3
# tests/test_vault_credentials.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Credential order for the bin/ tools outside the playbooks.

An explicit flag, then exported credential variables, then the vault, then
cached logins; the vault is decrypted only when it is needed.

Nothing here decrypts a real vault or reaches a cloud: read_vault and the
SDK constructors are patched out.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import vault_common  # noqa: E402

def load_script(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def optional_script(filename, module_name):
    try:
        return load_script(filename, module_name)
    except ImportError:  # the cloud SDK is not installed
        return None

reconcile_aws = optional_script("reconcile-catalog-aws.py", "vc_reconcile_aws")
reconcile_gcp = optional_script("reconcile-catalog-gcp.py", "vc_reconcile_gcp")
reconcile_openstack = optional_script(
    "reconcile-catalog-openstack.py", "vc_reconcile_openstack")
enable_cbt = optional_script("enable-cbt.py", "vc_enable_cbt")

NO_VAULT = Path("/nonexistent/vault.yml")

def unused_vault():
    raise AssertionError("the vault was decrypted although the environment decided")

class CleanEnvironment(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

class VaultCommonTests(unittest.TestCase):

    def test_missing_vault_file_is_none(self):
        with mock.patch.object(vault_common, "read_vault") as read:
            self.assertIsNone(vault_common.vault_projects(
                Path("/nonexistent/vault.yml"), "aws_projects"))
        read.assert_not_called()

    def test_existing_vault_is_decrypted_and_keyed_by_string(self):
        with tempfile.NamedTemporaryFile() as vault, \
                mock.patch.object(vault_common, "read_vault",
                                  return_value={"aws_projects": {123: None}}):
            self.assertEqual(
                vault_common.vault_projects(vault.name, "aws_projects"), {"123": {}})

    def test_environment_source_notes_a_skipped_vault(self):
        with tempfile.NamedTemporaryFile(suffix=".yml") as vault:
            self.assertEqual(
                vault_common.environment_source("X environment", vault.name),
                f"X environment; {Path(vault.name).name} not consulted")
        self.assertEqual(vault_common.environment_source("X environment", NO_VAULT),
                         "X environment")

    def test_undecryptable_vault_stops_the_run(self):
        with tempfile.NamedTemporaryFile() as vault, \
                mock.patch.object(vault_common, "read_vault",
                                  side_effect=SystemExit("could not decrypt")):
            with self.assertRaises(SystemExit):
                vault_common.vault_projects(vault.name, "aws_projects")

    def test_match_project_by_key_or_field(self):
        projects = {"lab": {"project_id": "P-1"}, "other": {"project_id": "p-2"}}
        self.assertEqual(vault_common.match_project(projects, "lab", ("project_id",))[0], "lab")
        self.assertEqual(vault_common.match_project(projects, "p-1", ("project_id",))[0], "lab")
        self.assertEqual(
            vault_common.match_project(projects, {"x", "P-2"}, ("project_id",))[0], "other")
        self.assertEqual(vault_common.match_project(projects, "x", ("project_id",)),
                         (None, None))
        self.assertEqual(vault_common.match_project(None, "lab", ()), (None, None))
        self.assertEqual(vault_common.match_project(projects, None, ()), (None, None))

    def password_file_used(self, home, env=None):
        run = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout="{}"))
        with mock.patch.dict(os.environ, env or {}, clear=True), \
                mock.patch.object(vault_common.Path, "home", return_value=Path(home)), \
                mock.patch.object(vault_common, "ansible_vault_bin", return_value="ansible-vault"), \
                mock.patch.object(vault_common.subprocess, "run", run):
            vault_common.read_vault("v.yml")
        command = run.call_args.args[0]
        if "--vault-password-file" not in command:
            return None
        return command[command.index("--vault-password-file") + 1]

    def test_read_vault_password_file_order(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(self.password_file_used(home))
            legacy = Path(home, ".ssh/dib-vault-pass")
            legacy.parent.mkdir()
            legacy.write_text("x")
            self.assertEqual(self.password_file_used(home), str(legacy))
            default = Path(home, ".dib-vault-pass")
            default.write_text("x")
            self.assertEqual(self.password_file_used(home), str(default))
            explicit = Path(home, "explicit")
            explicit.write_text("x")
            self.assertEqual(
                self.password_file_used(home, {"VAULT_PASSWORD_FILE": str(explicit)}),
                str(explicit))

@unittest.skipIf(reconcile_aws is None, "needs boto3")
class AwsReconcileCredentialTests(CleanEnvironment):
    PROJECTS = {"lab": {"aws_account_id": "111", "aws_access_key_id": "AK",
                        "aws_secret_access_key": "SK", "aws_session_token": ""}}

    def session_for(self, account, load, profile=None):
        with mock.patch.object(reconcile_aws.boto3, "Session") as session:
            _, source = reconcile_aws.session_for(account, load, NO_VAULT, profile)
        return session, source

    def test_explicit_profile_wins(self):
        os.environ["AWS_PROFILE"] = "envprof"
        session, source = self.session_for("111", unused_vault, "mine")
        session.assert_called_once_with(profile_name="mine")
        self.assertEqual(source, "profile mine")

    def test_exported_variables_win_over_the_vault(self):
        for name in reconcile_aws.AWS_ENVIRONMENT:
            with mock.patch.dict(os.environ, {name: "x"}, clear=True):
                session, source = self.session_for("111", unused_vault)
            session.assert_called_once_with()
            self.assertEqual(source, f"{name} environment")

    def test_vault_keys_for_the_account(self):
        session, source = self.session_for("111", lambda: self.PROJECTS)
        session.assert_called_once_with(aws_access_key_id="AK",
                                        aws_secret_access_key="SK",
                                        aws_session_token=None)
        self.assertEqual(source, "vault entry 'lab'")

    def test_vault_profile_for_the_account(self):
        projects = {"lab": {"aws_account_id": "111", "aws_profile": "labprof"}}
        session, _ = self.session_for("111", lambda: projects)
        session.assert_called_once_with(profile_name="labprof")

    def test_vault_keys_win_over_the_vault_profile(self):
        # vars/aws-common.yml uses the profile only when there are no keys.
        projects = {"lab": {**self.PROJECTS["lab"], "aws_profile": "labprof"}}
        session, _ = self.session_for("111", lambda: projects)
        session.assert_called_once_with(aws_access_key_id="AK",
                                        aws_secret_access_key="SK",
                                        aws_session_token=None)

    def test_boto3_chain_last(self):
        for account, projects in (("222", self.PROJECTS), ("111", None), (None, self.PROJECTS)):
            session, source = self.session_for(account, lambda: projects)
            session.assert_called_once_with()
            self.assertEqual(source, "boto3 default chain")

    def test_entry_without_credentials_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "neither aws_profile"):
            reconcile_aws.session_for(
                "111", lambda: {"lab": {"aws_account_id": "111"}}, NO_VAULT)

@unittest.skipIf(reconcile_gcp is None, "needs google-auth")
class GcpReconcileCredentialTests(CleanEnvironment):
    KEY = '{"type": "service_account", "project_id": "proj-a", "client_email": "x@y"}'
    PROJECTS = {"lab": {"gcp_project": "proj-a", "service_account_key": KEY}}

    def test_exported_key_file_wins_over_the_vault(self):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/k.json"
        credentials = SimpleNamespace(project_id="proj-a")
        with mock.patch.object(reconcile_gcp.google.auth, "default",
                               return_value=(credentials, None)):
            _, project, source = reconcile_gcp.credentials_for(
                "proj-a", unused_vault, NO_VAULT)
        self.assertEqual((project, source), ("proj-a", "GOOGLE_APPLICATION_CREDENTIALS"))

    def test_vault_service_account_for_the_project(self):
        with mock.patch.object(reconcile_gcp.service_account.Credentials,
                               "from_service_account_info") as info, \
                mock.patch.object(reconcile_gcp.google.auth, "default") as default:
            _, project, source = reconcile_gcp.credentials_for(
                "proj-a", lambda: self.PROJECTS, NO_VAULT)
        self.assertEqual(info.call_args.args[0]["client_email"], "x@y")
        default.assert_not_called()
        self.assertEqual((project, source), ("proj-a", "vault entry 'lab'"))

    def test_google_defaults_last(self):
        credentials = SimpleNamespace(project_id=None)
        for projects in (self.PROJECTS, None):
            with mock.patch.object(reconcile_gcp.google.auth, "default",
                                   return_value=(credentials, "ambient")):
                _, project, source = reconcile_gcp.credentials_for(
                    "proj-b", lambda: projects, NO_VAULT)
            self.assertIsNone(project)
            self.assertEqual(source, "Google application default credentials")

    def test_bad_key_is_an_error(self):
        projects = {"lab": {"gcp_project": "proj-a", "service_account_key": "{"}}
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            reconcile_gcp.credentials_for("proj-a", lambda: projects, NO_VAULT)

@unittest.skipIf(reconcile_openstack is None, "needs openstacksdk")
class OpenStackReconcileCredentialTests(CleanEnvironment):
    PROJECTS = {"9999": {"auth_url": "https://keystone/v3", "username": "u",
                         "password": "p", "project_id": 9999,
                         "project_name": "lab", "region_name": "R0"}}

    def connect_for(self, load, wanted, cloud=None):
        with mock.patch.object(reconcile_openstack.openstack, "connect") as connect:
            _, source = reconcile_openstack.connect_for(
                load, NO_VAULT, wanted, "R1", cloud)
        return connect, source

    def test_explicit_cloud_wins(self):
        os.environ["OS_CLOUD"] = "envcloud"
        connect, _ = self.connect_for(unused_vault, {"lab"}, "mycloud")
        connect.assert_called_once_with(cloud="mycloud", region_name="R1")

    def test_exported_variables_win_over_the_vault(self):
        os.environ.update(OS_AUTH_URL="a", OS_USERNAME="u", OS_PASSWORD="p",
                          OS_PROJECT_NAME="lab")
        connect, source = self.connect_for(unused_vault, {"lab"})
        connect.assert_called_once_with(region_name="R1")
        self.assertEqual(source, "OS_* environment")

    def test_incomplete_exported_variables_are_an_error(self):
        os.environ["OS_AUTH_URL"] = "a"
        with self.assertRaisesRegex(ValueError, "missing required OpenStack"):
            self.connect_for(unused_vault, {"lab"})

    def test_vault_entry_alone_without_the_environment(self):
        connect, source = self.connect_for(lambda: self.PROJECTS, {"lab"})
        connect.assert_called_once_with(
            load_envvars=False, load_yaml_config=False,
            auth_url="https://keystone/v3", username="u", password="p",
            project_id="9999", project_name="lab", region_name="R1")
        self.assertEqual(source, "vault entry '9999'")

    def test_nothing_found_is_an_error(self):
        for projects in (None, self.PROJECTS):
            with self.assertRaisesRegex(ValueError, "no OpenStack credentials"):
                self.connect_for(lambda: projects, {"other"})

@unittest.skipIf(enable_cbt is None, "needs pyVmomi")
class EnableCbtCredentialTests(unittest.TestCase):
    DATA = {"vsphere_projects": {"site-a": {"vcenter_hostname": "vc1",
                                         "vcenter_username": "u",
                                         "vcenter_password": "p"}}}

    def select(self, data, requested, vault=None):
        with tempfile.NamedTemporaryFile() as existing, \
                mock.patch.object(enable_cbt, "read_vault", return_value=data):
            return enable_cbt.vault_vcenter(vault or existing.name, requested)[0]

    def test_selection_mirrors_select_vcenter(self):
        legacy = dict(self.DATA, vcenter_hostname="flat")
        self.assertEqual(self.select(self.DATA, None), "site-a")
        self.assertEqual(self.select(self.DATA, "vc1"), "site-a")
        self.assertEqual(self.select(self.DATA, "site-a"), "site-a")
        self.assertEqual(self.select(legacy, None), "legacy")
        self.assertEqual(self.select(legacy, "flat"), "legacy")

    def test_no_entry_falls_back_to_the_environment(self):
        self.assertIsNone(self.select(self.DATA, "elsewhere"))
        self.assertIsNone(self.select({}, None))
        self.assertIsNone(self.select(self.DATA, None, vault="/nonexistent/v.yml"))

if __name__ == "__main__":
    unittest.main()
