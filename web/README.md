# Controller web UI

The site queues the repository's existing `bin/` migrate and launch
wrappers. It offers AWS, Azure, GCP, and OpenStack; Azure and OpenStack also
offer datastore-direct VDDK migration. A completed migration in the image
catalog is required before launch. Each controller uses its own vaults,
and every local web account can use the environments in those vaults.

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

The web process uses only Python's standard library; reading
Ansible Vaults and the migration catalog needs the repository's
existing Python/Ansible dependencies. The actual migration and launch
wrappers require the full runtime listed in [the container dependency
plan](../doc/ubuntu-24.04-container-dependencies.md). That image has
not yet been built in this repository. `compose.yaml` therefore requires
`MIGRATE_V2V_RUNTIME_IMAGE` to name a prepared image with those tools and
the Python environment on `PATH`. The VDDK kit and OVF Tool continue to
install under the mounted artifact root.

The image must include `python3`, `ansible-vault`, PyYAML, and all tools
needed by the selected workflows. Run with a UID/GID that can read the vault
password file and write the artifact root. The repository itself is mounted
read-only; artifacts, catalog, vaults, tools, and web state live under
`/work/migrate-v2v`.

Files selected in the web forms (SSH public keys, a vCenter CA bundle,
or OpenStack cloud-init data) must be placed under `WEB_INPUT_DIR`, which
defaults to `ARTIFACT_ROOT/web-inputs` (`/work/migrate-v2v/web-inputs` in
Compose). The site resolves symlinks and rejects files outside that directory,
including vault and web-state files. Do not put secrets in the input directory.

## Setup

1. Copy `web/.env.example` to `web/.env`, set the runtime image, host UID/GID,
   artifact root, and absolute vault password file path. Keep `.env` private.

2. Create a local account:

   ```sh
   docker compose --env-file web/.env -f web/compose.yaml run --rm controller python3 web/app.py user add operator
   ```

3. Start the site:

   ```sh
   docker compose --env-file web/.env -f web/compose.yaml up -d
   ```

4. Open `https://localhost:8443`. Caddy uses a per-install local certificate
   authority. Its root certificate is in the persistent `caddy-data` volume at
   `/data/caddy/pki/authorities/local/root.crt`; trust that root on each client
   that will open the site. Keep the CA private key in the volume private.

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
