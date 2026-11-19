#!/usr/bin/env python3
# web/tests/test_web_server.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Request handling, validation, the job worker and caches of web/app.py.

Every test runs against a private temporary state, input, vault and catalog
directory, with os.environ restored afterwards, so nothing touches
/work/migrate-v2v or the operator's real vaults. Requests go through
Handler.do_GET/do_POST directly, with the socket replaced by buffers.
"""

import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from web import app, facts, forms

PASSWORD = "sample-password-123"

class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def header(self, name):
        return next((value for key, value in self.headers if key.lower() == name.lower()), None)

class IsolatedState(unittest.TestCase):
    """A fresh controller state per test, with one account: operator."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "web-state"
        self.inputs = self.root / "web-inputs"
        self.vaults = self.root / "vaults"
        self.catalogs = self.root / "catalogs"
        self.vaults.mkdir()
        self.catalogs.mkdir()
        self.passfile = self.root / "vault-pass"
        self.passfile.write_text("secret\n")
        for patcher in (
            patch.multiple(app, STATE=self.state, USERS=self.state / "users.json",
                           KEY=self.state / "session.key", DB=self.state / "jobs.sqlite3",
                           INPUT_DIR=self.inputs, VAULT_DIR=self.vaults,
                           CATALOG_DIR=self.catalogs, PASSFILE=self.passfile,
                           ARTIFACT_ROOT=self.root),
            patch.dict(os.environ, {"WEB_SECURE_COOKIE": "true"}),
            patch.dict(app.LOGIN_FAILURES, clear=True),
            patch.dict(app.VAULT_CACHE, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        app.STOP_EVENT.clear()
        self.addCleanup(app.STOP_EVENT.clear)
        app.init_state()
        self.stored = app.password_hash(PASSWORD)
        app.save_users({"operator": self.stored})

    # -- requests -----------------------------------------------------------

    def request(self, method, path, form=None, cookie=None, body=None, headers=None):
        handler = object.__new__(app.Handler)
        handler.headers = Message()
        for key, value in (headers or {}).items():
            handler.headers[key] = value
        if cookie is not None:
            handler.headers["Cookie"] = "session=" + cookie
        handler.client_address = ("127.0.0.1", 50000)
        handler.path = path
        handler.wfile = io.BytesIO()
        sent = []
        status = []
        handler.send_response = status.append
        handler.send_header = lambda key, value: sent.append((key, value))
        handler.end_headers = lambda: None
        if method == "POST":
            data = body if body is not None else urlencode(form or {}, doseq=True).encode()
            handler.rfile = io.BytesIO(data)
            if "Content-Length" not in handler.headers:
                handler.headers["Content-Length"] = str(len(data))
            handler.do_POST()
        else:
            handler.do_GET()
        return Response(status[0], sent, handler.wfile.getvalue().decode(errors="replace"))

    def session(self, user="operator", stored=None, csrf="test-csrf", expires_in=600):
        stored = stored or self.stored
        return app.sign({"user": user, "auth": hashlib.sha256(stored.encode()).hexdigest(),
                         "csrf": csrf, "exp": int(time.time()) + expires_in})

    def login_page_session(self):
        """The pre-auth cookie and CSRF token GET /login hands out."""
        response = self.request("GET", "/login")
        token = response.header("Set-Cookie").split(";", 1)[0].split("=", 1)[1]
        return token, app.unsign(token)["csrf"]

    def post_login(self, username, password, ip="192.0.2.1"):
        token, csrf = self.login_page_session()
        return self.request("POST", "/login", {"_csrf": csrf, "username": username,
                                               "password": password},
                            cookie=token, headers={"X-Forwarded-For": ip})

class CsrfAndBodyTests(IsolatedState):

    def test_post_without_a_session_is_rejected(self):
        response = self.request("POST", "/logout", {"_csrf": "anything"})
        self.assertEqual(response.status, 403)

    def test_post_with_the_wrong_token_is_rejected(self):
        response = self.request("POST", "/logout", {"_csrf": "wrong"}, cookie=self.session())
        self.assertEqual(response.status, 403)
        self.assertIn("The form expired", response.body)

    def test_post_with_a_missing_or_non_ascii_token_is_rejected(self):
        cookie = self.session(csrf="test-csrf")
        self.assertEqual(self.request("POST", "/logout", {"x": "1"}, cookie=cookie).status, 403)
        self.assertEqual(self.request("POST", "/logout", {"_csrf": "tést-csrf"},
                                      cookie=cookie).status, 403)

    def test_csrf_is_checked_before_any_route(self):
        # A forged queue request never reaches validation or the job table.
        with patch.object(app, "validate") as validate, patch.object(app, "enqueue") as enqueue:
            response = self.request("POST", "/new/migrate/aws/standard",
                                    {"_csrf": "forged", "VM_NAME": "vm1"},
                                    cookie=self.session())
        self.assertEqual(response.status, 403)
        validate.assert_not_called()
        enqueue.assert_not_called()

    def test_body_size_and_encoding_limits(self):
        cookie = self.session()
        self.assertEqual(self.request("POST", "/logout", body=b"", cookie=cookie).status, 413)
        big = b"_csrf=test-csrf&x=" + b"a" * app.MAX_BODY
        self.assertEqual(self.request("POST", "/logout", body=big, cookie=cookie).status, 413)
        self.assertEqual(self.request("POST", "/logout", body=b"_csrf=\xff\xfe",
                                      cookie=cookie).status, 400)
        self.assertEqual(self.request("POST", "/logout", body=b"x=1", cookie=cookie,
                                      headers={"Content-Length": "many"}).status, 400)

class LoginTests(IsolatedState):

    def test_login_page_sets_a_secure_short_lived_cookie(self):
        cookie = self.request("GET", "/login").header("Set-Cookie")
        for flag in ("HttpOnly", "SameSite=Strict", "Secure", "Max-Age=600"):
            self.assertIn(flag, cookie)

    def test_second_login_page_keeps_first_form_valid(self):
        token, csrf = self.login_page_session()
        second = self.request("GET", "/login", cookie=token)
        self.assertEqual(second.status, 200)
        self.assertIsNone(second.header("Set-Cookie"))
        self.assertIn(f'value="{csrf}"', second.body)
        response = self.request("POST", "/login",
                                {"_csrf": csrf, "username": "nobody",
                                 "password": "invalid"}, cookie=token)
        self.assertEqual(response.status, 401)
        self.assertNotIn("The form expired", response.body)

    def test_valid_login_issues_a_session(self):
        response = self.post_login("operator", PASSWORD)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.header("Location"), "/")
        token = response.header("Set-Cookie").split(";", 1)[0].split("=", 1)[1]
        self.assertEqual(app.unsign(token)["user"], "operator")
        self.assertEqual(self.request("GET", "/", cookie=token).status, 200)

    def test_wrong_password_and_unknown_user_both_fail_the_same_way(self):
        wrong = self.post_login("operator", "not-the-password")
        unknown = self.post_login("nobody", PASSWORD)
        self.assertEqual((wrong.status, unknown.status), (401, 401))
        self.assertEqual(wrong.body, unknown.body)

    def test_unknown_user_still_runs_the_password_hash(self):
        # Skipping scrypt for unknown names would reveal which names exist.
        with patch.object(app, "verify_password", return_value=False) as verify:
            self.post_login("nobody", PASSWORD)
        verify.assert_called_once_with(PASSWORD, app.DUMMY_PASSWORD_HASH)

    def test_ten_failures_lock_that_user_from_that_address(self):
        for _ in range(10):
            self.assertEqual(self.post_login("operator", "bad", ip="192.0.2.7").status, 401)
        self.assertEqual(self.post_login("operator", PASSWORD, ip="192.0.2.7").status, 429)
        # Another address, or another user, is not locked out.
        self.assertEqual(self.post_login("operator", PASSWORD, ip="192.0.2.8").status, 303)
        self.assertEqual(self.post_login("someone", "bad", ip="192.0.2.7").status, 401)

    def test_the_limit_holds_for_parallel_attempts(self):
        # Every attempt is counted before the slow password check, so a burst
        # of parallel requests cannot all slip under the limit.
        results = []
        def slow_verify(password, encoded):
            time.sleep(0.05)
            return False
        with patch.object(app, "verify_password", side_effect=slow_verify):
            threads = [threading.Thread(target=lambda: results.append(
                self.post_login("operator", "bad", ip="192.0.2.9").status)) for _ in range(15)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(results.count(401), 10)
        self.assertEqual(results.count(429), 5)

    def test_success_clears_earlier_failures(self):
        for _ in range(9):
            self.post_login("operator", "bad", ip="192.0.2.10")
        self.assertEqual(self.post_login("operator", PASSWORD, ip="192.0.2.10").status, 303)
        self.assertNotIn(("192.0.2.10", "operator"), app.LOGIN_FAILURES)

    def test_logout_expires_the_cookie(self):
        response = self.request("POST", "/logout", {"_csrf": "test-csrf"}, cookie=self.session())
        self.assertEqual(response.status, 303)
        self.assertEqual(response.header("Location"), "/login")
        self.assertIn("Max-Age=0", response.header("Set-Cookie"))

class SessionTokenTests(IsolatedState):

    def assert_signed_out(self, cookie):
        response = self.request("GET", "/", cookie=cookie)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.header("Location"), "/login")

    def test_valid_session_is_accepted(self):
        self.assertEqual(self.request("GET", "/", cookie=self.session()).status, 200)

    def test_tampered_payload_is_rejected(self):
        raw, mac = self.session(user="operator").rsplit(".", 1)
        other_raw = self.session(user="admin").rsplit(".", 1)[0]
        self.assert_signed_out(f"{other_raw}.{mac}")
        self.assert_signed_out(f"{raw}.{'0' * len(mac)}")
        self.assert_signed_out("not-a-token")

    def test_expired_session_is_rejected(self):
        self.assert_signed_out(self.session(expires_in=-1))

    def test_password_change_revokes_existing_sessions(self):
        cookie = self.session()
        app.save_users({"operator": app.password_hash("a-new-password-456")})
        self.assert_signed_out(cookie)

    def test_deleted_account_is_signed_out(self):
        cookie = self.session()
        app.save_users({})
        self.assert_signed_out(cookie)

class JobPageTests(IsolatedState):

    def test_out_of_range_job_ids_are_not_found(self):
        cookie = self.session()
        self.assertEqual(self.request("GET", "/jobs/999", cookie=cookie).status, 404)
        # Larger than SQLite's 64-bit integers: used to raise OverflowError.
        self.assertEqual(self.request("GET", "/jobs/" + "9" * 20, cookie=cookie).status, 404)

    def test_overview_icons_have_no_duplicate_ids(self):
        body = self.request("GET", "/", cookie=self.session()).body
        self.assertNotIn('id="aws"', body)
        self.assertEqual(body.count("<svg"), 8)

    def test_clear_finished_jobs_keeps_queued_and_running(self):
        ids = {status: app.enqueue("operator", "migrate", "aws", "standard",
                                   {"VM_NAME": f"vm-{status}", "AWS_TARGET_PROJECT": "site"})
               for status in ("queued", "running", "completed", "failed", "interrupted")}
        with app.db() as conn:
            for status, job_id in ids.items():
                conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        for job_id in ids.values():
            (self.state / f"job-{job_id}.log").write_text("output\n")
        cookie = self.session()
        unconfirmed = self.request("POST", "/maintenance/jobs/clear", {"_csrf": "test-csrf"},
                                   cookie=cookie)
        self.assertEqual(unconfirmed.status, 400)
        response = self.request("POST", "/maintenance/jobs/clear",
                                {"_csrf": "test-csrf", "confirm": "yes"}, cookie=cookie)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.header("Location"), "/maintenance/jobs?cleared=3&log_errors=0")
        with app.db() as conn:
            left = {row["status"] for row in conn.execute("SELECT status FROM jobs")}
        self.assertEqual(left, {"queued", "running"})
        self.assertTrue((self.state / f"job-{ids['running']}.log").exists())
        self.assertFalse((self.state / f"job-{ids['failed']}.log").exists())

