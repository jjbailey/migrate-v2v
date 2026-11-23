#!/usr/bin/env python3
# tests/test_vddk_resume.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Resume and discard of a datastore-direct OpenStack run, without a vCenter.

The real task files run under ansible-playbook against three stand-ins:

* a stub ``govc`` that answers ``find``, ``snapshot.tree`` and ``vm.info``
  from a JSON file, so each case chooses which snapshots the source VM has;
* a stub ``nbdkit`` for the backend checks (the real one is used for the
  disk streams in the upload tests, through ``vddk_local``);
* a fake ``openstack.cloud`` collection (``image`` and ``image_info``) that
  keeps Glance's images in a JSON file and can be told to fail an upload part
  way, leaving the queued image a real failed upload leaves.

A failed run is made the way a real one fails: the first ansible-playbook
exits non-zero with some images uploaded, then a second one resumes it.
What the tests read afterwards is the fake Glance's call log, so they can say
which disks were uploaded how many times, not only that the play passed.

Layout, one class per (what is tested, target):

                              OpenStack                    Azure
    import, upload, reuse     ImportResumeOpenStack        ImportResumeAzure (test_vddk_resume_azure.py)
    resume-vddk.yml checks    ResumeChecksOpenStack        ResumeChecksAzure
    backend pinning           BackendPinningOpenStack      BackendPinningAzure

ResumeChecks and BackendPinning are mixins, so a test written once runs for
both targets. Add a target-specific test to the ImportResume class instead.

