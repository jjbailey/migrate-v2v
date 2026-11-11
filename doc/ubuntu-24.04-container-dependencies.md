# Ubuntu 24.04 Container Dependency Plan

This is preparation only. It records the dependency plan for a future
container; it does not add a Dockerfile or Compose configuration.

## Target

- Ubuntu 24.04 LTS (`noble`), `amd64`.
- Use a glibc-based image. The current OpenVDDK kit supports Linux x86-64 and
  glibc 2.28 or newer; the user's Ubuntu 24.04 host meets that baseline. Recheck
  this against the author's renamed release before adopting it.
- The current host reports Ubuntu 24.04.5, Python 3.12.3, `nbdkit`
  1.36.3-1ubuntu10, and `qemu-utils` 8.2.2. These are observations, not
  selected image pins.

## Runtime Dependencies

- **Python and Ansible:** `python3`, `python3-venv`; create one venv for
  Ansible and the `python3` scripts. Applies to all workflows.
- **CA trust and download tools:** `ca-certificates`, `curl`, `gnupg`.
- **Disk conversion:** `qemu-utils` (`qemu-img`) for OpenStack and Azure.
- **Datastore-direct:** `nbdkit` for OpenStack and Azure direct workflows.
- **Certificate inspection:** `openssl` for vCenter preflight, especially
  datastore-direct.
- **Shell commands:** `bash`, `coreutils`, `findutils`, `grep`, `sed`, and
  `gawk` for wrappers. The private scripts under `local/` are out of scope.
- **Guest staging over SSH:** `openssh-client` for `stage-v2v.yml`.
- **AWS API and chunked uploads:** AWS CLI v2 for AWS migrations. AWS's Linux
  installer also requires `unzip`, `groff`, and `less`.
- **Google API and chunked uploads:** `google-cloud-cli` for GCP migrations.
- **vSphere API helper:** a pinned `govc` Linux release binary for all
  migrations.
- **Interactive menus:** `dialog` or `whiptail` for dialog wrappers only.