class InputPathTests(IsolatedState):

    def setUp(self):
        super().setUp()
        self.good = self.inputs / "user-data.yaml"
        self.good.write_text("#cloud-config\n")

    def test_file_under_the_input_dir_is_accepted(self):
        self.assertEqual(app.checked_input_path(str(self.good)), self.good.resolve())

    def test_relative_missing_and_outside_paths_are_rejected(self):
        outside = self.root / "elsewhere.yaml"
        outside.write_text("x")
        for value in ("user-data.yaml", str(self.inputs / "missing"), str(outside),
                      str(self.inputs / ".." / "elsewhere.yaml"), str(self.inputs), ""):
            with self.subTest(value=value):
                self.assertIsNone(app.checked_input_path(value))

    def test_symlinks_to_secrets_are_rejected(self):
        for name, target in (("pass", self.passfile), ("key", self.state / "session.key"),
                             ("users", self.state / "users.json")):
            link = self.inputs / name
            link.symlink_to(target)
            with self.subTest(target=target.name):
                self.assertIsNone(app.checked_input_path(str(link)))

    def test_private_dirs_inside_the_input_dir_are_still_rejected(self):
        # Even a deployment that nests state or vaults under WEB_INPUT_DIR
        # must not expose them.
        nested_vaults = self.inputs / "vaults"
        nested_vaults.mkdir()
        secret = nested_vaults / "aws.yml"
        secret.write_text("x")
        with patch.object(app, "VAULT_DIR", nested_vaults):
            self.assertIsNone(app.checked_input_path(str(secret)))
        nested_pass = self.inputs / "vault-pass"
        nested_pass.write_text("x")
        with patch.object(app, "PASSFILE", nested_pass):
            self.assertIsNone(app.checked_input_path(str(nested_pass)))

