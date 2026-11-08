#!/usr/bin/env python3
# tests/test_azure_project_facts.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Tests for bin/azure-project-facts.py, using fake vault and client objects."""

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "azure-project-facts.py"
spec = importlib.util.spec_from_file_location("azure_project_facts", SCRIPT)
facts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(facts)

# ssh-keygen -lf prints SHA256:cx83l9iEWgdSBAEFPhNn4k9xlaUhVOL1vdGJHfY5sXw
# for this key.
ED25519 = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKHYuBi1NfnAOZ6cQtId+wRIij"
           "/lgVVG1bjkjVjWlO17 cloud")
ED25519_FP = "SHA256:cx83l9iEWgdSBAEFPhNn4k9xlaUhVOL1vdGJHfY5sXw"

ENTRY = {
    "tenant_id": "t", "subscription_id": "sub-a", "client_id": "c",
    "client_secret": "s", "resource_group": "rg-a", "location": "westus2",
    "storage_account": "acct",
}

def key(name, public_key=ED25519, location="westus2"):
    return SimpleNamespace(name=name, public_key=public_key,
                           location=location, id=f"/x/sshPublicKeys/{name}")

class FakeClient:
    def __init__(self, keys):
        self.keys = keys
        self.groups = []
        self.ssh_public_keys = self

    def list_by_resource_group(self, resource_group):
        self.groups.append(resource_group)
        return iter(self.keys)

class FingerprintTests(unittest.TestCase):
    def test_matches_ssh_keygen(self):
        self.assertEqual(facts.key_fingerprint(ED25519),
                         ("ssh-ed25519", ED25519_FP))

    def test_unparseable_keys_give_blanks(self):
        self.assertEqual(facts.key_fingerprint(""), ("", ""))
        self.assertEqual(facts.key_fingerprint("ssh-rsa not*base64"),
                         ("ssh-rsa", ""))

class VaultSelectionTests(unittest.TestCase):
    def select(self, projects, requested):
        with mock.patch.object(facts, "read_vault",
                               return_value={"azure_projects": projects}):
            return facts.select_vault_entry("vault.yml", requested)

    def test_matches_key_or_subscription_id(self):
        projects = {"a": ENTRY, "b": {**ENTRY, "subscription_id": "sub-b"}}
        self.assertEqual(self.select(projects, "b")[0], "b")
        self.assertEqual(self.select(projects, "sub-b")[0], "b")

    def test_single_entry_is_auto_selected(self):
        self.assertEqual(self.select({"a": ENTRY}, None)[0], "a")

    def test_several_entries_need_a_name(self):
        with self.assertRaises(SystemExit):
            self.select({"a": ENTRY, "b": ENTRY}, None)

    def test_unknown_name_fails(self):
        with self.assertRaises(SystemExit):
            self.select({"a": ENTRY}, "nope")

    def test_missing_credentials_fail(self):
        with self.assertRaises(SystemExit):
            facts.require_entry("a", {**ENTRY, "client_secret": ""})

class KeypairTests(unittest.TestCase):
    def test_lists_sorted_keys_with_fingerprints(self):
        client = FakeClient([key("zeta"), key("cloud")])
        pairs = facts.gather_keypairs(client, "rg-a")
        self.assertEqual(client.groups, ["rg-a"])
        self.assertEqual([pair["name"] for pair in pairs], ["cloud", "zeta"])
        self.assertEqual(pairs[0]["fingerprint"], ED25519_FP)
        self.assertEqual(pairs[0]["type"], "ssh-ed25519")

    def run_main(self, argv, client):
        out = io.StringIO()
        with mock.patch.object(facts, "read_vault",
                               return_value={"azure_projects": {"a": ENTRY}}), \
                mock.patch.object(facts, "make_client", return_value=client), \
                contextlib.redirect_stdout(out):
            self.assertEqual(facts.main(argv), 0)
        return out.getvalue()

    def test_json_output(self):
        client = FakeClient([key("cloud")])
        data = json.loads(self.run_main(["--keypairs", "--json"], client))
        self.assertEqual(data["resource_group"], "rg-a")
        self.assertEqual(data["subscription_id"], "sub-a")
        self.assertEqual(data["keypairs"][0]["name"], "cloud")

    def test_resource_group_override(self):
        client = FakeClient([])
        text = self.run_main(["a", "--resource-group", "rg-other"], client)
        self.assertEqual(client.groups, ["rg-other"])
        self.assertIn("resource group rg-other): 0", text)

if __name__ == "__main__":
    unittest.main()
