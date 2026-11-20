#!/usr/bin/env python3
# tests/test_migrate_dialogs.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Exercise the migration dialogs with a fake widget and a real PTY."""

import os
import pty
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
FAKE_DIALOG = r'''#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if '--inputbox' in args:
    prompt = args[args.index('--inputbox') + 1]
    if 'source vCenter' in prompt:
        value = os.environ.get('TEST_VCENTER', '')
    elif 'datacenter override' in prompt:
        value = os.environ.get('TEST_DATACENTER', args[-1])
        if value == '__DEFAULT__':
            value = args[-1]
    elif 'Source VM name' in prompt:
        value = "Web Server's 01"
    else:
        value = os.environ.get('TEST_TARGET', 'target one')
    print(value, file=sys.stderr, end='')
elif '--radiolist' in args:
    prompt = args[args.index('--radiolist') + 1]
    if 'VDDK transport backend' in prompt:
        print('auto', file=sys.stderr, end='')
    else:
        print(os.environ.get('TEST_CERTS', 'true'), file=sys.stderr, end='')
elif '--yesno' in args:
    prompt = args[args.index('--yesno') + 1]
    if os.environ.get('TEST_CANCEL') and 'Quiesce' in prompt:
        sys.exit(255)
    sys.exit(0 if 'Quiesce' in prompt or 'Compress' in prompt else 1)
'''

class MigrateDialogTests(unittest.TestCase):
    SCRIPTS = {
        "migrate-aws-dialog.sh": ("AWS_TARGET_PROJECT", "migrate-aws-vm.sh"),
        "migrate-azure-dialog.sh": ("AZURE_TARGET_PROJECT", "migrate-azure-vm.sh"),
        "migrate-azure-vddk-dialog.sh": ("AZURE_TARGET_PROJECT", "migrate-azure-vm-vddk.sh"),
        "migrate-gcp-dialog.sh": ("GCP_TARGET_PROJECT", "migrate-gcp-vm.sh"),
        "migrate-openstack-dialog.sh": ("OS_PROJECT_NAME", "migrate-openstack-vm.sh"),
        "migrate-openstack-vddk-dialog.sh": ("OS_PROJECT_NAME", "migrate-openstack-vm-vddk.sh"),
    }

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        stub_bin = self.tmp / "bin"
        stub_bin.mkdir()
        fake = stub_bin / "dialog"
        fake.write_text(FAKE_DIALOG)
        fake.chmod(0o755)
        home = self.tmp / "home"
        home.mkdir()
        vault_dir = self.tmp / "vaults"
        vault_dir.mkdir()
        catalog_dir = self.tmp / "catalogs"
        catalog_dir.mkdir()
        inherited = {name: os.environ[name] for name in
                     ("PATH", "LANG", "LC_ALL", "TERM") if name in os.environ}
        self.env = {
            **inherited,
            "PATH": f"{stub_bin}:{os.environ['PATH']}",
            "HOME": str(home),
            "TMPDIR": str(self.tmp),
            "ARTIFACT_ROOT": str(self.tmp / "artifacts"),
            "MIGRATE_V2V_VAULT_DIR": str(vault_dir),
            "MIGRATE_V2V_CATALOG_DIR": str(catalog_dir),
            "VAULT_PASSWORD_FILE": str(self.tmp / "missing-vault-password"),
            "TEST_DATACENTER": "Legacy DC",
            "TEST_TARGET": "target one",
        }

    def run_dialog(self, script, **overrides):
        env = {**self.env, **overrides}
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(BIN / script, [str(BIN / script)], env)
        output = bytearray()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        os.close(fd)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output.decode(errors="replace"))
        return sorted(self.tmp.glob("migrate-*-dialog.*"))

    def test_each_dialog_generates_the_right_wrapper_command(self):
        for script, (target_var, wrapper) in self.SCRIPTS.items():
            with self.subTest(script=script):
                files = self.run_dialog(script)
                self.assertEqual(len(files), 1)
                command = files[0].read_text()
                self.assertIn("export VM_NAME='Web Server'\\''s 01'", command)
                self.assertIn("export VCENTER_DATACENTER='Legacy DC'", command)
                self.assertIn(f"export {target_var}='target one'", command)
                self.assertIn("export VALIDATE_CERTS='true'", command)
                self.assertIn("export QUIESCE_SNAPSHOT='true'", command)
                self.assertIn(f"'{BIN / wrapper}'", command)
                self.assertEqual("export COMPRESS='true'" in command,
                                 script == "migrate-openstack-vddk-dialog.sh")
                self.assertEqual("export GUEST_CONVERT='false'" in command,
                                 script == "migrate-openstack-vddk-dialog.sh")
                if script.endswith("-vddk-dialog.sh"):
                    self.assertIn("export BACKEND='auto'", command)
                    self.assertIn("export DISCARD_RUN_ID=''", command)
                files[0].unlink()

    def test_cancel_on_yesno_stops_before_command_generation(self):
        files = self.run_dialog("migrate-aws-dialog.sh", TEST_CANCEL="1")
        self.assertEqual(files, [])

    def test_prefilled_datacenter_survives_manual_override(self):
        files = self.run_dialog("migrate-gcp-dialog.sh", TEST_DATACENTER="__DEFAULT__",
                                VCENTER_DATACENTER="Existing DC")
        self.assertIn("export VCENTER_DATACENTER='Existing DC'", files[0].read_text())

    def test_selected_vcenter_clears_legacy_datacenter(self):
        files = self.run_dialog("migrate-aws-dialog.sh", TEST_VCENTER="vc one",
                                VCENTER_DATACENTER="Old DC")
        command = files[0].read_text()
        self.assertIn("export VSPHERE_TARGET_VCENTER='vc one'", command)
        self.assertIn("export VCENTER_DATACENTER=''", command)

if __name__ == "__main__":
    unittest.main()
