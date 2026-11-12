#!/usr/bin/env python3
# tests/test_launch_dialogs.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Check launch dialog command generation without contacting cloud APIs."""

import json
import os
import pty
import subprocess
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
    values = {
        'Azure VM name': '',
        'VM name': "Web Server's 01",
        'AWS subnet': 'subnet-123',
        'Security group names': 'default,web',
        'EC2 key pair': 'cloud',
        'VM size': 'Standard_B2s_v2',
        'Admin user': 'azureuser',
        'SSH public key resource': 'cloud',
        'SSH public key file': os.environ.get('TEST_KEY', ''),
        'GCP zone': 'us-central1-a',
        'Machine type': 'e2-standard-2',
        'Network:': 'default',
        'Subnetwork': '',
        'Network tags': 'web,ssh',
        'OpenStack flavor': 's1-8',
        'SSH key pair name': 'cloud',
        'OpenStack network name': 'Ext-Net',
        'Network UUID': '',
        'Custom NIC definitions': os.environ.get('TEST_NICS', ''),
    }
    value = next((value for name, value in values.items() if name in prompt), None)
    if value is None and 'Which ' in prompt:
        value = 'target one'
    if value is None:
        raise SystemExit(f'Unhandled input: {prompt}')
    print(value, file=sys.stderr, end='')
elif '--radiolist' in args:
    prompt = args[args.index('--radiolist') + 1]
    def security_group_mode():
        # TEST_SG_SEQUENCE answers successive asks in order (state in a file).
        sequence = os.environ.get('TEST_SG_SEQUENCE')
        if not sequence:
            return 'none' if os.environ.get('TEST_PLATFORM') == 'openstack' else 'names'
        state = os.environ['TEST_SG_STATE']
        asked = int(open(state).read()) if os.path.exists(state) else 0
        open(state, 'w').write(str(asked + 1))
        answers = sequence.split(',')
        return answers[min(asked, len(answers) - 1)]
    choices = {
        'Which VM': '_other',
        'Which security groups': security_group_mode() if 'Which security groups' in prompt else None,
        'Which instance type': '_blank',
        'How should the new instance get': 'dhcp',
        'How should MAC': os.environ.get('TEST_MAC_MODE', 'auto'),
        'Should cloud-init': 'none',
    }
    value = next((value for name, value in choices.items() if name in prompt), None)
    if value is None:
        raise SystemExit(f'Unhandled radio: {prompt}')
    print(value, file=sys.stderr, end='')
elif '--yesno' in args:
    prompt = args[args.index('--yesno') + 1]
    if os.environ.get('TEST_CANCEL') and 'Launch without an external IP' in prompt:
        sys.exit(255)
    sys.exit(0 if 'Use the shared launch network' in prompt or 'Request a floating' in prompt else 1)
'''

class LaunchDialogTests(unittest.TestCase):
    SCRIPTS = {
        "aws": "launch-aws-instance.sh",
        "azure": "launch-azure-instance.sh",
        "gcp": "launch-gcp-instance.sh",
        "openstack": "launch-openstack-instance.sh",
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
        timeout = stub_bin / "timeout"
        timeout.write_text("#!/bin/sh\nexit 1\n")
        timeout.chmod(0o755)
        ansible = stub_bin / "ansible-playbook"
        ansible.write_text("#!/bin/sh\npython3 -c 'import json,os,sys; json.dump(sys.argv[1:], open(os.environ[\"TEST_ARGV\"], \"w\"))' \"$@\"\n")
        ansible.chmod(0o755)
        home = self.tmp / "home"
        home.mkdir()
        key = self.tmp / "key.pub"
        key.write_text("ssh-ed25519 AAAA test\n")
        self.env = {
            **os.environ,
            "PATH": f"{stub_bin}:{os.environ['PATH']}",
            "HOME": str(home),
            "TMPDIR": str(self.tmp),
            "TEST_KEY": str(key),
            "TEST_ARGV": str(self.tmp / "argv.json"),
        }

    def run_dialog(self, platform, **overrides):
        env = {**self.env, "TEST_PLATFORM": platform, **overrides}
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(BIN / f"launch-{platform}-dialog.sh",
                      [str(BIN / f"launch-{platform}-dialog.sh")], env)
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
        return sorted(self.tmp.glob(f"launch-{platform}.*"))

    def test_each_dialog_generates_a_wrapper_command(self):
        for platform, wrapper in self.SCRIPTS.items():
            with self.subTest(platform=platform):
                files = self.run_dialog(platform)
                self.assertEqual(len(files), 1)
                command = files[0].read_text()
                self.assertIn("export VM_NAME='Web Server'\\''s 01'", command)
                self.assertIn(f"'{BIN / wrapper}'", command)
                if platform == "aws":
                    self.assertIn("export AWS_INSTANCE_TYPE=''", command)
                elif platform == "azure":
                    self.assertIn("export INSTANCE_NAME=''", command)
                    self.assertIn("export AZURE_SSH_KEY_NAME='cloud'", command)
                    self.assertIn("export AZURE_SSH_KEY_FILE=''", command)
                elif platform == "gcp":
                    self.assertIn("export GCP_SSH_KEY_FILE=''", command)
                subprocess.run(["bash", "-n", files[0]], check=True)
                files[0].unlink()

    def test_openstack_none_clears_ambient_groups_and_nics(self):
        files = self.run_dialog("openstack")
        command = files[0].read_text()
        self.assertIn("export OS_SECURITY_GROUPS_JSON='[]'", command)
        self.assertIn("export OS_NICS_JSON=''", command)
        self.assertIn("export OS_USER_DATA=''", command)
        env = {**self.env, "OS_SECURITY_GROUPS_JSON": '["ambient"]',
               "OS_NICS_JSON": '[{"net-id":"ambient"}]'}
        subprocess.run(["bash", files[0]], env=env, check=True,
                       capture_output=True, text=True, timeout=30)
        argv = json.loads((self.tmp / "argv.json").read_text())
        extra = {}
        for position, arg in enumerate(argv):
            if arg == "-e":
                extra.update(json.loads(argv[position + 1]))
        self.assertEqual(extra["openstack_security_groups"], [])
        self.assertEqual(extra["openstack_network"], "Ext-Net")
        self.assertNotIn("openstack_nics", extra)

    def test_openstack_custom_nics_reach_the_command(self):
        nics = '[{"net-id":"abc"}]'
        files = self.run_dialog("openstack", TEST_NICS=nics)
        self.assertIn(f"export OS_NICS_JSON='{nics}'", files[0].read_text())

    def test_openstack_preserved_macs_require_a_security_group(self):
        # Preserved MACs use pre-created ports, which the playbook builds only
        # with explicit security groups, so "None" is asked again.
        state = self.tmp / "sg-asked"
        files = self.run_dialog("openstack", TEST_MAC_MODE="preserve",
                                TEST_SG_SEQUENCE="none,names", TEST_SG_STATE=str(state))
        self.assertEqual(state.read_text(), "2")
        command = files[0].read_text()
        self.assertIn("export OS_SECURITY_GROUPS_JSON='[\"default\",\"web\"]'", command)
        self.assertIn("export OS_PRESERVE_MAC='true'", command)

    def test_cancel_stops_before_command_generation(self):
        self.assertEqual(self.run_dialog("gcp", TEST_CANCEL="1"), [])

if __name__ == "__main__":
    unittest.main()