Not covered here: a real vCenter, VixDiskLib, and killing ansible-playbook
itself (test_vddk_streams.py covers stopping a killed run's streams).
"""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "playbooks" / "tasks" / "migration"
COMMON = MIGRATION / "common"
OPENSTACK = MIGRATION / "openstack"
PREFIX = "vm-openstack-vddk-123"

FAKE_IMAGE_MODULE = r'''#!/usr/bin/python
# Fake openstack.cloud.image: Glance as a JSON file (FAKE_GLANCE_STATE).
import json, os
from ansible.module_utils.basic import AnsibleModule

def main():
    module = AnsibleModule(argument_spec=dict(
        auth=dict(type="raw", no_log=True), region_name=dict(type="raw"),
        name=dict(type="str", required=True), filename=dict(type="str"),
        disk_format=dict(type="str"), container_format=dict(type="str"),
        state=dict(type="str", default="present"), wait=dict(type="bool"),
        timeout=dict(type="int"), properties=dict(type="raw")))
    path = os.environ["FAKE_GLANCE_STATE"]
    state = json.load(open(path))
    name = module.params["name"]
    if module.params["state"] == "absent":
        state["calls"].append(["delete", name])
        state["images"].pop(name, None)
        json.dump(state, open(path, "w"))
        module.exit_json(changed=True)
    state["calls"].append(["upload", name])
    if name in state["fail_upload"]:
        # A failed upload leaves a queued image behind, as Glance does.
        state["fail_upload"].remove(name)
        state["images"][name] = {"id": "partial-" + name, "status": "queued"}
        json.dump(state, open(path, "w"))
        module.fail_json(msg="fake upload failure for " + name)
    if not os.path.exists(module.params["filename"]):
        module.fail_json(msg="no staged file " + str(module.params["filename"]))
    state["next_id"] += 1
    image = {"id": "image-%d" % state["next_id"], "status": "active"}
    state["images"][name] = image
    json.dump(state, open(path, "w"))
    module.exit_json(changed=True, image=dict(image, name=name))

main()
'''

FAKE_IMAGE_INFO_MODULE = r'''#!/usr/bin/python
# Fake openstack.cloud.image_info over the same JSON file.
import json, os
from ansible.module_utils.basic import AnsibleModule

def main():
    module = AnsibleModule(argument_spec=dict(
        auth=dict(type="raw", no_log=True), region_name=dict(type="raw"),
        name=dict(type="str")))
    state = json.load(open(os.environ["FAKE_GLANCE_STATE"]))
    wanted = module.params["name"]
    images = [dict(image, name=name) for name, image in sorted(state["images"].items())
              if wanted is None or wanted == name]
    module.exit_json(changed=False, images=images)

main()
'''

GOVC_STUB = r'''#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["GOVC_STUB_STATE"]))
args = sys.argv[1:]
if args[0] == "find":
    print("\n".join(state["vms"]))
elif args[0] == "snapshot.tree":
    for number, name in enumerate(state["snapshots"], start=88800):
        print(("[snapshot-%d]  %s" % (number, name)) if "-i" in args else name)
elif args[0] == "vm.info":
    print(json.dumps({"virtualMachines": [{"Self": {"value": "vm-42"}}]}))
else:
    sys.exit("unexpected govc call: %r" % args)
'''

NBDKIT_STUB = r'''#!/bin/sh
# --dump-plugin of the vddk plugin answers as NBDKIT_STUB_VDDK says:
# "loads" (plugin and library present) or "missing" (no plugin at all).
case "$*" in
  *--dump-plugin*)
    if [ "${NBDKIT_STUB_VDDK:-missing}" = loads ]; then
      echo vddk_library_version=8.0.3
    else
      echo "nbdkit: error: cannot open plugin 'vddk'" >&2
      exit 1
    fi ;;
  *) echo "nbdkit 1.36.3" ;;
esac
'''

def flat(text):
    """Ansible wraps long messages; compare them as one line."""
    return " ".join(text.replace("\\n", " ").split())

@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is required")
class ResumeTestCase(unittest.TestCase):
    """Shared scaffolding: a scratch tree, the fake collection and stubs."""

    def setUp(self):
        try:
            with tempfile.TemporaryDirectory(prefix=".vddk-probe-", dir="/tmp") as directory:
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.bind(str(Path(directory) / "probe.sock"))
        except OSError as error:
            self.skipTest(f"Unix sockets unavailable: {error}")
        self.tmp = Path(tempfile.mkdtemp(prefix="vddk-resume-"))
        self.sockets = Path(tempfile.mkdtemp(prefix=".vddk-resume-", dir="/tmp"))
        self.run_dir = self.tmp / "run"
        self.staging = self.run_dir / "disks"
        self.staging.mkdir(parents=True)

        modules = self.tmp / "collections/ansible_collections/openstack/cloud/plugins/modules"
        modules.mkdir(parents=True)
        (modules / "image.py").write_text(FAKE_IMAGE_MODULE)
        (modules / "image_info.py").write_text(FAKE_IMAGE_INFO_MODULE)
        self.glance_state = self.tmp / "glance.json"
        self.set_glance()

        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.govc = self.stub("govc", GOVC_STUB)
        self.nbdkit = self.stub("nbdkit", NBDKIT_STUB)
        self.govc_state = self.tmp / "govc.json"
        self.set_vm(snapshots=["migration-123"])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.sockets, ignore_errors=True)

    def stub(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def set_glance(self, images=None, fail_upload=(), calls=None):
        self.glance_state.write_text(json.dumps({
            "images": images or {}, "fail_upload": list(fail_upload),
            "calls": calls or [], "next_id": 0}))

    def glance(self):
        return json.loads(self.glance_state.read_text())

    def uploads(self, name):
        return [call for call in self.glance()["calls"] if call == ["upload", name]]

    def set_vm(self, snapshots, vms=("/DC/vm/my-vm",)):
        self.govc_state.write_text(json.dumps({"vms": list(vms), "snapshots": snapshots}))

    def environment(self, **extra):
        return {**os.environ,
                "ANSIBLE_COLLECTIONS_PATH": str(self.tmp / "collections"),
                "ANSIBLE_LOCALHOST_WARNING": "False",
                "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False",
                "FAKE_GLANCE_STATE": str(self.glance_state),
                "GOVC_STUB_STATE": str(self.govc_state),
                **extra}

    def play(self, tasks, variables, **env):
        """Run tasks with variables as *play* vars: set_fact cannot override
        an extra var, and these tasks build up lists and maps with set_fact."""
        text = yaml.safe_dump([{"hosts": "localhost", "connection": "local",
                                "gather_facts": False, "vars": variables,
                                "tasks": tasks}], sort_keys=False)
        playbook = self.tmp / "play.yml"
        playbook.write_text(text)
        return subprocess.run(["ansible-playbook", str(playbook)], capture_output=True,
                              text=True, check=False, timeout=300,
                              env=self.environment(**env))

    def output(self, played):
        return flat(played.stdout + played.stderr)

class ImportResumeOpenStack(ResumeTestCase):
    """import-vddk.yml: the retained run's Glance images after a failure."""

    def setUp(self):
        super().setUp()
        self.disks = []
        for index in (0, 1):
            raw = self.tmp / f"disk{index}.raw"
            raw.write_bytes(bytes([0xA0 + index]) * 1024 * 1024)
            self.disks.append({"index": index, "path": str(raw)})
        (self.sockets / "run").mkdir()

    def import_disks(self, resume, **overrides):
        variables = {
            "artifact_manifest": {"disks": self.disks, "source_firmware": "bios"},
            "migration_resume_mode": resume,
            "migration_run_dir": str(self.run_dir),
            "openstack_staging_dir": str(self.staging),
            "openstack_image_prefix": PREFIX,
            "openstack_auth": {}, "openstack_region_name": "test",
            "openstack_boot_mode": "", "openstack_guest_convert": False,
            "openstack_convert_args": [], "openstack_convert_compress": False,
            "openstack_convert_async_timeout": 120,
            "openstack_convert_poll_retries": 60, "openstack_convert_poll_delay": 1,
            "openstack_import_poll_retries": 5, "openstack_import_poll_delay": 1,
            "vddk_local": True, "vddk_backend_effective": "curl",
            "vddk_nbdkit_bin": "nbdkit", "vddk_socket_dir": str(self.sockets),
            "qemu_img_bin": "qemu-img",
            # Mutable state the earlier plays of a real run set up. They are
            # play vars so the tasks' set_fact can replace them.
            "openstack_pending_disks": [], "openstack_disk_image_ids": {},
            "openstack_uploaded_image_names": [], "openstack_convert_jobs": {},
            "openstack_image_ids": [], "openstack_image_names": [],
            **overrides,
        }
        return self.play([{"ansible.builtin.import_tasks": str(OPENSTACK / "import-vddk.yml")}],
                         variables)

    def leftovers(self):
        return sorted(path.name for path in self.staging.iterdir())

    @unittest.skipUnless(shutil.which("nbdkit") and shutil.which("qemu-img"),
                         "needs nbdkit and qemu-img")
    def test_fresh_run_uploads_each_disk_once(self):
        played = self.import_disks(resume=False)
        self.assertEqual(played.returncode, 0, played.stdout + played.stderr)
        self.assertEqual(len(self.uploads(f"{PREFIX}-disk-0")), 1)
        self.assertEqual(len(self.uploads(f"{PREFIX}-disk-1")), 1)
        self.assertEqual(self.leftovers(), [])

    @unittest.skipUnless(shutil.which("nbdkit") and shutil.which("qemu-img"),
                         "needs nbdkit and qemu-img")
    def test_resume_after_a_failed_second_upload_uploads_only_what_is_missing(self):
        self.set_glance(fail_upload=[f"{PREFIX}-disk-1"])
        failed = self.import_disks(resume=False)
        self.assertNotEqual(failed.returncode, 0)
        images = self.glance()["images"]
        self.assertEqual(images[f"{PREFIX}-disk-0"]["status"], "active")
        self.assertEqual(images[f"{PREFIX}-disk-1"]["status"], "queued")
        first = images[f"{PREFIX}-disk-0"]["id"]

        resumed = self.import_disks(resume=True)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("1 of 2 disk(s) need conversion and upload", self.output(resumed))
        state = self.glance()
        # Disk 0 was uploaded by the first attempt and left alone by the resume.
        self.assertEqual(len(self.uploads(f"{PREFIX}-disk-0")), 1)
        self.assertEqual(state["images"][f"{PREFIX}-disk-0"]["id"], first)
        # Disk 1: the partial image was deleted, then it was uploaded again.
        self.assertEqual(len(self.uploads(f"{PREFIX}-disk-1")), 2)
        self.assertIn(["delete", f"{PREFIX}-disk-1"], state["calls"])
        self.assertEqual(state["images"][f"{PREFIX}-disk-1"]["status"], "active")
        self.assertEqual(self.leftovers(), [])

    @unittest.skipUnless(shutil.which("nbdkit") and shutil.which("qemu-img"),
                         "needs nbdkit and qemu-img")
    def test_resume_after_a_failed_first_upload_redoes_both_disks(self):
        self.set_glance(fail_upload=[f"{PREFIX}-disk-0"])
        self.assertNotEqual(self.import_disks(resume=False).returncode, 0)
        # The lookahead had already converted disk 1; none of it may be
        # mistaken for an upload, and the resume starts clean.
        resumed = self.import_disks(resume=True)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("2 of 2 disk(s) need conversion and upload", self.output(resumed))
        images = self.glance()["images"]
        self.assertEqual({name: image["status"] for name, image in images.items()},
                         {f"{PREFIX}-disk-0": "active", f"{PREFIX}-disk-1": "active"})
        self.assertEqual(self.leftovers(), [])

    def test_resume_with_every_image_present_uploads_nothing(self):
        self.set_glance(images={f"{PREFIX}-disk-0": {"id": "a", "status": "active"},
                                f"{PREFIX}-disk-1": {"id": "b", "status": "active"}})
        resumed = self.import_disks(resume=True)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.glance()["calls"], [])
        self.assertIn("0 of 2 disk(s) need conversion and upload", self.output(resumed))

    def test_a_fresh_run_refuses_a_leftover_partial_image(self):
        # Only a resume may delete an incomplete image; a new run that finds
        # one under its own run-ID name stops instead of guessing.
        self.set_glance(images={f"{PREFIX}-disk-0": {"id": "p", "status": "queued"}})
        played = self.import_disks(resume=False)
        self.assertNotEqual(played.returncode, 0)
        self.assertIn("so an earlier upload did not finish", self.output(played))
        self.assertEqual(self.glance()["calls"], [])

    def test_resume_refuses_a_converted_run_without_guest_convert(self):
        (self.run_dir / "guest-converted.json").write_text("{}")
        self.set_glance(images={f"{PREFIX}-disk-0": {"id": "a", "status": "active"}})
        played = self.import_disks(resume=True)
        self.assertNotEqual(played.returncode, 0)
        self.assertIn("already guest-converted its disks", self.output(played))
        self.assertIn("Resume with GUEST_CONVERT=true", self.output(played))
        self.assertEqual(self.uploads(f"{PREFIX}-disk-1"), [])

    def test_a_converted_run_that_is_fully_uploaded_needs_no_guest_convert(self):
        (self.run_dir / "guest-converted.json").write_text("{}")
        self.set_glance(images={f"{PREFIX}-disk-0": {"id": "a", "status": "active"},
                                f"{PREFIX}-disk-1": {"id": "b", "status": "active"}})
        played = self.import_disks(resume=True)
        self.assertEqual(played.returncode, 0, played.stdout + played.stderr)

    def test_guest_convert_resume_deletes_uploaded_images_and_queues_every_disk(self):
        # virt-v2v-in-place converts the guest's disks together, so a partly
        # uploaded set is redone whole. The play is expected to stop afterwards,
        # in the conversion itself, which needs libguestfs; what is checked is
        # the redo that precedes it.
        (self.run_dir / "guest-converted.json").write_text("{}")
        self.set_glance(images={f"{PREFIX}-disk-0": {"id": "a", "status": "active"}})
        played = self.import_disks(resume=True, openstack_guest_convert=True)
        self.assertIn("2 of 2 disk(s) need conversion and upload", self.output(played))
        self.assertIn(["delete", f"{PREFIX}-disk-0"], self.glance()["calls"])
        self.assertEqual(self.glance()["images"], {})

