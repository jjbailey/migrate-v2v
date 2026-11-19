# Controller web UI

![The controller overview: migrate and launch cards for AWS, Azure, Google Cloud, and OpenStack, with recent jobs below](overview.svg)

The overview puts every destination one click away. Choose a cloud to migrate
a vSphere VM, then launch from its completed image. Azure and OpenStack also
offer datastore-direct migration. Recent jobs keep the controller's activity
in view. The SVG shows a new controller before its first job.

The site queues the repository's existing `bin/` migrate and launch wrappers.
A completed migration in the image catalog is required before launch. Each
controller uses its own vaults, and every local web account can use the
environments in those vaults.

Migrate and launch both begin with a vault project selector. Migration then
asks for the source vCenter and VM. Launch loads the selected project's
available subnets, security groups, key pairs, flavors, zones, machine
types, and networks using the same `bin/*-project-facts.py` tools as the
dialogs. Suggestions can be typed over when a known value is absent. AWS
security groups depend on the selected subnet; GCP machine types and
subnetworks depend on the selected zone and network. Use **Update choices**
after changing those fields. If a cloud lookup fails, the form offers
manual entry.

The form field set and wrapper names are read from the corresponding
`bin/*-dialog.sh` scripts when a page is requested. `web/forms.py` supplies
input labels, defaults, and validation. When changing a dialog, run `python3 -m
unittest discover -s web/tests -v` and review the web metadata. The test checks
hashes of all dialog scripts; after reconciling any changed wording, defaults,
and behavior, update `web/dialog-checksums.json`. If a dialog exports a new
variable without web metadata, the form refuses to load rather than silently
dropping it. The dialog script is authoritative if wording or behavior differs.

## Runtime

The pinned x86_64 project image is built from the repository-root
[Dockerfile](../Dockerfile) by Compose. It includes the project scripts,
playbooks, templates, web controller, and documentation, plus the runtime
dependencies. It pins the Ubuntu base image digest and APT snapshot, installs
the Python requirements and Ansible collections, builds the nbdkit VDDK plugin,
and stores the fixed libguestfs appliance in a sparse-aware compressed archive.
The controller unpacks it under `/tmp` at startup. The VDDK user kit and
virtio-win ISO still install on demand under the mounted artifact root.
Broadcom's OVF Tool is not included; standard export migrations download it on
demand after the operator accepts its license. See the
[runtime-image recipe](../doc/ubuntu-24.04-container-dependencies.md) for the
versions and build inputs.

To build or refresh the local image, run `docker compose --env-file web/.env
-f web/compose.yaml build controller`. `MIGRATE_V2V_RUNTIME_IMAGE` can name a
different tag for the image produced by this build. Run the service with a
UID/GID that can read the vault password file and write the artifact root. The
image carries the code, so it can run without mounting a source checkout.
Artifacts, catalogs, vaults, tools, and web state live under
`/work/migrate-v2v` and remain outside the image.

Files selected in the web forms (SSH public keys, a vCenter CA bundle,
OpenStack cloud-init data, or virtio-win driver files) must be placed under
`WEB_INPUT_DIR`, which defaults to `ARTIFACT_ROOT/web-inputs`
(`/work/migrate-v2v/web-inputs` in Compose). For an OpenStack datastore-direct
migration with guest conversion enabled, the host play installs Ubuntu's
`virt-v2v`, `libguestfs-tools`, and Windows `rhsrvany` packages if needed, then
builds the fixed appliance under `ARTIFACT_ROOT/libguestfs-appliance`. The web
image has those packages already and uses its baked appliance at
`/tmp/libguestfs-appliance`. Windows conversions download Fedora's stable
virtio-win ISO to `ARTIFACT_ROOT/web-inputs/virtio-win.iso` when missing; the
form pre-fills that path. The supplied Compose service runs as the host UID
with all capabilities dropped. The virtio-win field accepts an ISO file or an
unpacked driver directory under `WEB_INPUT_DIR`. The site resolves symlinks
and rejects input paths outside that directory, including vault and web-state
files. Do not put secrets in the input directory.

## Setup

1. Copy `web/.env.example` to `web/.env`, set the host UID/GID, artifact root,
   and absolute vault password file path. Keep `.env` private.