class ValidateTests(IsolatedState):

    def launch(self, cloud, **values):
        posted = {name: forms.F[name].default for name in forms.exported_fields("launch", cloud)}
        posted.update({app.target_key(cloud): "site", "VM_NAME": "vm1"}, **values)
        with patch.object(app, "targets", return_value=["site"]), \
             patch.object(app, "catalog_vms", return_value=["vm1"]), \
             patch.object(app, "launch_choices", return_value=({}, {}, [])):
            return app.validate("launch", cloud, "standard", posted)

    def migrate(self, cloud, method="standard", confirm=None, **values):
        posted = {name: forms.F[name].default
                  for name in forms.exported_fields("migrate", cloud, method)}
        posted.update({app.target_key(cloud): "site", "VM_NAME": "vm1",
                       "VSPHERE_TARGET_VCENTER": "vc"}, **values)
        if confirm:
            posted["_confirm_discard"] = confirm
        with patch.object(app, "targets", return_value=["site"]), \
             patch.object(app, "vault_keys", return_value=["vc"]):
            return app.validate("migrate", cloud, method, posted)

    def test_baseline_forms_are_valid(self):
        self.assertEqual(self.launch("openstack", OS_FLAVOR="small", OS_NETWORK="net")[1], [])
        self.assertEqual(self.migrate("openstack", "vddk")[1], [])

    def test_unknown_project_and_vm_are_rejected(self):
        _, errors = self.launch("aws", AWS_TARGET_PROJECT="other", AWS_SUBNET_ID="subnet-1")
        self.assertIn("Select an environment from the vault.", errors)
        _, errors = self.launch("aws", VM_NAME="not-migrated", AWS_SUBNET_ID="subnet-1")
        self.assertIn("Select a VM with a completed migration in the catalog.", errors)
        _, errors = self.migrate("aws", VSPHERE_TARGET_VCENTER="unknown-vc")
        self.assertIn("Select a source vCenter from the vault.", errors)

    def test_field_kinds_are_enforced(self):
        _, errors = self.migrate("aws", QUIESCE_SNAPSHOT="maybe")
        self.assertIn("Choose a value for Quiesce snapshot.", errors)
        _, errors = self.migrate("openstack", "vddk", BACKEND="ftp")
        self.assertIn("Choose a valid VDDK transport.", errors)
        _, errors = self.launch("aws", AWS_SUBNET_ID="")
        self.assertIn("Subnet ID is required.", errors)
        _, errors = self.launch("aws", AWS_SUBNET_ID="s", AWS_SECURITY_GROUPS_JSON='{"a": 1}')
        self.assertTrue(any("must be a JSON array" in error for error in errors), errors)
        _, errors = self.launch("aws", AWS_SUBNET_ID="s", AWS_SECURITY_GROUPS_JSON="[1]")
        self.assertTrue(any("must be a JSON array" in error for error in errors), errors)
        _, errors = self.launch("aws", AWS_SUBNET_ID="x" * (app.MAX_FIELD + 1))
        self.assertIn("Subnet ID is too long.", errors)

    def test_path_fields_must_be_under_the_input_dir(self):
        _, errors = self.launch("openstack", OS_FLAVOR="small", OS_NETWORK="net",
                                OS_USER_DATA_FILE=str(self.passfile))
        self.assertTrue(any("must name a readable file under" in error for error in errors), errors)
        good = self.inputs / "init.yaml"
        good.write_text("#cloud-config\n")
        values, errors = self.launch("openstack", OS_FLAVOR="small", OS_NETWORK="net",
                                     OS_USER_DATA_FILE=str(good))
        self.assertEqual(errors, [])
        self.assertEqual(values["OS_USER_DATA_FILE"], str(good.resolve()))

    def test_discard_run_needs_digits_and_confirmation(self):
        _, errors = self.migrate("openstack", "vddk", DISCARD_RUN_ID="12a")
        self.assertIn("Failed run ID must contain 1 to 20 digits.", errors)
        _, errors = self.migrate("openstack", "vddk", DISCARD_RUN_ID="123")
        self.assertIn("Confirm that the failed run should be discarded.", errors)
        _, errors = self.migrate("openstack", "vddk", confirm="yes", DISCARD_RUN_ID="123")
        self.assertEqual(errors, [])

    def test_azure_public_ip_requires_an_ssh_source(self):
        required = {name: "x" for name, meta in forms.F.items() if meta.required}
        required = {name: value for name, value in required.items()
                    if name in forms.exported_fields("launch", "azure")}
        _, errors = self.launch("azure", AZURE_NO_PUBLIC_IP="false",
                                AZURE_SSH_SOURCE_PREFIX="", **required)
        self.assertIn("Allow SSH from is required when assigning a public IP.", errors)
        values, errors = self.launch("azure", AZURE_NO_PUBLIC_IP="true",
                                     AZURE_SSH_SOURCE_PREFIX="203.0.113.0/24", **required)
        self.assertNotIn("Allow SSH from is required when assigning a public IP.", errors)
        self.assertEqual(values["AZURE_SSH_SOURCE_PREFIX"], "")

    def test_openstack_cross_field_rules(self):
        base = {"OS_FLAVOR": "small", "OS_NETWORK": "net"}
        cases = [
            ({"OS_FIXED_IPS_JSON": '["192.0.2.5"]', "OS_AUTO_IP": "true",
              "OS_SECURITY_GROUPS_JSON": '["default"]'},
             "Fixed IPs and an automatic floating IP cannot be combined."),
            ({"OS_FIXED_IPS_JSON": '["192.0.2.5"]', "OS_AUTO_IP": "false",
              "OS_SECURITY_GROUPS_JSON": "[]", "OS_SECURITY_GROUP_IDS_JSON": "[]"},
             "Fixed IPs and preserved MACs require a security group."),
            ({"OS_MAC_ADDRESSES_JSON": '["fa:16:3e:00:00:01"]', "OS_PRESERVE_MAC": "false"},
             "Enable preserved MACs when specifying manual MAC addresses."),
            ({"OS_FIXED_IPS_JSON": '["not-an-ip"]', "OS_AUTO_IP": "false",
              "OS_SECURITY_GROUPS_JSON": '["default"]'},
             f"{forms.F['OS_FIXED_IPS_JSON'].label} contains an invalid address."),
            ({"OS_MAC_ADDRESSES_JSON": '["fa:16:3e"]', "OS_PRESERVE_MAC": "true",
              "OS_SECURITY_GROUPS_JSON": '["default"]'},
             f"{forms.F['OS_MAC_ADDRESSES_JSON'].label} contains an invalid address."),
            ({"OS_USER_DATA": "#cloud-config", "OS_DHCP_USER_DATA": "true"},
             "Choose one cloud-init user-data source."),
        ]
        for extra, message in cases:
            with self.subTest(message=message):
                _, errors = self.launch("openstack", **base, **extra)
                self.assertIn(message, errors)