class ResumeChecks:
    """resume-vddk.yml: what a resume needs from the manifest and the VM.

    Shared by both targets: a mixin, run once per target by the subclasses
    below, so a check written here covers OpenStack and Azure. Only the
    discard's image lookup differs (assert_discard_images).
    """

    def setUp(self):
        super().setUp()
        self.manifest = self.run_dir / "artifact-manifest.json"
        self.manifest.write_text(json.dumps({"disks": [], "transfer_backend": "vddk"}))
        self.facts = self.tmp / "facts.json"

    def resume(self, discard=False, backend="vddk", **overrides):
        variables = {
            "migration_resume_mode": True, "migration_discard_only": discard,
            "migration_run_id": "123", "migration_target": self.target,
            "artifact_manifest_path": str(self.manifest),
            "vm_name": "my-vm", "datacenter": "DC", "snapshot_name": "migration-123",
            "govc_bin": str(self.govc), "govc_url": "https://vc.test/sdk",
            "govc_username": "u", "govc_password": "p", "govc_insecure": "1",
            "stdin_env_bin": str(ROOT / "bin/exec-with-stdin-env.sh"),
            "vddk_backend_effective": backend,
            "resume_backend_manifest": {"disks": []},
            "openstack_image_prefix": PREFIX, "openstack_auth": {},
            "openstack_region_name": "test", "openstack_uploaded_image_names": [],
            "snapshot_cleanup_required": False,
            **overrides,
        }
        tasks = [
            {"ansible.builtin.import_tasks": str(COMMON / "resume-vddk.yml")},
            {"ansible.builtin.copy": {
                "dest": str(self.facts),
                "content": "{{ {'snapshot_cleanup_required': snapshot_cleanup_required,"
                           " 'moref': source_snapshot_moref | default(''),"
                           " 'vm': source_vm_moref | default(''),"
                           " 'images': openstack_uploaded_image_names} | to_json }}"}},
        ]
        return self.play(tasks, variables)

    def test_a_retained_snapshot_is_resumed_and_taken_over_for_cleanup(self):
        self.set_vm(snapshots=["unrelated", "migration-123"])
        played = self.resume()
        self.assertEqual(played.returncode, 0, played.stdout + played.stderr)
        facts = json.loads(self.facts.read_text())
        self.assertTrue(facts["snapshot_cleanup_required"])
        self.assertEqual(facts["vm"], "vm-42")
        # The VixDiskLib reference of the right snapshot, not the first one.
        self.assertEqual(facts["moref"], "snapshot-88801")

    def test_a_longer_snapshot_name_does_not_stand_in_for_ours(self):
        self.set_vm(snapshots=["migration-1234"])
        played = self.resume()
        self.assertNotEqual(played.returncode, 0)
        self.assertIn("needs snapshot 'migration-123'", self.output(played))
        self.assertIn("no longer exists", self.output(played))

    def test_a_missing_snapshot_cannot_be_resumed(self):
        self.set_vm(snapshots=[])
        played = self.resume()
        self.assertNotEqual(played.returncode, 0)
        self.assertIn("The frozen disks cannot be re-read; start a new run",
                      self.output(played))

    def test_a_missing_manifest_cannot_be_resumed(self):
        self.manifest.unlink()
        played = self.resume()
        self.assertNotEqual(played.returncode, 0)
        self.assertIn("is missing", self.output(played))
        self.assertIn("Keep the persistent migration directory", self.output(played))

    def test_an_ambiguous_source_vm_is_refused(self):
        self.set_vm(snapshots=["migration-123"], vms=["/DC/vm/a/my-vm", "/DC/vm/b/my-vm"])
        played = self.resume()
        self.assertNotEqual(played.returncode, 0)
        self.assertIn("2 match(es)", self.output(played))

    def test_a_vm_whose_name_only_ends_the_same_is_not_a_match(self):
        self.set_vm(snapshots=["migration-123"], vms=["/DC/vm/not-my-vm"])
        played = self.resume()
        self.assertNotEqual(played.returncode, 0)
        self.assertIn("0 match(es)", self.output(played))

    def test_a_curl_resume_checks_the_recorded_disks_again(self):
        # The VM may have been snapshotted since: curl would read stale files.
        disks = [{"index": 0, "path": "[ds] vm/vm.vmdk", "snapshot_chain_depth": 1}]
        played = self.resume(backend="curl", resume_backend_manifest={"disks": disks})
        self.assertNotEqual(played.returncode, 0)
        self.assertIn("would read only the stale base files", self.output(played))
        self.assertIn("-e vddk_backend=vddk", self.output(played))

    def test_discard_tolerates_a_missing_manifest_and_snapshot(self):
        self.manifest.unlink()
        self.set_vm(snapshots=[])
        self.set_glance(images={f"{PREFIX}-disk-0": {"id": "p", "status": "queued"},
                                "someone-elses-image": {"id": "x", "status": "active"}})
        played = self.resume(discard=True)
        self.assertEqual(played.returncode, 0, played.stdout + played.stderr)
        facts = json.loads(self.facts.read_text())
        self.assertFalse(facts["snapshot_cleanup_required"])
        self.assert_discard_images(facts["images"])

    def test_discard_queues_the_retained_snapshot_for_removal(self):
        played = self.resume(discard=True)
        self.assertEqual(played.returncode, 0, played.stdout + played.stderr)
        self.assertTrue(json.loads(self.facts.read_text())["snapshot_cleanup_required"])

