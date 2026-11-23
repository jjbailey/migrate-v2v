#!/usr/bin/env python3
# tests/test_vddk_resume_azure.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Resume and discard of a datastore-direct Azure run, without Azure or vCenter.

The Azure counterpart of test_vddk_resume.py. The real azure/import.yml and
azure/discard-vddk.yml run under ansible-playbook with a fake
``azure.azcollection`` (storage account, page blobs, managed images and
snapshots kept in a JSON file) that can be told to fail an upload, an image
creation or a snapshot import part way, leaving what Azure leaves: a partial
blob, or a resource whose provisioning state is Failed. Disks are read from
local files (``vddk_local``) through the real nbdkit and qemu-img, so each disk
is converted to a real fixed VHD.

The snapshot, manifest and backend checks are shared with the OpenStack
playbook and tested in test_vddk_resume.py.
"""

import json
import shutil
import unittest
from pathlib import Path

from test_vddk_resume import MIGRATION, ResumeTestCase

AZURE = MIGRATION / "azure"
IMAGE = "vm-migration-123"
BLOBS = "migrate-v2v/123"
RESOURCES = ("azure_rm_storageaccount_info", "azure_rm_storageaccount",
             "azure_rm_storageblob", "azure_rm_storageblob_info", "azure_rm_image",
             "azure_rm_image_info", "azure_rm_snapshot", "azure_rm_snapshot_info")

PRELUDE = r'''#!/usr/bin/python
import json, os
from ansible.module_utils.basic import AnsibleModule

def load():
    return json.load(open(os.environ["FAKE_AZURE_STATE"]))

def save(state):
    json.dump(state, open(os.environ["FAKE_AZURE_STATE"], "w"))

def module(*names):
    return AnsibleModule(argument_spec={name: dict(type="raw") for name in names})
'''

MODULES = {
    "azure_rm_storageaccount_info": '''
m = module("resource_group", "name")
state = load()
m.exit_json(changed=False, storageaccounts=[{
    "name": m.params["name"], "location": state["location"],
    "primary_endpoints": {"blob": {"endpoint": "https://acct.blob.test/"}}}])
''',
    "azure_rm_storageaccount": '''
module("resource_group", "name", "location", "account_type", "kind", "https_only",
       "minimum_tls_version", "allow_blob_public_access", "tags").exit_json(changed=True)
''',
    "azure_rm_storageblob": '''
m = module("resource_group", "storage_account_name", "container", "blob", "blob_type",
           "src", "force", "state")
state = load()
blob = m.params["blob"]
if m.params["state"] == "absent":
    state["calls"].append(["delete-blob", blob])
    state["blobs"].pop(blob, None)
    save(state)
    m.exit_json(changed=True)
state["calls"].append(["upload", blob])
if blob in state["fail"]["blob"]:
    # An interrupted page-blob upload leaves a short blob behind.
    state["fail"]["blob"].remove(blob)
    state["blobs"][blob] = False
    save(state)
    m.fail_json(msg="fake upload failure for " + blob)
if not os.path.exists(m.params["src"]):
    m.fail_json(msg="no staged VHD " + str(m.params["src"]))
state["blobs"][blob] = True
save(state)
m.exit_json(changed=True)
''',
    "azure_rm_storageblob_info": '''
m = module("resource_group", "storage_account_name", "container_name", "name_starts_with")
state = load()
m.exit_json(changed=False, blob=[{"name": name} for name in sorted(state["blobs"])
                                 if name.startswith(m.params["name_starts_with"])])
''',
    "azure_rm_image": '''
m = module("resource_group", "name", "location", "source", "os_type",
           "hyper_v_generation", "tags", "state")
state = load()
name = m.params["name"]
if m.params["state"] == "absent":
    state["calls"].append(["delete-image", name])
    state["images"].pop(name, None)
    save(state)
    m.exit_json(changed=True)
state["calls"].append(["create-image", name])
if name in state["fail"]["image"]:
    state["fail"]["image"].remove(name)
    state["images"][name] = "Failed"
    save(state)
    m.fail_json(msg="fake image failure for " + name)
source = m.params["source"]
if not any(source.endswith("/" + blob) and complete for blob, complete in state["blobs"].items()):
    m.fail_json(msg="image source blob missing or incomplete: " + source)
state["images"][name] = "Succeeded"
save(state)
m.exit_json(changed=True)
''',
    "azure_rm_image_info": '''
m = module("resource_group", "name")
state = load()
m.exit_json(changed=False, images=[
    {"name": name, "id": "/rg/images/" + name, "provisioning_state": status}
    for name, status in sorted(state["images"].items())])
''',
    "azure_rm_snapshot": '''
m = module("resource_group", "name", "location", "sku", "creation_data", "tags", "state")
state = load()
name = m.params["name"]
if m.params["state"] == "absent":
    state["calls"].append(["delete-snapshot", name])
    state["snapshots"].pop(name, None)
    save(state)
    m.exit_json(changed=True)
state["calls"].append(["create-snapshot", name])
if name in state["fail"]["snapshot"]:
    state["fail"]["snapshot"].remove(name)
    state["snapshots"][name] = "Failed"
    save(state)
    m.fail_json(msg="fake snapshot failure for " + name)
source = m.params["creation_data"]["source_uri"]
if not any(source.endswith("/" + blob) and complete for blob, complete in state["blobs"].items()):
    m.fail_json(msg="snapshot source blob missing or incomplete: " + source)
state["snapshots"][name] = "Succeeded"
save(state)
m.exit_json(changed=True)
''',
    "azure_rm_snapshot_info": '''
m = module("resource_group", "name")
state = load()
m.exit_json(changed=False, state=[
    {"name": name, "id": "/rg/snapshots/" + name,
     "properties": {"provisioningState": status}}
    for name, status in sorted(state["snapshots"].items())
    if m.params["name"] in (None, name)])
''',
}

@unittest.skipUnless(shutil.which("nbdkit") and shutil.which("qemu-img"),
                     "needs nbdkit and qemu-img")
class ImportResumeAzure(ResumeTestCase):

    def setUp(self):
        super().setUp()
        collection = self.tmp / "collections/ansible_collections/azure/azcollection"
        (collection / "plugins/modules").mkdir(parents=True)
        (collection / "meta").mkdir()
        (collection / "meta/runtime.yml").write_text(
            "requires_ansible: '>=2.14'\naction_groups:\n  all:\n"
            + "".join(f"    - azure.azcollection.{name}\n" for name in MODULES))
        for name, body in MODULES.items():
            (collection / "plugins/modules" / f"{name}.py").write_text(PRELUDE + body)
        self.azure_state = self.tmp / "azure.json"
        self.set_azure()
        self.staging = self.run_dir / "disks"
        self.disks = []
        for index in (0, 1):
            raw = self.tmp / f"disk{index}.raw"
            raw.write_bytes(bytes([0xA0 + index]) * 1024 * 1024)
            self.disks.append({"index": index, "path": str(raw),
                               "file_size_bytes": raw.stat().st_size})

    def set_azure(self, fail=None, images=None, snapshots=None, blobs=None, location="eastus"):
        self.azure_state.write_text(json.dumps({
            "location": location, "images": images or {}, "snapshots": snapshots or {},
            "blobs": blobs or {},
            "fail": {"blob": [], "image": [], "snapshot": [], **(fail or {})},
            "calls": []}))

    def azure(self):
        return json.loads(self.azure_state.read_text())

    def count(self, *call):
        return self.azure()["calls"].count(list(call))

    def environment(self, **extra):
        return super().environment(FAKE_AZURE_STATE=str(self.azure_state), **extra)

    def variables(self, **overrides):
        return {
            "artifact_manifest": {"disks": self.disks},
            "migration_run_id": "123", "migration_run_dir": str(self.run_dir),
            "export_format": "datastore",
            "azure_resource_group": "rg", "azure_storage_account": "acct",
            "azure_storage_container": "images", "azure_location": "eastus",
            "azure_module_defaults": {}, "azure_replace_existing_image": True,
            "azure_blob_prefix": BLOBS, "azure_image_prefix": IMAGE,
            "azure_staging_dir": str(self.staging),
            "azure_hyper_v_generation_effective": "V2",
            "azure_image_lookup_retries": 3, "azure_image_lookup_delay": 1,
            "azure_convert_args": [], "azure_convert_async_timeout": 120,
            "azure_convert_poll_retries": 60, "azure_convert_poll_delay": 1,
            "vddk_local": True, "vddk_backend_effective": "curl",
            "vddk_nbdkit_bin": "nbdkit", "vddk_socket_dir": str(self.sockets),
            "qemu_img_bin": "qemu-img",
            # State the earlier plays of a real run set up; play vars so the
            # tasks' set_fact can replace them.
            "azure_created_image_names": [], "azure_reused_image_names": [],
            "azure_created_snapshot_names": [], "azure_reused_snapshot_names": [],
            "azure_blob_names": [], "azure_convert_jobs": {}, "azure_pending_disks": [],
            **overrides,
        }

    def import_disks(self, resume, **overrides):
        return self.play([{"ansible.builtin.import_tasks": str(AZURE / "import.yml")}],
                         self.variables(migration_resume_mode=resume, **overrides))

    def leftovers(self):
        return sorted(path.name for path in self.staging.iterdir())

    def test_fresh_run_creates_the_image_and_the_snapshot(self):
        played = self.import_disks(resume=False)
        self.assertEqual(played.returncode, 0, played.stdout + played.stderr)
        self.assertEqual(self.azure()["images"], {f"{IMAGE}-disk-0": "Succeeded"})
        self.assertEqual(self.azure()["snapshots"], {f"{IMAGE}-disk-1": "Succeeded"})
        self.assertEqual(self.leftovers(), [])

    def test_resume_after_a_failed_data_disk_upload_reuses_the_boot_image(self):
        self.set_azure(fail={"blob": [f"{BLOBS}/disk-1.vhd"]})
        failed = self.import_disks(resume=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.azure()["blobs"][f"{BLOBS}/disk-1.vhd"], False)

        resumed = self.import_disks(resume=True)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("1 of 2 disk(s) need conversion and upload", self.output(resumed))
        self.assertIn(f"Reusing existing Azure resource(s) {IMAGE}-disk-0", self.output(resumed))
        # The completed boot image was neither uploaded nor created again.
        self.assertEqual(self.count("upload", f"{BLOBS}/disk-0.vhd"), 1)
        self.assertEqual(self.count("create-image", f"{IMAGE}-disk-0"), 1)
        self.assertEqual(self.count("delete-image", f"{IMAGE}-disk-0"), 0)
        # The partial blob was replaced and the snapshot built from it.
        self.assertEqual(self.count("upload", f"{BLOBS}/disk-1.vhd"), 2)
        self.assertEqual(self.azure()["blobs"][f"{BLOBS}/disk-1.vhd"], True)
        self.assertEqual(self.azure()["snapshots"], {f"{IMAGE}-disk-1": "Succeeded"})
        self.assertEqual(self.leftovers(), [])

    def test_resume_replaces_a_boot_image_that_failed_to_provision(self):
        self.set_azure(fail={"image": [f"{IMAGE}-disk-0"]})
        self.assertNotEqual(self.import_disks(resume=False).returncode, 0)
        self.assertEqual(self.azure()["images"], {f"{IMAGE}-disk-0": "Failed"})

        resumed = self.import_disks(resume=True)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("2 of 2 disk(s) need conversion and upload", self.output(resumed))
        self.assertEqual(self.count("delete-image", f"{IMAGE}-disk-0"), 1)
        self.assertEqual(self.count("create-image", f"{IMAGE}-disk-0"), 2)
        # A named page blob may hold an interrupted upload: every disk that
        # still needs import is sent again.
        self.assertEqual(self.count("upload", f"{BLOBS}/disk-0.vhd"), 2)
        self.assertEqual(self.azure()["images"], {f"{IMAGE}-disk-0": "Succeeded"})
        self.assertEqual(self.azure()["snapshots"], {f"{IMAGE}-disk-1": "Succeeded"})

    def test_resume_replaces_a_snapshot_that_failed_to_import(self):
        self.set_azure(fail={"snapshot": [f"{IMAGE}-disk-1"]})
        self.assertNotEqual(self.import_disks(resume=False).returncode, 0)
        self.assertEqual(self.azure()["snapshots"], {f"{IMAGE}-disk-1": "Failed"})

        resumed = self.import_disks(resume=True)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.count("create-image", f"{IMAGE}-disk-0"), 1)
        self.assertEqual(self.count("delete-snapshot", f"{IMAGE}-disk-1"), 1)
        self.assertEqual(self.count("create-snapshot", f"{IMAGE}-disk-1"), 2)
        self.assertEqual(self.azure()["snapshots"], {f"{IMAGE}-disk-1": "Succeeded"})

    def test_resume_will_not_replace_a_failed_resource_unless_allowed(self):
        self.set_azure(fail={"snapshot": [f"{IMAGE}-disk-1"]})
        self.assertNotEqual(self.import_disks(resume=False).returncode, 0)
        calls = len(self.azure()["calls"])
        resumed = self.import_disks(resume=True, azure_replace_existing_image=False)
        self.assertNotEqual(resumed.returncode, 0)
        self.assertIn(f"Azure resource(s) {IMAGE}-disk-1 already exist", self.output(resumed))
        self.assertIn("azure_replace_existing_image=true", self.output(resumed))
        self.assertEqual(len(self.azure()["calls"]), calls)

    def test_resume_with_everything_present_uploads_nothing(self):
        self.set_azure(images={f"{IMAGE}-disk-0": "Succeeded"},
                       snapshots={f"{IMAGE}-disk-1": "Succeeded"})
        resumed = self.import_disks(resume=True)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("0 of 2 disk(s) need conversion and upload", self.output(resumed))
        self.assertEqual([call for call in self.azure()["calls"]
                          if call[0] in ("upload", "create-image", "create-snapshot")], [])

    def test_a_fresh_run_refuses_to_reuse_a_leftover_image(self):
        # Only a resume may adopt a finished image; a new run that finds one
        # under its own run-ID name stops unless replacement is allowed.
        self.set_azure(images={f"{IMAGE}-disk-0": "Succeeded"})
        played = self.import_disks(resume=False, azure_replace_existing_image=False)
        self.assertNotEqual(played.returncode, 0)
        self.assertIn(f"Azure resource(s) {IMAGE}-disk-0 already exist", self.output(played))

    def test_discard_finds_the_whole_retained_run_and_nothing_else(self):
        self.set_azure(
            images={f"{IMAGE}-disk-0": "Failed", "someone-elses-image": "Succeeded"},
            snapshots={f"{IMAGE}-disk-1": "Succeeded", "other-vm-disk-1": "Succeeded"},
            blobs={f"{BLOBS}/disk-0.vhd": True, f"{BLOBS}/disk-1.vhd": False,
                   "migrate-v2v/456/disk-0.vhd": True})
        facts = self.tmp / "facts.json"
        played = self.play([
            {"ansible.builtin.import_tasks": str(AZURE / "discard-vddk.yml")},
            {"ansible.builtin.copy": {"dest": str(facts), "content": (
                "{{ {'images': azure_created_image_names,"
                " 'snapshots': azure_created_snapshot_names,"
                " 'blobs': azure_blob_names} | to_json }}")}},
        ], self.variables(migration_resume_mode=True, migration_discard_only=True))
        self.assertEqual(played.returncode, 0, played.stdout + played.stderr)
        self.assertEqual(json.loads(facts.read_text()), {
            "images": [f"{IMAGE}-disk-0"], "snapshots": [f"{IMAGE}-disk-1"],
            "blobs": [f"{BLOBS}/disk-0.vhd", f"{BLOBS}/disk-1.vhd"]})

if __name__ == "__main__":
    unittest.main()