class WorkerTests(IsolatedState):
    """worker() against small wrapper scripts standing in for bin/*."""

    def wrapper(self, body):
        script = self.root / "wrapper.sh"
        script.write_text("#!/bin/sh\n" + body + "\n")
        script.chmod(0o755)
        patcher = patch.object(app, "wrapper_path", return_value=script)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start_worker(self):
        thread = threading.Thread(target=app.worker, daemon=True)
        thread.start()
        def stop():
            app.STOP_EVENT.set()
            app.stop_active_process()
            thread.join(30)
        self.addCleanup(stop)
        return thread

    def queue(self, values=None):
        return app.enqueue("operator", "migrate", "aws", "standard",
                           values or {"VM_NAME": "vm1", "AWS_TARGET_PROJECT": "site"})

    def job(self, job_id):
        with app.db() as conn:
            return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def wait_for(self, job_id, statuses, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.job(job_id)
            if row["status"] in statuses:
                return row
            time.sleep(0.05)
        self.fail(f"job {job_id} stayed {self.job(job_id)['status']}")

    def log(self, job_id):
        return (self.state / f"job-{job_id}.log").read_text()

    def test_successful_job_completes_with_its_output(self):
        self.wrapper('echo "migrating $VM_NAME to $AWS_TARGET_PROJECT"')
        job_id = self.queue()
        self.start_worker()
        row = self.wait_for(job_id, app.TERMINAL_JOB_STATUSES)
        self.assertEqual((row["status"], row["exit_code"]), ("completed", 0))
        self.assertIn("migrating vm1 to site", self.log(job_id))
        self.assertEqual((self.state / f"job-{job_id}.log").stat().st_mode & 0o777, 0o600)

    def test_failing_job_records_its_exit_code(self):
        self.wrapper("exit 3")
        job_id = self.queue()
        self.start_worker()
        row = self.wait_for(job_id, app.TERMINAL_JOB_STATUSES)
        self.assertEqual((row["status"], row["exit_code"]), ("failed", 3))

    def test_wrapper_gets_only_the_allowlisted_environment(self):
        self.wrapper('echo "secret=${SOME_OPERATOR_SECRET-unset} vault=$MIGRATE_V2V_VAULT_DIR"')
        with patch.dict(os.environ, {"SOME_OPERATOR_SECRET": "leak"}):
            job_id = self.queue()
            self.start_worker()
            self.wait_for(job_id, app.TERMINAL_JOB_STATUSES)
        self.assertIn(f"secret=unset vault={self.vaults}", self.log(job_id))

    def test_a_job_that_cannot_start_fails_and_the_queue_moves_on(self):
        with patch.object(app, "wrapper_path", return_value=self.root / "missing-wrapper"):
            first = self.queue()
            self.start_worker()
            row = self.wait_for(first, app.TERMINAL_JOB_STATUSES)
        self.assertEqual(row["status"], "failed")
        self.assertIn("Job failed before completion", self.log(first))

    def test_corrupt_job_values_fail_without_stalling_the_queue(self):
        self.wrapper("exit 0")
        bad = self.queue()
        with app.db() as conn:
            conn.execute("UPDATE jobs SET values_json='[1, 2]' WHERE id=?", (bad,))
        good = self.queue()
        self.start_worker()
        self.assertEqual(self.wait_for(bad, app.TERMINAL_JOB_STATUSES)["status"], "failed")
        self.assertEqual(self.wait_for(good, app.TERMINAL_JOB_STATUSES)["status"], "completed")

    def test_shutdown_stops_the_job_group_and_marks_it_interrupted(self):
        marker = self.root / "child-survived"
        # The wrapper's own child must die with it: the whole process group
        # is signalled, not just the wrapper.
        self.wrapper(f"(sleep 30; touch '{marker}') & echo started; wait")
        job_id = self.queue()
        thread = self.start_worker()
        self.wait_for(job_id, ("running",))
        deadline = time.monotonic() + 10
        while "started" not in self.log(job_id) and time.monotonic() < deadline:
            time.sleep(0.05)
        app.STOP_EVENT.set()
        app.stop_active_process()
        thread.join(30)
        row = self.job(job_id)
        self.assertEqual(row["status"], "interrupted")
        self.assertIn("controller is shutting down", self.log(job_id))
        self.assertFalse(marker.exists())

    def test_a_job_claimed_during_shutdown_is_not_started(self):
        # The stop flag is checked under the same lock stop_active_process
        # takes, immediately before launch.
        started = self.root / "started"
        self.wrapper(f"touch '{started}'")
        job_id = self.queue()
        real_popen = subprocess.Popen
        def popen_after_stop(*args, **kwargs):
            raise AssertionError("Popen must not run after shutdown began")
        with patch.object(app.subprocess, "Popen", side_effect=popen_after_stop):
            app.ACTIVE_LOCK.acquire()
            try:
                thread = self.start_worker()
                self.wait_for(job_id, ("running",))
                app.STOP_EVENT.set()
            finally:
                app.ACTIVE_LOCK.release()
            thread.join(30)
        self.assertIs(subprocess.Popen, real_popen)
        self.assertEqual(self.job(job_id)["status"], "interrupted")
        self.assertFalse(started.exists())

class CacheTests(IsolatedState):

    def test_vault_names_are_cached_until_the_file_changes(self):
        vault = self.vaults / "aws.yml"
        vault.write_text("encrypted\n")
        result = subprocess.CompletedProcess([], 0, stdout=b"aws_projects:\n  site: {}\n  lab: {}\n")
        with patch.object(app.subprocess, "run", return_value=result) as run:
            self.assertEqual(app.vault_keys("aws.yml", "aws_projects"), ["lab", "site"])
            self.assertEqual(app.vault_keys("aws.yml", "aws_projects"), ["lab", "site"])
            self.assertEqual(run.call_count, 1)
            vault.write_text("encrypted, changed\n")
            app.vault_keys("aws.yml", "aws_projects")
            self.assertEqual(run.call_count, 2)

    def test_vault_decrypt_failure_returns_no_names(self):
        (self.vaults / "aws.yml").write_text("encrypted\n")
        error = subprocess.CalledProcessError(1, "ansible-vault", stderr=b"bad password")
        with patch.object(app.subprocess, "run", side_effect=error):
            self.assertEqual(app.vault_keys("aws.yml", "aws_projects"), [])

    def test_a_slow_vault_does_not_block_another(self):
        for name in ("aws.yml", "gcp.yml"):
            (self.vaults / name).write_text("encrypted\n")
        release = threading.Event()
        def run(command, **kwargs):
            if "aws.yml" in command[2]:
                release.wait(10)
            return subprocess.CompletedProcess(command, 0, stdout=b"gcp_projects:\n  p: {}\n")
        with patch.object(app.subprocess, "run", side_effect=run):
            slow = threading.Thread(target=app.vault_keys, args=("aws.yml", "aws_projects"))
            slow.start()
            try:
                started = time.monotonic()
                self.assertEqual(app.vault_keys("gcp.yml", "gcp_projects"), ["p"])
                self.assertLess(time.monotonic() - started, 5)
            finally:
                release.set()
                slow.join(10)

class FactsQueryTests(unittest.TestCase):

    def setUp(self):
        patcher = patch.dict(facts._CACHE, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_option_like_values_never_reach_the_command(self):
        with patch.object(facts.subprocess, "run") as run:
            self.assertIsNone(facts.query("aws", "-p", "--subnets"))
            self.assertIsNone(facts.query("aws", "site", "--subnet", "--profile=x"))
            self.assertIsNone(facts.query("gcp", "site", "--machine-types", "--zone", "-z"))
        run.assert_not_called()

    def test_success_is_cached_and_failure_is_retried_soon(self):
        clock = [1000.0]
        ok = subprocess.CompletedProcess([], 0, stdout='{"zones": []}')
        with patch.object(facts.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(facts.subprocess, "run") as run:
            run.side_effect = subprocess.TimeoutExpired("facts", 60)
            self.assertIsNone(facts.query("gcp", "site", "--zones"))
            self.assertIsNone(facts.query("gcp", "site", "--zones"))
            self.assertEqual(run.call_count, 1)
            clock[0] += facts._NEGATIVE_TTL + 1
            run.side_effect = None
            run.return_value = ok
            self.assertEqual(facts.query("gcp", "site", "--zones"), {"zones": []})
            self.assertEqual(run.call_count, 2)
            clock[0] += facts._TTL - 1
            facts.query("gcp", "site", "--zones")
            self.assertEqual(run.call_count, 2)

if __name__ == "__main__":
    unittest.main()