class ResumeChecksOpenStack(ResumeChecks, ResumeTestCase):
    target = "openstack"

    def assert_discard_images(self, found):
        # A partial upload is found by its run-ID name; nothing else is.
        self.assertEqual(found, [f"{PREFIX}-disk-0"])

class ResumeChecksAzure(ResumeChecks, ResumeTestCase):
    target = "azure"

    def assert_discard_images(self, found):
        # Azure's lookup is azure/discard-vddk.yml (test_vddk_resume_azure.py);
        # the shared tasks must not ask Glance about an Azure run.
        self.assertEqual(found, [])
        self.assertEqual(self.glance()["calls"], [])

class BackendPinning:
    """select-backend-vddk.yml: a resume reads disks the way the run did.

    A mixin run once per target, like ResumeChecks.
    """

    def setUp(self):
        super().setUp()
        self.manifest = self.run_dir / "artifact-manifest.json"
        self.facts = self.tmp / "facts.json"

    def select(self, recorded, requested="auto", plugin="loads"):
        manifest = {"disks": []}
        if recorded is not None:
            manifest["transfer_backend"] = recorded
        self.manifest.write_text(json.dumps(manifest))
        variables = {
            "migration_run_dir": str(self.run_dir), "resume_run_id": "123",
            "migration_discard_only": False, "migration_target": self.target,
            "artifact_root": str(self.tmp), "vm_name": "my-vm",
            "vddk_backend": requested, "vddk_nbdkit_bin": str(self.nbdkit),
            "vddk_libdir": str(self.tmp / "no-such-libdir"),
            "vddk_ca_bundle": "", "vddk_thumbprint": "AA", "validate_certs": False,
            "vcenter_hostname": "vc.test",
        }
        tasks = [
            {"ansible.builtin.import_tasks": str(COMMON / "select-backend-vddk.yml")},
            {"ansible.builtin.copy": {
                "dest": str(self.facts),
                "content": "{{ {'backend': vddk_backend_effective} | to_json }}"}},
        ]
        return self.play(tasks, variables, NBDKIT_STUB_VDDK=plugin)

    def backend(self):
        return json.loads(self.facts.read_text())["backend"]

    def test_auto_follows_the_recorded_vddk_backend(self):
        played = self.select("vddk", plugin="loads")
        self.assertEqual(played.returncode, 0, played.stdout + played.stderr)
        self.assertEqual(self.backend(), "vddk")

    def test_auto_follows_the_recorded_curl_backend_even_when_vddk_loads(self):
        # A new run would choose vddk here; a resume must not change method.
        played = self.select("curl", plugin="loads")
        self.assertEqual(played.returncode, 0, played.stdout + played.stderr)
        self.assertEqual(self.backend(), "curl")

    def test_asking_for_the_other_backend_is_refused(self):
        for recorded, requested in (("vddk", "curl"), ("curl", "vddk")):
            with self.subTest(recorded=recorded, requested=requested):
                played = self.select(recorded, requested=requested, plugin="loads")
                self.assertNotEqual(played.returncode, 0)
                self.assertIn(f"read its disks with the {recorded} backend", self.output(played))
                self.assertIn(f"Resume with -e vddk_backend={recorded}", self.output(played))
                self.assertIn("discard the run with -e discard_run_id=123", self.output(played))

    def test_a_vddk_run_never_falls_back_to_curl_when_the_plugin_is_gone(self):
        played = self.select("vddk", plugin="missing")
        self.assertNotEqual(played.returncode, 0)
        self.assertIn("the curl fallback cannot stand in for it on a resume",
                      self.output(played))
        self.assertIn("discard the run with -e discard_run_id=123", self.output(played))
        self.assertFalse(self.facts.exists())

    def test_a_manifest_that_predates_the_recorded_backend_only_warns(self):
        played = self.select(None, plugin="loads")
        self.assertEqual(played.returncode, 0, played.stdout + played.stderr)
        self.assertIn("predates transfer_backend", self.output(played))
        self.assertEqual(self.backend(), "vddk")

    def test_an_unknown_recorded_backend_is_refused(self):
        played = self.select("hotadd", plugin="loads")
        self.assertNotEqual(played.returncode, 0)
        self.assertIn("records an unknown transfer_backend 'hotadd'", self.output(played))

class BackendPinningOpenStack(BackendPinning, ResumeTestCase):
    target = "openstack"

class BackendPinningAzure(BackendPinning, ResumeTestCase):
    target = "azure"

if __name__ == "__main__":
    unittest.main()
