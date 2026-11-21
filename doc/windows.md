# Windows guests

This page describes the Windows-specific handling implemented by this
repository. Its only in-repository Windows conversion is for **OpenStack**,
through datastore-direct migration with guest conversion. The other migration
playbooks do not run this conversion or inject the Fedora `virtio-win` drivers;
their destination import workflows and Windows guest requirements differ.
Everything else about migrating a VM is covered in the
[README](../README.md) and [datastore-direct.md](datastore-direct.md).

## Support at a glance

| Target                                                | What this repository does                                                          | Validation status                                                                                     |
| ----------------------------------------------------- | ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| OpenStack, datastore-direct with `GUEST_CONVERT=true` | Injects OpenStack virtio drivers and removes VMware tools (details below).         | Tested with **Windows 10 Pro** only.                                                                  |
| OpenStack, standard (OVF) path                        | Refuses a Windows guest before taking a snapshot.                                  | Unsupported by this path because it cannot perform the required KVM driver conversion.                |
| OpenStack, datastore-direct without `GUEST_CONVERT`   | Refuses a Windows guest before taking a snapshot.                                  | Unsupported without guest conversion because the resulting image would lack the required KVM drivers. |
| AWS                                                   | Passes the OVA to EC2 VM Import/Export; the playbook does not inject `virtio-win`. | End-to-end Windows migration and launch have not been tested in this project.                         |
| Azure                                                 | Converts and registers the VHD; the playbook does not modify Windows drivers.      | End-to-end Windows migration and launch have not been tested in this project.                         |
| Google Cloud                                          | Uses Migrate to Virtual Machines image import; it does not inject `virtio-win`.    | End-to-end Windows migration and launch have not been tested in this project.                         |

Only Windows 10 Pro on OpenStack has been tested end to end by this project.
Other Windows versions and all three AWS, Azure, and Google Cloud paths remain
unverified here. “Unverified” does not mean those providers reject Windows;
their own import requirements and guest adaptations still apply.

## Why OpenStack needs drivers

OpenStack on KVM presents virtio disks and network adapters. A Windows VM
migrated from vSphere (which uses VMware's SCSI and network hardware) needs the
OpenStack virtio drivers installed before it can boot and use its network.
This repository injects those drivers only on the OpenStack conversion path.

AWS and Google Cloud use provider-managed import adaptation for Windows:
[EC2 VM Import/Export](https://docs.aws.amazon.com/vm-import/latest/userguide/import-modify-vm.html)
modifies Windows boot settings, and [Google Migrate to Virtual Machines](https://cloud.google.com/migrate/virtual-machines/docs/5.0/resources/vm-adaptations)
performs Windows OS adaptations during image import. Google Compute Engine
also maintains its own Windows storage and network drivers, including
VirtIO-based drivers; these are distinct from the Fedora `virtio-win` ISO used
by this project's OpenStack conversion. The Azure path here creates an image
directly and does not run Azure Migrate's guest adaptation workflow, so its
Windows boot and guest-agent requirements have not been verified by this
project.

## How the conversion works

With `GUEST_CONVERT=true`, the migration runs libguestfs's `virt-v2v-in-place`
over every disk of the VM before anything is uploaded. For a Windows guest it:

- installs the virtio drivers from the **virtio-win** ISO into the offline
  Windows image,
- removes VMware Tools and its drivers, and
- installs a first-boot helper (`rhsrvany`, a Windows service wrapper) so the
  conversion's first-boot work runs when Windows first starts under KVM.

The boot disk's Glance image is then tagged `os_type=windows`,
`hw_disk_bus=virtio` and `hw_vif_model=virtio`, so Nova presents the hardware
the conversion assumed. See
[Guest conversion](datastore-direct.md#guest-conversion-virt-v2v-in-place) for
the flow, staging and resume behaviour.

## Before you migrate

1. **Use the datastore-direct OpenStack migration** with guest conversion
   enabled:

   ```sh
   GUEST_CONVERT=true VM_NAME=my-win-vm ./bin/migrate-openstack-vm-vddk.sh
   ```

   The interactive `migrate-openstack-vddk-dialog.sh` and the web form ask the
   same question; answer **Yes** to guest conversion.

2. **Provide the virtio-win drivers.** If you do nothing, the play downloads
   Fedora's stable `virtio-win.iso` to
   `$ARTIFACT_ROOT/web-inputs/virtio-win.iso` before the snapshot is taken
   (normally `/work/migrate-v2v/web-inputs/virtio-win.iso`). To use your own
   copy, or on a controller without internet access, download the ISO from the
   [Fedora virtio-win project](https://github.com/virtio-win/virtio-win-pkg-scripts#downloads)
   and set `VIRTIO_WIN=/path/to/virtio-win.iso` (or
   `-e openstack_virtio_win=...`). An unpacked driver directory also works. In
   the web form, the file must be under `WEB_INPUT_DIR`.

   The download is not checksum-verified, so use your own vetted ISO if that
   matters in your environment. A `VIRTIO_WIN` path you set that does not exist
   fails preflight, before any snapshot or upload.

3. **Controller requirements.** The play installs `virt-v2v`,
   `libguestfs-tools` and `rhsrvany` (Ubuntu packages) if missing and builds a
   libguestfs appliance under `$ARTIFACT_ROOT/libguestfs-appliance`, which needs
   sudo on a host. The container image already includes the packages and a
   prebuilt appliance. `/dev/kvm` is optional but makes the conversion much
   faster; see [web/docker.md](../web/docker.md#optional-compose-overrides).

4. **Keep the quiesced snapshot** (`QUIESCE_SNAPSHOT`, the default). A
   crash-consistent NTFS volume can fail to mount cleanly for the conversion.

5. **Allow extra staging space and time.** Every disk is converted before any is
   uploaded, so the staging area must hold the whole VM's QCOW2 files.

## After launch

- Launch with a **Windows-capable flavor** (enough RAM and disk for the guest).
- Expect **one or more reboots** on first boot while Windows installs the
  drivers.
- Connect with **RDP** (TCP 3389), not SSH: make sure the instance's security
  group allows it. The launch step's printed ssh command is meant for Linux
  guests and does not apply to Windows.
- This tool does nothing at launch to set a Windows administrator password or
  hostname. Sign in with the credentials the VM had in vSphere. Static IP
  settings, domain membership and licensing/activation after the move have not
  been checked.

## Troubleshooting

- **Fails before the snapshot with "no virtio-win drivers":** the ISO path does
  not exist or the download failed. Supply `VIRTIO_WIN`.
- **Conversion fails:** the full output is saved as `virt-v2v-in-place.log` in
  the run directory, and its last lines appear in the play's error. Failed runs
  that can be resumed keep the log; a successful migration removes the run
  directory.
- **Windows boots to a recovery screen or `INACCESSIBLE_BOOT_DEVICE`:** guest
  conversion was almost certainly off, or the image was launched without the
  virtio properties. Check the image has `hw_disk_bus=virtio`,
  `hw_vif_model=virtio` and `os_type=windows`, and re-migrate with
  `GUEST_CONVERT=true`.
- **No network after boot:** give Windows time to finish its first boot and any
  reboots; then check the instance's port and security groups.