2. Build the runtime image:

   ```sh
   docker compose --env-file web/.env -f web/compose.yaml build controller
   ```

   For standard OVF/OVA exports, first review and accept Broadcom's OVF Tool
   license, then set `OVFTOOL_ACCEPT_LICENSE=1` in `web/.env`. The image does
   not include OVF Tool; the wrapper downloads the pinned version on demand.

3. Create a local account:

   ```sh
   docker compose --env-file web/.env -f web/compose.yaml run --rm controller python3 web/app.py user add operator
   ```

4. Start the site:

   ```sh
   docker compose --env-file web/.env -f web/compose.yaml up -d
   ```

Migration and launch wrappers can also run directly in the same image, for
example:

```sh
docker compose --env-file web/.env -f web/compose.yaml run --rm controller \
  ./bin/launch-openstack-instance.sh ...
```

The build context is the repository root. `.dockerignore` leaves out local
credentials, vaults, runtime data, private local scripts, and tests; those stay
on the host or in the mounted artifact directory.

5. Open `https://localhost:8443`. Caddy uses a per-install local certificate
   authority. Its root certificate is in the persistent `caddy-data` volume at
   `/data/caddy/pki/authorities/local/root.crt`; trust that root on each client
   that will open the site. Keep the CA private key in the volume private.

On a Linux host with `/dev/kvm`, guest conversion can use hardware acceleration.
Set `KVM_GID` in `web/.env` to the device's group ID (`stat -c '%g' /dev/kvm`),
then add `-f web/compose.kvm.yaml` to the Compose commands. Without that device,
libguestfs uses software emulation, which is slower.

By default Compose publishes HTTPS on `127.0.0.1` only. To also serve this
controller's own LAN IP, set `WEB_ADDRESS` in `web/.env` to that IP and
start with both files:

```sh
docker compose --env-file web/.env -f web/compose.yaml -f web/compose.lan.yaml up -d
```

Then open `https://CONTROLLER_IP:8443`. The configured IP must belong to
this controller. The controller HTTP process has no published port; Caddy is
the sole entry point. The site's local accounts use salted scrypt password
hashes, signed HttpOnly sessions, and CSRF tokens. Run `python3 web/app.py
user add NAME` in the controller service again to add or reset an account;
`user delete NAME` removes one.

Jobs are stored in `WEB_STATE_DIR/jobs.sqlite3` and processed one at a time.
`WEB_STATE_DIR` defaults to `/work/migrate-v2v/web-state`; it does not
follow `ARTIFACT_ROOT`, so set both when the artifact root lives elsewhere.
Job output is stored in mode `0600` log files there. A running job stopped
by a controller shutdown or restart is marked **interrupted** and is never
rerun automatically. Review its log and cloud state before submitting another
operation. The site does not offer arbitrary shell arguments or a terminal.

The **Maintenance** page shows concise migration and launch catalog entries
grouped by cloud provider in a print-friendly view, with a separate option
to inspect both catalog files as raw JSON. It can also clear finished job
records and their output logs; queued and running jobs are retained.

## Run directly on this host

The Python process can use the repository dependencies installed on the
host. From the repository root, set `VAULT_PASSWORD_FILE` to the existing
readable vault password file, then create a local account:

```sh
export VAULT_PASSWORD_FILE="$HOME/.dib-vault-pass"
python3 web/app.py user add operator
```

Start Caddy and the web backend together. If `local/run-caddy.sh` does not
exist yet, copy the example first; `-n` keeps an existing script:

```sh
cp -n examples/run-caddy-example.sh local/run-caddy.sh
```

Modify local/run-caddy.sh as required. To run both as services instead, see
the systemd units in [examples/README.md](../examples/README.md#caddy-on-the-ansible-control-host).

```sh
./local/run-caddy.sh
```

The script binds Caddy on all IPv4 and IPv6 interfaces and includes every
IP assigned to the host when it starts. Open `https://localhost:8443` or
`https://<controller-ip>:8443`. If the host's addresses change, restart the
script so Caddy can issue certificates for the new addresses. Caddy must be
installed separately; it is not bundled in the repository. Its local CA may
need to be trusted by each client browser. For temporary local development
without Caddy, set `WEB_SECURE_COOKIE=false`, start `python3 web/app.py
serve --host 127.0.0.1 --port 8080`, and open `http://127.0.0.1:8080`.
