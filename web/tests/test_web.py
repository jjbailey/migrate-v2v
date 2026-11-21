#!/usr/bin/env python3
# web/tests/test_web.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

import io
import hashlib
import json
import os
import tempfile
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from web import app, facts, forms

class DialogContractTests(unittest.TestCase):
    def test_dialog_edits_require_form_review(self):
        checksums = json.loads((Path(__file__).parents[1] / "dialog-checksums.json").read_text())
        for name, expected in checksums.items():
            with self.subTest(dialog=name):
                actual = hashlib.sha256((forms.BIN / name).read_bytes()).hexdigest()
                self.assertEqual(expected, actual, f"Review web form metadata after editing {name}, then update its checksum")

    def test_every_dialog_has_web_fields_and_wrapper(self):
        for cloud in forms.CLOUDS:
            for mode in ("migrate", "launch"):
                methods = ("standard", "vddk") if mode == "migrate" and cloud in ("azure", "openstack") else ("standard",)
                for method in methods:
                    with self.subTest(mode=mode, cloud=cloud, method=method):
                        fields = forms.exported_fields(mode, cloud, method)
                        self.assertIn("VM_NAME", fields)
                        self.assertIn(app.target_key(cloud), fields)
                        self.assertTrue(forms.wrapper_path(mode, cloud, method).is_file())
                        if method == "vddk":
                            self.assertIn("BACKEND", fields)
                        if cloud == "openstack" and method == "vddk":
                            self.assertIn("COMPRESS", fields)

    @patch.object(app, "targets", return_value=["site"])
    @patch.object(app, "vault_keys", return_value=["source"])
    def test_migration_selection_uses_vault_entries(self, _keys, _targets):
        values = {name: forms.F[name].default for name in forms.exported_fields("migrate", "aws")}
        values.update(VM_NAME="vm1", AWS_TARGET_PROJECT="other", VSPHERE_TARGET_VCENTER="source")
        _, errors = app.validate("migrate", "aws", "standard", values)
        self.assertTrue(any("environment" in error for error in errors))
        values["AWS_TARGET_PROJECT"] = "site"
        _, errors = app.validate("migrate", "aws", "standard", values)
        self.assertFalse(errors)