Ubuntu's `nbdkit` package on the current host owns and supplies the curl
plugin. The package does not supply the VDDK plugin: the host's installed
`nbdkit-vddk-plugin.so` is not owned by an Ubuntu package. Ubuntu package
details are available for [`nbdkit`](https://packages.ubuntu.com/noble/nbdkit),
[`nbdkit-plugin-dev`](https://packages.ubuntu.com/noble/nbdkit-plugin-dev), and
[`qemu-utils`](https://packages.ubuntu.com/noble/qemu-utils).

Scripts under `local/` are private tests and are out of container scope. The
public `bin/` dialog wrappers remain in scope; their `dialog` or `whiptail`
dependency is optional unless a dialog is used.

`az` and the OpenStack CLI are not needed by the migration and launch
playbooks. Azure's service principal and SDK are still required for the Azure
Ansible collection.

## Python and Ansible

Use one virtual environment for both `ansible-playbook` and scripts invoked as
`python3`; put its `bin/` first on `PATH`. This matches the repository's
validation requirement that both interpreters have the project's Python
dependencies.

- Install the pinned dependencies in [`requirements.txt`](../requirements.txt).
- Install the pinned collections in [`requirements.yml`](../requirements.yml).
- Install the Python SDK requirements shipped by `azure.azcollection` into
  that same environment, using
  [`requirements-azure-constraints.txt`](../requirements-azure-constraints.txt)
  to pin the collection's range-only requirements to the active controller's
  versions. The collection's exact requirements remain pinned by its own file.
- Install collections in a fixed image path or the final runtime user's home,
  so the build user's Ansible collection directory is not accidentally used.

These are direct requirement pins, not a complete hash-locked transitive
Python lockfile. The image build should still resolve and record the complete
dependency closure if byte-for-byte rebuilds are required.

Python 3.12 is the Ubuntu 24.04 host baseline. Keep the Ansible and collection
pins together; the repository documents the `community.vmware` and
`ansible-core` compatibility constraint in `requirements.yml`.

## Versioned External Tools

The active, fully patched controller reports `govc 0.33.0`, AWS CLI v2
`2.35.21`, and Google Cloud CLI `592.0.0` (Ubuntu package `592.0.0-0`).

- **OVF Tool:** Keep it out of the image. The existing installer downloads the
  validated 5.1.0 package under `/work/migrate-v2v/tools/ovftool-5.1.0` after
  license acceptance. Preserve the external download and persistent install
  model.
- **OpenVDDK successor:** Keep its user kit out of the image and install it
  under the shared `/work/migrate-v2v/tools/` mount. The author has not
  published the renamed release yet, so its release name, archive, checksum,
  library SONAME, and any changed configuration interface remain open. Do not
  pin the current beta as the future container contract.
- **`govc`:** Pin the installed version, `0.33.0`, and verify the selected
  release binary's checksum. The [upstream installation guide][govc-install]
  publishes Linux release binaries.
- **AWS CLI v2:** Pin the installed version, `2.35.21`, using AWS's
  version-selectable Linux installer and verify its checksum. See the
  [AWS CLI v2 installation guide][aws-install].
- **Google Cloud CLI:** Pin the installed version, `592.0.0` (Ubuntu package
  `592.0.0-0`), from Google's APT repository. See [Google's Debian and Ubuntu
  installation instructions][gcloud-install].

## Datastore-Direct VDDK Plugin

The image needs nbdkit's VDDK plugin when datastore-direct VDDK migrations are
in scope. Build the plugin against the same Ubuntu 24.04 nbdkit release used at
runtime, and install it in that release's plugin directory. The existing
[manual build instructions](datastore-direct.md#installing-the-vddk-backend)
download the matching nbdkit source and build the VDDK plugin. The exact
build-only package list still needs to be confirmed from that procedure.

Likely build-stage tools are `build-essential`, `pkg-config`, `curl`, `gnupg`,
and `ca-certificates`. Keep compiler and source-fetch tools out of the final
runtime image if a multi-stage build can copy the resulting plugin and its
needed runtime libraries cleanly. Check the plugin's dynamic dependencies and
run `nbdkit vddk --dump-plugin` in the finished image.

OpenVDDK is a separate runtime library loaded by this plugin. It should not be
compiled into or copied into the image. Once the renamed release is available,
check that its library still exports the VixDiskLib ABI and expected SONAME;
if those change, the plugin or installer may need an update as well.

## Runtime Inputs

- Mount `/work/migrate-v2v` read-write at the same container path. It stores
  artifacts, catalogs, vaults, and the on-demand OVF Tool and VDDK libraries.
- Mount the vault password file read-only at
  `/run/secrets/migrate-v2v-vault-password` and set `VAULT_PASSWORD_FILE` to
  that path. Do not place the password in the image or environment value.
- Run with a UID/GID that can write the workspace and read the secret file.
- Allow network access to vCenter, the selected cloud APIs, and the ESXi hosts
  used by VDDK's NFC data path.

## Decisions Still Open

1. Wait for the renamed OpenVDDK release, then pin its exact archive and
   checksum and validate its library ABI with nbdkit.
2. Confirm the exact nbdkit plugin build dependencies.
3. When the image is eventually built, pin the Ubuntu base digest and APT
   package versions to make rebuilds repeatable.

The Azure collection's direct SDK requirements are pinned using the current
controller's versions. A complete hash-locked transitive Python dependency
closure is optional extra hardening if byte-for-byte rebuilds are required.

[govc-install]: https://github.com/vmware/govmomi/blob/main/govc/README.md
[aws-install]: https://docs.aws.amazon.com/cli/latest/userguide/
[gcloud-install]: https://docs.cloud.google.com/sdk/docs/install-sdk
