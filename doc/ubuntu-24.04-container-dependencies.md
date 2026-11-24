# Ubuntu 24.04 Pinned Runtime Image

The project image is built from the repository-root [`Dockerfile`](../Dockerfile).
Compose defaults to the local tag `migrate-v2v-runtime:ubuntu24.04-20261117`.
Build or refresh it with `docker compose --env-file web/.env -f
web/compose.yaml build controller` from the repository root. The recipe is
x86_64-only and pins the Ubuntu base image digest, Ubuntu APT snapshot, Python
requirements, Ansible collections, govc, AWS CLI, and Google Cloud CLI.

## Target

- Ubuntu 24.04 LTS (`noble`), `amd64`.
- The base is the official Ubuntu 24.04 amd64 image pinned to
  `sha256:a2d43bffc46670d7108bb3c0468c67c5557c4c92e83e1e34cbff1322ad1b94dc`.
- Ubuntu packages come from the archive snapshot `20261117T000000Z`, including
  the VDDK plugin build dependencies. Update the snapshot and digest together
  after rebuilding and reviewing the image.
- The minimal Ubuntu base has no CA bundle. The Dockerfile first installs
  `ca-certificates` from Ubuntu's default HTTP archive (whose package metadata
  is signed), then installs the matching snapshot version before using HTTPS
  sources. This allows APT to verify the snapshot service's TLS certificate
  without disabling certificate checks.

## Runtime Dependencies

- **Python and Ansible:** `python3`, `python3-venv`; create one venv for the
  repository's pinned Python requirements and Python scripts. Install pinned
  Ansible collections into `/opt/ansible/collections`.
- **CA trust and download tools:** `ca-certificates` and `curl`. GnuPG and
  `unzip` are used to verify and install AWS CLI during the image build, then
  removed from the final image.