class HttpTests(unittest.TestCase):
    def test_migrate_requires_destination_project_first(self):
        with patch.object(app, "targets", return_value=["site"]), \
             patch.object(app, "vault_keys", return_value=["source"]), \
             patch.object(app, "launch_choices") as lookup:
            picker = app.form_html("migrate", "openstack", "vddk", "token", user="operator")
            self.assertIn("Choose a project", picker)
            self.assertNotIn("QUIESCE_SNAPSHOT", picker)
            form = app.form_html("migrate", "openstack", "vddk", "token",
                                 posted={"OS_PROJECT_NAME": "site"}, user="operator")
            self.assertIn('name="OS_PROJECT_NAME" value="site"', form)
            self.assertIn('name="VSPHERE_TARGET_VCENTER"', form)
            self.assertIn('name="BACKEND"', form)
            self.assertIn("Change project", form)
            lookup.assert_not_called()

    def test_launch_requires_project_before_looking_up_choices(self):
        with patch.object(app, "targets", return_value=["site"]), patch.object(app, "launch_choices") as lookup:
            picker = app.form_html("launch", "openstack", "standard", "token", user="operator")
            self.assertIn("Choose a project", picker)
            self.assertNotIn("OS_FLAVOR", picker)
            lookup.assert_not_called()
            lookup.return_value = ({"OS_FLAVOR": [("small", "small · 2 vCPU")],
                                    "OS_SECURITY_GROUPS_JSON": [("default", "default · owned")]}, {}, [])
            form = app.form_html("launch", "openstack", "standard", "token",
                                 posted={"OS_PROJECT_NAME": "site"}, user="operator")
            self.assertIn("OS_FLAVOR", form)
            self.assertIn('value="small"', form)
            self.assertIn('name="_pick_OS_SECURITY_GROUPS_JSON"', form)
            lookup.assert_called_once()

    def test_azure_public_ip_defaults_to_yes(self):
        with patch.object(app, "targets", return_value=["site"]), \
             patch.object(app, "launch_choices", return_value=({}, {}, [])), \
             patch.object(app, "catalog_vms", return_value=[]):
            form = app.form_html("launch", "azure", "standard", "token",
                                 posted={"AZURE_TARGET_PROJECT": "site"}, user="operator")
        self.assertIn('<label for="AZURE_NO_PUBLIC_IP">Public IP</label>', form)
        self.assertIn('<option value="false" selected>Yes</option><option value="true">No</option>', form)

    def test_aws_choices_follow_selected_subnet(self):
        calls = []
        def query(cloud, project, *flags):
            calls.append(flags)
            if flags == ("--subnets",):
                return {"subnets": [{"id": "subnet-one", "state": "available", "name": "One"}]}
            if flags == ("--keypairs",):
                return {"keypairs": [{"name": "cloud"}]}
            if flags == ("--subnet", "subnet-one"):
                return {"security_groups": [{"id": "sg-one", "name": "default"}]}
            return None
        with patch.object(facts, "query", side_effect=query):
            choices, defaults, missing = facts.choices("aws", "site", {})
        self.assertEqual(defaults["AWS_SUBNET_ID"], "subnet-one")
        self.assertEqual(choices["AWS_SECURITY_GROUPS_JSON"][0][0], "sg-one")
        self.assertIn(("--subnet", "subnet-one"), calls)
        self.assertFalse(missing)

    def test_gcp_choices_follow_zone_and_network(self):
        calls = []
        def query(cloud, project, *flags):
            calls.append(flags)
            if flags == ("--zones",):
                return {"zones": [{"name": "zone-a"}]}
            if flags == ("--networks",):
                return {"networks": [{"name": "private"}]}
            if flags == ("--machine-types", "--zone", "zone-a"):
                return {"machine_types": [{"name": "medium"}]}
            if flags == ("--subnetworks", "--zone", "zone-a", "--network", "private"):
                return {"subnetworks": [{"name": "subnet-a"}]}
            return None
        with patch.object(facts, "query", side_effect=query):
            found, defaults, missing = facts.choices("gcp", "site", {"GCP_ZONE": "zone-a", "GCP_NETWORK": "private"})
            _, cleared_defaults, _ = facts.choices("gcp", "site", {"GCP_ZONE": "zone-a", "GCP_NETWORK": "private", "GCP_SUBNETWORK": ""})
        self.assertEqual(found["GCP_MACHINE_TYPE"][0][0], "medium")
        self.assertEqual(found["GCP_SUBNETWORK"][0][0], "subnet-a")
        self.assertEqual(defaults["GCP_SUBNETWORK"], "subnet-a")
        self.assertNotIn("GCP_SUBNETWORK", cleared_defaults)
        self.assertFalse(missing)

    def test_openstack_project_choices(self):
        data = {"flavors": [{"name": "small"}], "networks": [{"name": "internal"}],
                "keypairs": [{"name": "cloud"}],
                "security_groups": [{"name": "default", "id": "group-id"}]}
        with patch.object(facts, "query", return_value=data):
            found, _, missing = facts.choices("openstack", "site", {})
        self.assertEqual(found["OS_FLAVOR"][0][0], "small")
        self.assertEqual(found["OS_SECURITY_GROUPS_JSON"][0][0], "default")
        self.assertEqual(found["OS_SECURITY_GROUP_IDS_JSON"][0][0], "group-id")
        self.assertFalse(missing)

    def test_selected_security_groups_become_wrapper_json(self):
        posted = {name: forms.F[name].default for name in forms.exported_fields("launch", "aws")}
        posted.update(AWS_TARGET_PROJECT="site", VM_NAME="vm1", AWS_SUBNET_ID="subnet-one")
        posted["_pick_AWS_SECURITY_GROUPS_JSON"] = ["sg-one", "sg-two"]
        available = {"AWS_SECURITY_GROUPS_JSON": [("sg-one", "One"), ("sg-two", "Two")]}
        with patch.object(app, "targets", return_value=["site"]), \
             patch.object(app, "catalog_vms", return_value=["vm1"]), \
             patch.object(app, "launch_choices", return_value=(available, {}, [])):
            values, errors = app.validate("launch", "aws", "standard", posted)
        self.assertFalse(errors)
        self.assertEqual(json.loads(values["AWS_SECURITY_GROUPS_JSON"]), ["sg-one", "sg-two"])

    def test_login_and_pages(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            # init_state() creates INPUT_DIR and sets ANSIBLE_LOCAL_TEMP: keep
            # both inside the temp dir and restore the environment afterwards.
            with patch.multiple(app, STATE=state, USERS=state / "users.json", KEY=state / "session.key",
                                DB=state / "jobs.sqlite3", INPUT_DIR=state / "web-inputs",
                                VAULT_DIR=state / "vaults", CATALOG_DIR=state / "catalogs"), \
                 patch.dict(os.environ):
                app.init_state()
                stored = app.password_hash("sample-password-123")
                app.USERS.write_text(json.dumps({"operator": stored}) + "\n")
                handler = object.__new__(app.Handler)
                handler.headers = Message()
                handler.wfile = io.BytesIO()
                handler.send_response = lambda status: None
                handler.send_header = lambda key, value: None
                handler.end_headers = lambda: None
                handler.path = "/login"
                handler.do_GET()
                self.assertIn("Sign in", handler.wfile.getvalue().decode())
                csrf = "test-csrf"
                session = app.sign({"user": "operator", "auth": hashlib.sha256(stored.encode()).hexdigest(),
                                    "csrf": csrf, "exp": int(time.time()) + 600})
                handler.headers["Cookie"] = "session=" + session
                handler.wfile = io.BytesIO()
                handler.path = "/"
                handler.do_GET()
                self.assertIn("Move with clarity", handler.wfile.getvalue().decode())
                handler.wfile = io.BytesIO()
                handler.path = "/new/migrate/aws/standard"
                handler.do_GET()
                markup = handler.wfile.getvalue().decode()
                self.assertIn("Choose a project", markup)
                with patch.object(app, "targets", return_value=["site"]), \
                     patch.object(app, "vault_keys", return_value=["source"]):
                    handler.wfile = io.BytesIO()
                    handler.path = "/new/migrate/aws/standard?project=site"
                    handler.do_GET()
                    markup = handler.wfile.getvalue().decode()
                    self.assertIn("Quiesce snapshot", markup)
                    self.assertIn("Queue migrate", markup)
                with patch.object(app, "targets", return_value=["site"]), \
                     patch.object(app, "launch_choices", return_value=({}, {}, [])), \
                     patch.object(app, "catalog_vms", return_value=["vm1"]):
                    handler.wfile = io.BytesIO()
                    handler.path = "/new/launch/aws/standard"
                    handler.do_GET()
                    self.assertIn("Choose a project", handler.wfile.getvalue().decode())
                    handler.wfile = io.BytesIO()
                    handler.path = "/new/launch/aws/standard?project=site"
                    handler.do_GET()
                    self.assertIn("Queue launch", handler.wfile.getvalue().decode())
                job_id = app.enqueue("operator", "migrate", "aws", "standard",
                                     {"VM_NAME": "vm1", "AWS_TARGET_PROJECT": "site"})
                with app.db() as conn:
                    conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
                (state / f"job-{job_id}.log").write_text("Migration is progressing\n")
                handler.wfile = io.BytesIO()
                handler.path = f"/jobs/{job_id}"
                handler.do_GET()
                self.assertIn("Migration is progressing", handler.wfile.getvalue().decode())

if __name__ == "__main__":
    unittest.main()
