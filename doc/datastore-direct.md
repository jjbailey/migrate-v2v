# Datastore-Direct Migration (OpenStack and Azure)

<!-- markdownlint-disable MD013 -->

`playbooks/migrate-openstack-vm-vddk.yml` is a second way to migrate a running
vSphere VM to OpenStack. It does not clone the VM and does not export an OVF/OVA
package. It takes a quiesced snapshot of the source VM, streams each frozen disk
straight from the datastore, converts it to QCOW2, and uploads it to Glance. The
existing `migrate-openstack-vm.yml` is unchanged; the two methods share the
catalog format, so launch, lookup, and reconcile work on either.

`playbooks/migrate-azure-vm-vddk.yml` does the same for Azure; see
[Azure](#azure) below. Everything in this document applies to both targets
unless that section says otherwise.

## How it works

```text
quiesced snapshot of the source VM          (no clone)
  → build the disk manifest from `govc vm.info` taken BEFORE the snapshot
  → per disk, one conversion ahead of the upload:
      nbdkit -r  vddk | curl   → read-only NBD export on a Unix socket
        → qemu-img convert -f raw -O qcow2  disks/disk-N.qcow2
        → openstack.cloud.image             (QCOW2 → Glance)
  → catalog entry                           (same schema as the OVF method)
  → snapshot removed                        (only after success)
```

Disk 0, the boot disk, is the disk named first in the VM's configured boot order
(`config.bootOptions.bootOrder`); when the VM sets none, it is the first disk by
controller and unit, the firmware default. The manifest records which as
`boot_order_source`.

Two backends serve the disk to `qemu-img`. Both read the snapshot's frozen data,
so the result is a point-in-time image; `quiesce_snapshot` (default true) makes it
application-consistent where the guest supports it. The source VM is never
powered off.

| Backend                         | How it reads                                                                                                                            | Needs                                 | Limits                                                                                      |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------- | ------------------------------------------------------------------------------------------- |
| `vddk` (default when available) | VixDiskLib through nbdkit's `vddk` plugin: vCenter login, ticket, NFC over TLS to the ESXi host, disks opened at the migration snapshot | nbdkit `vddk` plugin and a VixDiskLib | none of the curl limits                                                                     |
| `curl` (fallback)               | each `-flat.vmdk` file from vCenter's `/folder` endpoint with HTTP Range requests, through nbdkit's `curl` plugin                       | nothing beyond nbdkit                 | flat disks only, no existing snapshots; on VMFS the VM must be powered off; reads zeros too |

`vddk_backend` (wrapper: `BACKEND`) selects `auto` (the default: `vddk`
when the plugin and library load, else `curl`, with a message saying which of the
two is missing), `vddk`,
or `curl`. The manifest records the backend used as `transfer_backend`, and a
resume reads the disks with that same backend (see the resume notes below).

Measured on a running two-disk Debian VM (38 GB thin boot disk on an NFS
datastore): the `vddk` backend read the boot disk in about 4 minutes
(~150 MiB/s), the `curl` backend in about 15 (~40 MiB/s), and the two results
were byte-for-byte identical.

A QCOW2 file is still staged on the controller for each disk in flight (the
Glance module uploads from a file). Peak staging is one or two QCOW2 files, not a
full copy of the VM.

### Compression for slow upload links

`COMPRESS=true` (`-e openstack_convert_compress=true`, off by default) adds
`qemu-img convert -c`: clusters are compressed as the QCOW2 is written, in the
same single pass, so both the staging file and the upload shrink. Anything that
reads QCOW2 decompresses transparently; Glance stores the image as uploaded. On
the test VM the boot disk shrank from 2.62 GB to 1.03 GB, the conversion took
about as long, the whole migration over a slow uplink dropped from about 35 to
about 11 minutes, and an OVH instance booted from the compressed images. The
cost is controller CPU, so leave it off on a fast link.

### Guest conversion (virt-v2v-in-place)

By default only the disk format changes (VMDK to QCOW2); the guest has to boot
under KVM as it is. `GUEST_CONVERT=true` (`-e openstack_guest_convert=true`, off
by default) adds libguestfs's `virt-v2v-in-place`, the same guest conversion
that virt-v2v performs: it installs virtio drivers, removes VMware tools and
drivers, and fixes the boot configuration. This is what a guest that lacks
virtio in its initramfs, and every **Windows** guest, needs.

```text
stream and convert every disk → disks/disk-N.qcow2          (uncompressed)
  → virt-v2v-in-place -i disk disk-0.qcow2 disk-1.qcow2 …   (boot order)
  → [COMPRESS] qemu-img convert -c, per disk
  → openstack.cloud.image, per disk, as usual
```

virt-v2v-in-place inspects the guest across all of its disks (`/etc/fstab`,
`/boot`, the ESP), so the disks are converted together and **nothing is
uploaded until all of them are converted**. Unlike the default path, the
conversion of one disk does not overlap the upload of the previous one, and
peak staging is the whole VM's QCOW2 files. `COMPRESS` still works: the
compression runs after the in-place pass, because the pass rewrites clusters.

- **Windows** needs the virtio-win drivers (see [windows.md](windows.md) for the
  full picture). When the source VM is Windows, the
  play downloads the stable ISO published by the
  [Fedora virtio-win project](https://github.com/virtio-win/virtio-win-pkg-scripts#downloads)
  from its [stable ISO URL](https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso)
  if it is missing. It saves the ISO at
  `$ARTIFACT_ROOT/web-inputs/virtio-win.iso` (normally
  `/work/migrate-v2v/web-inputs/virtio-win.iso`), which is also the web form's
  default; set `WEB_INPUT_DIR` to use another input directory. The web form and
  command-line wrapper accept an ISO file or an unpacked driver directory. Set
  `VIRTIO_WIN=/path/to/virtio-win.iso` or
  `-e openstack_virtio_win=...` to use an existing ISO or directory instead.

  The ISO download and driver-path checks happen before the snapshot. A missing
  explicitly configured `VIRTIO_WIN` path still fails preflight. Windows
  installs the drivers on its first boot after launch, so expect a reboot.
  Launch the instance with a Windows-capable flavor; the images carry
  `os_type=windows`.

- The boot disk's Glance image gets `hw_disk_bus=virtio`, `hw_vif_model=virtio`,
  `os_type` (`windows` or `linux`), and `migrate_v2v_guest_converted=true`, so
  Nova presents the hardware the conversion assumed.
- `-e openstack_guest_convert_args='["--firstboot", "script.sh"]'` (a list, or a
  whitespace-separated string) is passed to `virt-v2v-in-place`, for example to
  run a script on first boot.
- A quiesced snapshot (`QUIESCE_SNAPSHOT`, the default) matters more here: a
  crash-consistent NTFS or ext4 volume can fail to mount cleanly for the
  conversion.
- Requirements: with `GUEST_CONVERT=true`, the OpenStack play installs Ubuntu's
  `virt-v2v` and `libguestfs-tools` packages if missing, then builds a fixed
  libguestfs appliance under
  `$ARTIFACT_ROOT/libguestfs-appliance`. The appliance lets the unprivileged
  migration user run conversions without read access to `/boot/vmlinuz`.
  Windows conversions also install `rhsrvany` and download missing drivers as
  described above.
  On a host, the first run needs sudo access to install system packages and
  builds the appliance under the artifact root. The web runtime image includes
  the packages and bundles its appliance as a sparse-aware compressed archive,
  unpacked at `/tmp/libguestfs-appliance` on startup; Compose sets
  `LIBGUESTFS_PATH` to that location by default. The artifact root must be
  writable and shared when switching between host and container. Set
  `LIBGUESTFS_PATH` to use another fixed appliance. The
  preflight checks that `virt-v2v-in-place` runs. libguestfs uses
  `LIBGUESTFS_BACKEND=direct`; without `/dev/kvm` it falls back to emulation,
  which is much slower.
- Resume: a marker file (`guest-converted.json`) in the run directory records a
  finished conversion. Resuming a converted run requires `GUEST_CONVERT=true`
  again. When some of the run's images are already in Glance, they are deleted
  and every disk is converted again, since the disks must be converted together.
  The `virt-v2v-in-place` output is saved as
  `virt-v2v-in-place.log` in the run directory. Failed resumable runs retain
  that log; a successful migration removes its derived run directory during
  cleanup.
- The Windows path has been exercised with `windows10-base`: its converted
  image boots in OpenStack and has network connectivity. The automated tests
  use a stand-in `virt-v2v-in-place`; a Linux guest that lacks virtio still
  needs an end-to-end check.

## OpenStack Usage

```bash
VM_NAME=my-vm VSPHERE_TARGET_VCENTER=my-vcenter ./bin/migrate-openstack-vm-vddk.sh
GUEST_CONVERT=true VM_NAME=my-vm ./bin/migrate-openstack-vm-vddk.sh     # virtio and VMware tools
GUEST_CONVERT=true VIRTIO_WIN=/iso/virtio-win.iso VM_NAME=my-win-vm ./bin/migrate-openstack-vm-vddk.sh
BACKEND=curl VM_NAME=my-vm ./bin/migrate-openstack-vm-vddk.sh           # force a backend
./bin/migrate-openstack-vddk-dialog.sh                                  # guided front end
```

The guided front end asks for the backend, whether to require source CBT,
whether to discard a failed run, an optional vCenter CA bundle, QCOW2
compression, and guest conversion.

The wrapper takes the same environment variables as `bin/migrate-openstack-vm.sh`
(`VM_NAME`, `VSPHERE_TARGET_VCENTER`, `VCENTER_DATACENTER`, `OS_PROJECT_NAME`,
`QUIESCE_SNAPSHOT`, `VALIDATE_CERTS`, `ARTIFACT_ROOT`), plus `BACKEND`,
`COMPRESS`, `GUEST_CONVERT` and `VIRTIO_WIN` (see above), `REQUIRE_CBT` (see
below), `DISCARD_RUN_ID`, and `VCENTER_CA_BUNDLE`.
Any Ansible arguments after the wrapper's own are passed through. The
datastore-direct settings are named `vddk_<name>` and shared by both targets;
the OpenStack playbook still accepts the older `openstack_vddk_<name>`
spelling. Other playbook settings: `vddk_libdir` (defaults to
`/work/migrate-v2v/tools/openvddk`; set empty to use the plugin's built-in default),
`vddk_transports` (default `nbdssl`), and
`vddk_thumbprint` (empty reads the vCenter certificate in preflight;
with `validate_certs` true the certificate must verify against this host's CAs
first, because VixDiskLib trusts whatever thumbprint it is given).
`vddk_ca_bundle` (wrapper: `VCENTER_CA_BUNDLE`) names a PEM file
holding the CA that issued the vCenter certificate, for a vCenter this host does
not already trust; the thumbprint check verifies against it and the curl backend
gets it as `cainfo=`, which needs an nbdkit curl plugin that accepts it
(preflight checks).

Resume a failed run with `-e resume_run_id=<id>` (printed on failure). The
snapshot is the resume checkpoint, so a failed run **keeps it**; a retained
snapshot keeps growing as the VM writes, so resume or discard promptly. To give
up on a failed run, discard it with `-e discard_run_id=<id>` (wrapper:
`DISCARD_RUN_ID=<id>`, with the same `VM_NAME`): that removes the run's snapshot,
its run directory, and any images it uploaded, including a partial upload, and
migrates nothing. The discard reports only what it actually removed. It fails,
rather than claiming success, when something could not be removed (it names
what is left; rerun it once the cause is fixed), when a lookup failed, or when
it found nothing at all for that run ID and VM name. The last usually means a
mistyped `DISCARD_RUN_ID` or `VM_NAME`, or a run already discarded.
(Passing `-e discard_failed_artifacts=true` to a new run does not touch an
earlier one.)

A resume reads the disks the way the failed run did, using the
`transfer_backend` its manifest records; `auto` follows the recorded value
rather than choosing again. Asking for the other backend explicitly fails, as
does a resume of a `vddk` run when the `vddk` plugin or library no longer
loads, because falling back to `curl` could read stale base disk files; the
message points to resuming with the recorded backend or discarding the run. A
manifest from before `transfer_backend` was recorded only draws a warning, and
the backend is chosen as for a new run. Whenever a resume uses `curl`, the
`curl` checks run again against the recorded disks, so a VM recorded with a
snapshot chain fails instead of being read from its base files.

A run that fails after taking its snapshot but before writing its manifest
cannot be resumed at all; the failure message says to discard it with
`-e discard_run_id=<id>` and start again. Do that promptly: the snapshot's
delta keeps growing until it is removed.

If a run fails while a disk is being uploaded, the next disk's background
stream and conversion are stopped before cleanup, so nothing keeps reading the
source or writing the staging directory while a resume or discard runs. If
`ansible-playbook` itself was killed and never reached that cleanup, the
resume or discard stops the streams the killed attempt left running before it
does anything else. Only an nbdkit still serving that run's own socket is
signalled, so a stale pidfile cannot stop an unrelated process.

Each disk's nbdkit log is kept in the run directory as `nbdkit-disk-<N>.log`,
and a failed conversion quotes it, so the failure says why the disk could not
be read (an untrusted certificate, a wrong URL, a refused login).

## Azure

`playbooks/migrate-azure-vm-vddk.yml` (wrapper `bin/migrate-azure-vm-vddk.sh`)
uses the same snapshot, manifest, backends, resume, and discard as
the OpenStack playbook. Only the per-disk output differs: `qemu-img`
writes each stream as the fixed-format VHD Azure requires (`-O vpc -o
subformat=fixed,force_size=on`, so the virtual size stays exact), which
is uploaded as a page blob. The boot disk becomes a managed image and data
disks become managed snapshots, as in `migrate-azure-vm.yml`.

```text
nbdkit -r  vddk | curl   → read-only NBD export on a Unix socket
  → qemu-img convert -f raw -O vpc (fixed)  disks/disk-N.vhd
  → page blob in the staging container → boot image or data snapshot
```

```bash
VM_NAME=my-vm AZURE_TARGET_PROJECT=my-sub ./bin/migrate-azure-vm-vddk.sh
BACKEND=curl VM_NAME=my-vm ./bin/migrate-azure-vm-vddk.sh            # force a backend
DISCARD_RUN_ID=<id> VM_NAME=my-vm ./bin/migrate-azure-vm-vddk.sh     # give up on a failed run
./bin/migrate-azure-vddk-dialog.sh                                   # guided front end
```

The wrapper takes the same environment variables as `bin/migrate-azure-vm.sh`
plus `BACKEND`, `REQUIRE_CBT`, `DISCARD_RUN_ID`, and `VCENTER_CA_BUNDLE`;
there is no `COMPRESS` (Azure needs an uncompressed fixed VHD). The manifest
records the source firmware, so the image's Hyper-V generation (V1 for BIOS,
V2 for UEFI) follows the VM as with the OVF method. A discard removes the run's
snapshot, run directory, staged blobs, managed image, and data snapshots, found
by the run-ID-derived names. A fixed VHD is staged at the disk's full virtual
size, written sparse, so it costs only the used blocks on the controller.

The Azure path shares `initialize`, `prepare-disks`, `import`, and
`import-disk` with the OVF method; `export_format: datastore` selects
`azure/convert-disk-vddk.yml` instead of `convert-disk.yml` and skips the OVF
VMDK checks. Tested end to end with the `vddk` backend on the same two-disk
Debian VM (UEFI, so a V2 image); resume, discard, and the `curl` backend have
not been exercised against Azure.

A clean Ubuntu 24.04.5 controller also migrated a one-disk, 40 GiB UEFI Ubuntu
26.04.1 VM with VDDK to a V2 Azure image. The snapshot and controller staging
files were removed after import. A `Standard_B2s_v2` VM launched without
a public IP, and its serial console reached the guest login prompt. At the
time of that test, Azure still reported `provisioningState=Creating` after
13 minutes, so the launch play was still waiting and no deployment catalog
entry had been written. This confirms image import and guest boot, but not
completed Azure provisioning; check the Azure guest setup requirement in
the README before treating launch as validated.

## Requirements

- On the control host: a Linux host (the stream bookkeeping reads `/proc` to
  confirm a pid still belongs to this run's socket, and relies on POSIX
  signals), `nbdkit`, `qemu-img`, `govc`, and the Ansible collections in
  `requirements.yml`. No `ovftool`.
- For the `vddk` backend: nbdkit's `vddk` plugin and a VixDiskLib (below).
- vCenter credentials from the vSphere vault under
  `/work/migrate-v2v/vaults/` by default, as for the other methods. The
  password is handed to nbdkit through a mode-0600 file, never on a command line;
  that file is per disk. The conversion shell removes it when the conversion
  ends; if the controller is killed first, the resume or discard that stops the
  leftover stream removes it, and cleanup removes the socket directory.
- The playbook refuses, before creating anything:
  - independent-persistent or independent-nonpersistent disks (a snapshot does
    not freeze them),
  - shared (multi-writer) disks, raw device mappings, and encrypted disks,
  - with the `curl` backend only: a VM that already has snapshots, and a disk
    file vCenter will not serve. On VMFS, ESXi locks a powered-on VM's
    `-flat.vmdk`, so `/folder` answers HTTP 500; power the VM off or use the
    `vddk` backend. NFS datastores serve a running VM's disks.

### Installing the `vddk` backend

nbdkit's `vddk` plugin loads VixDiskLib at run time from
`<libdir>/lib64/libvixDiskLib.so.8`. Ubuntu does not package the plugin; it
builds from nbdkit's source without VixDiskLib present. On Ubuntu 24.04, enable
the signed Ubuntu source packages and install the build dependencies for the
installed nbdkit version. `build-dep` installs compiler and development
packages, so this step needs several hundred MiB of additional disk space:

```bash
sudo sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/ubuntu.sources
sudo sed -i 's/^Components: main$/Components: main universe/' /etc/apt/sources.list.d/ubuntu.sources
sudo apt-get update
sudo apt-get install -y nbdkit nbdkit-plugin-dev pkg-config
sudo apt-get build-dep -y nbdkit

mkdir -p "$HOME/src/nbdkit-vddk" && cd "$HOME/src/nbdkit-vddk"
apt-get source nbdkit
src_dir=$(find . -mindepth 1 -maxdepth 1 -type d -name 'nbdkit-*' -print -quit)
cd "$src_dir"
./configure --disable-perl --disable-python --disable-ocaml --disable-rust \
    --disable-ruby --disable-tcl --disable-lua --disable-golang
for d in common/include common/replacements common/utils common/bitmap plugins/vddk ; do
    make -C "$d"
done
sudo install -m 0755 plugins/vddk/.libs/nbdkit-vddk-plugin.so "$(pkg-config --variable=plugindir nbdkit 2>/dev/null || echo /usr/lib/x86_64-linux-gnu/nbdkit/plugins)/"
```

The migration preflight installs OpenVDDK automatically when nbdkit's VDDK
plugin is present but cannot load a VixDiskLib. It downloads the fixed-name
runtime kit from the [official latest release asset](https://github.com/openvddk/openvddk/releases/latest/download/openvddk-runtime.tar.gz),
checks its SHA-256 against the pinned 0.7.5 kit, retains the kit's license and
notice files, and installs it under `/work/migrate-v2v/tools/openvddk` by
default. If upstream moves `latest` to a different kit, the checksum check
fails until the new version and checksum are reviewed. A host and a container
can share that install through the existing `/work/migrate-v2v` bind mount. The
kit is not stored in this repository or included in a container image. Set
`OPENVDDK_INSTALL_DIR` to choose a different writable location, or
`OPENVDDK_ARCHIVE` to install from a previously downloaded kit.
The installer never replaces a non-empty install directory. To upgrade from an
earlier kit (such as 0.7.4), move or remove the old directory first, or point
`OPENVDDK_INSTALL_DIR` at a new one.
The pinned kit supports Linux x86-64 with glibc 2.28 or newer.
`vddk_backend=auto` and `vddk_backend=vddk` may install it; explicitly selecting
`vddk_backend=curl` skips the VDDK probe and download.

To install it before the first migration, run:

```bash
./bin/install-openvddk.py
nbdkit vddk libdir=/work/migrate-v2v/tools/openvddk --dump-plugin | grep vddk_library_version
```

nbdkit looks for the OpenVDDK library under `lib64/`. Kits from 0.7.5 on
ship `lib64 -> lib` themselves, and the installer only verifies that the
link reaches `libvixDiskLib.so.8`; for older kits it creates the relative
link. Existing installations in other locations are still usable by setting
`OPENVDDK_INSTALL_DIR` to that `libdir`. In a container, keep the nbdkit VDDK
plugin in the image; the installer supplies only the library and its kit files.

The library tested is `libvixDiskLib.so.8.0.3` from OpenVDDK, an independent,
read-only replacement for VMware's disk library (HotAdd, NBDSSL, and NBD
transports). VMware's own VDDK, where you are licensed for it, works the
same way through `libdir`.

With the OpenVDDK build that supports `OpenVDDK.nbd.sessions`, each disk
uses **two parallel NFC sessions** over NBD/NBDSSL rather than the library's
default of one. The conversion task writes `OpenVDDK.nbd.sessions = "2"` to a
private per-disk configuration file and passes it to nbdkit's VDDK plugin with
`config=`. This requires the OpenVDDK library to be installed in the selected
`libdir`; the migration preflight installs it when it is missing. VMware's
library does not implement this OpenVDDK-specific setting. The curl fallback
and local-file tests are unchanged.

## CBT (Changed Block Tracking)

A full copy of a snapshot does not need CBT, so it is **off by default**.
`-e vddk_require_cbt=true` (or `REQUIRE_CBT=true`) makes the playbook
insist on it. The datastore-direct dialog and web form expose this option,
off by default. `bin/enable-cbt.py` (pyVmomi) can enable it ahead of time: it
reconfigures the VM without a power cycle and is idempotent. It takes
`--vm`. Exported `VCENTER_USERNAME` and `VCENTER_PASSWORD` (with
`VCENTER_HOSTNAME` or `--vcenter` for the host, and `--datacenter`) are used
when set; otherwise the vCenter, credentials, datacenter, and
`validate_certs` come from the vSphere vault (`--vcenter
<key-or-hostname>` picks an entry, as `vsphere_target_vcenter` does).
`--insecure` skips certificate checks.

Setting `ctkEnabled` in the VM's ExtraConfig (for example with `govc vm.change
-e`) does **not** work: vCenter leaves `changeTrackingEnabled` false. govc has no
`-cbt` option. CBT is only useful for incremental migrations, which are not
implemented: they would also need the snapshot's change ID saved in the catalog.

## Files

| File                                                                                                      | Role                                                                                    |
| --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `playbooks/migrate-openstack-vm-vddk.yml`                                                                 | entry playbook and settings                                                             |
| `bin/migrate-openstack-vm-vddk.sh`, `bin/migrate-openstack-vddk-dialog.sh`                                | wrapper and guided front end                                                            |
| `bin/inspect-vm.py`                                                                                       | builds the disk manifest from `govc vm.info -json`; refuses unsupported disks           |
| `bin/enable-cbt.py`                                                                                       | optional CBT helper                                                                     |
| `playbooks/tasks/migration/common/prepare-source-manifest.yml`                                            | snapshot and manifest                                                                   |
| `playbooks/tasks/migration/common/snapshot-ref-vddk.yml`                                                  | the snapshot's reference for VixDiskLib                                                 |
| `playbooks/tasks/migration/common/{preflight,resume,cleanup}-vddk.yml`                                    | copies of the OVF method's tasks without the OVF/clone assumptions                      |
| `playbooks/tasks/migration/common/check-curl-disks-vddk.yml`                                              | the `curl` backend's disk checks, run before the snapshot and again on a `curl` resume  |
| `playbooks/tasks/migration/common/{select-backend,stop-streams}-vddk.yml`                                 | nbdkit and backend choice; stopping streams after a failure                             |
| `playbooks/tasks/migration/openstack/{initialize,preflight,import,import-disk,convert-disk}-vddk.yml`     | OpenStack per-disk stream, convert, and upload                                          |
| `playbooks/tasks/migration/openstack/guest-convert*-vddk.yml`                                             | optional virt-v2v-in-place setup, pre-snapshot checks, disk conversion, and compression |
| `playbooks/migrate-azure-vm-vddk.yml`, `bin/migrate-azure-vm-vddk.sh`, `bin/migrate-azure-vddk-dialog.sh` | Azure entry playbook, wrapper, and guided front end                                     |
| `playbooks/tasks/migration/azure/{preflight,convert-disk,discard}-vddk.yml`                               | Azure preflight, per-disk stream to fixed VHD, and discard lookup                       |

The `-vddk` task files are deliberate copies of their OVF counterparts, so each
method can change without touching the other. A fix to the shared upload or
overlap logic should be considered for both. The tasks that need no change
(`check-image`, `record-image`, `build-catalog-entry`, `append-catalog`,
`load-manifest`, `validate-disk-order`, `openstack/cleanup`) are imported as is.

The catalog entry keeps a `clone_name` field because the catalog and its readers
key on it; here it is only an identifier (`<vm>-openstack-vddk-<run id>`), and no
such VM exists.

## Tested

On a running two-disk Debian VM (38 GB thin boot disk, 1 GB data disk) on an NFS
datastore:

- both backends: snapshot, stream, convert, upload, catalog entry, and snapshot
  and temporary-file cleanup; the two backends' boot-disk images were
  byte-for-byte identical;
- instances launched on OVH booted with both disks attached, from the `curl`
  backend's images and from the `vddk` backend's compressed images.

Not yet tested: resume after a failure, VMs that already have snapshots (`vddk`
backend), and VMFS, vSAN, or vVol datastores. VixDiskLib's allocated-block
query is used, but the NFS datastore reported every block allocated, so
zeros were still read.

**Resume is therefore experimental.** The retention, `resume_run_id`,
and `discard_run_id` paths are implemented and the backend used by a
run is pinned in its manifest so a resume cannot silently read a disk
differently, but a resumed datastore-direct run has not been exercised
against a real vCenter. `tests/test_vddk_resume.py` runs the real
resume tasks against a stub `govc` and a fake Glance: a failed upload
followed by a resume, snapshot and manifest checks, discard, and backend
pinning. `tests/test_vddk_resume_azure.py` does the same for the Azure
import and discard against a fake `azure.azcollection`. Neither replaces a
real run. Prefer `discard_run_id` plus a fresh run until it has been.