- **Disk conversion:** `qemu-utils` (`qemu-img`) for OpenStack and Azure.
- **Datastore-direct:** `nbdkit` for OpenStack and Azure direct workflows.
- **Guest conversion:** the image includes `virt-v2v`, `libguestfs-tools`, and
  `rhsrvany`. It stores the fixed appliance as a sparse-aware xz archive and
  unpacks it to `/tmp/libguestfs-appliance` at container startup. The build
  removes supermin's duplicate appliance cache. Windows conversions download
  the stable `virtio-win` ISO from the
  [Fedora virtio-win project](https://github.com/virtio-win/virtio-win-pkg-scripts#downloads)
  to `ARTIFACT_ROOT/web-inputs/virtio-win.iso` when it is missing.
  The image build temporarily installs Ubuntu's virtual kernel so supermin can
  construct the fixed appliance, archives the sparse raw root with GNU tar's
  sparse-file support, removes the duplicate supermin cache, then removes the
  kernel package. This avoids storing two 4 GiB logical appliance roots in the
  image while keeping the raw format used by Ubuntu's libguestfs package.
- **Certificate inspection:** `openssl` for vCenter preflight, especially
  datastore-direct.
- **Shell commands:** `bash`, `coreutils`, `findutils`, `grep`, `sed`, and
  `gawk` for wrappers. The private scripts under `local/` are out of scope.
- **Guest staging over SSH:** `openssh-client` for `stage-v2v.yml`.
- **AWS API and chunked uploads:** AWS CLI v2 for AWS migrations. AWS's Linux
  installer also requires `unzip`, `groff`, and `less`.
- **Google API and chunked uploads:** the image includes `google-cloud-cli` for
  GCP migrations. The GCP plays also install the pinned CLI on demand under
  `ARTIFACT_ROOT/tools/` when it is absent, which supports container variants
  that omit it.
- **vSphere API helper:** `govc`, built from the pinned Go module version for
  all migrations.
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
- For the container, install the reduced, pinned Azure SDK set in
  [`requirements-container-azure.txt`](../requirements-container-azure.txt),
  constrained by
  [`requirements-azure-constraints.txt`](../requirements-azure-constraints.txt).
  The upstream Azure collection's full requirements cover modules this project
  does not use, so the container list tracks the imports in its common utility
  and the Azure modules used by the shipped plays. Revisit it when either set
  changes. The host `.dib7` environment and its full collection requirements are
  unchanged.
- Install collections in a fixed image path or the final runtime user's home,
  so the build user's Ansible collection directory is not accidentally used.

These are direct requirement pins, not a complete hash-locked transitive
Python lockfile. The image build should still resolve and record the complete
dependency closure if byte-for-byte rebuilds are required.

Python 3.12 is the Ubuntu 24.04 host baseline. Keep the Ansible and collection
pins together; the repository documents the `community.vmware` and
`ansible-core` compatibility constraint in `requirements.yml`.

## Versioned External Tools

The image pins `govc 0.33.0`, AWS CLI v2 `2.35.21`, and Google Cloud CLI
`594.0.0`. These versions match the repository's tested CLI contract except
Google Cloud CLI, which is updated to the version available in the archive
when this recipe was added.

- **OVF Tool:** Keep it out of the image. The existing installer downloads the
  validated 5.1.0 package under `/work/migrate-v2v/tools/ovftool-5.1.0` on the
  first standard export migration. Broadcom states that downloading,
  installing, or using the tool means agreeing to its terms; the project
  defaults `OVFTOOL_ACCEPT_LICENSE` to `1` when the operator runs these
  workflows. Preserve the external download and persistent install model.
- **OpenVDDK:** Keep its user kit out of the image and install it under the
  shared `/work/migrate-v2v/tools/` mount with `bin/install-openvddk.py`, which
  downloads the upstream `latest` runtime asset and verifies it against the
  pinned 0.7.5 SHA-256. If upstream moves `latest`, the new archive, checksum,
  library SONAME, and any changed configuration interface must be re-checked
  before updating the pin.
- **`govc`:** Build `govc` from the pinned `v0.33.0` Go module. The Go module
  checksum database verifies downloaded source.
- **AWS CLI v2:** Install version `2.35.21` from the versioned Linux archive;
  verify its PGP signature against the AWS CLI Team key pinned by fingerprint
  in the Dockerfile. See the [AWS CLI v2 installation guide][aws-install].
- **Google Cloud CLI:** The image and GCP plays use version `594.0.0` from
  Google's versioned archive, checked against its pinned SHA256. The play
  stores its copy under the writable artifact mount. See [Google's versioned
  archive guidance][gcloud-install].

## Datastore-Direct VDDK Plugin

The Dockerfile uses Ubuntu's signed source package and `apt-get build-dep` to
build nbdkit's VDDK plugin in a separate stage, then copies the plugin into the
matching runtime nbdkit plugin directory. The compiler and source build
dependencies do not enter the final image. The OpenVDDK user kit remains a
separate, checksum-verified on-demand install under the shared artifact root.

OpenVDDK is a separate runtime library loaded by this plugin. It should not be
compiled into or copied into the image. If the pinned kit changes,
check that its library still exports the VixDiskLib ABI and expected SONAME;
if those change, the plugin or installer may need an update as well.

## Runtime Inputs

- Mount `/work/migrate-v2v` read-write at the same container path. It stores
  artifacts, catalogs, vaults, and on-demand tools. Project scripts, playbooks,
  templates, and the web controller are included in the image. The fixed
  libguestfs appliance is bundled as a compressed archive and unpacked under
  `/tmp/libguestfs-appliance` at startup; set `LIBGUESTFS_PATH` to override it.
- Mount the vault password file read-only at
  `/run/secrets/migrate-v2v-vault-password` and set `VAULT_PASSWORD_FILE` to
  that path. Do not place the password in the image or environment value.
- Run with a UID/GID that can write the workspace and read the secret file.
- For libguestfs hardware acceleration, optionally expose `/dev/kvm` with
  `web/compose.kvm.yaml` and pass its group ID through `KVM_GID`. Without it,
  libguestfs falls back to software emulation.
- Allow network access to vCenter, the selected cloud APIs, and the ESXi hosts
  used by VDDK's NFC data path.

The project requirements and selected Azure SDK packages are version-pinned.
Pip still resolves transitive packages at build time; a hash-locked Python
dependency closure would make builds more reproducible.
OVF Tool stays outside the image because Broadcom does not permit bundling it
for redistribution. Standard web migrations download it on demand and default
`OVFTOOL_ACCEPT_LICENSE` to `1`, consistent with Broadcom's statement that
downloading, installing, or using the tool means agreeing to its terms.

[govc-install]: https://github.com/vmware/govmomi/blob/main/govc/README.md
[aws-install]: https://docs.aws.amazon.com/cli/latest/userguide/
[gcloud-install]: https://docs.cloud.google.com/sdk/docs/install-sdk
