# Running the controller in Docker

This guide runs the controller web app, and by default a Caddy HTTPS front
end, as containers with Docker Compose. It is one of two supported ways to run
the controller; the other is directly on the Ansible control host (see
[Running the controller](README.md#running-the-controller)). Caddy is a
separate choice: the Compose file includes a Caddy container, but you can
instead run Caddy on the host and publish only the app. See
[caddy.md](caddy.md).

## The runtime image

The pinned x86_64 project image is built from the repository-root
[Dockerfile](../Dockerfile) by Compose. It includes the project scripts,
playbooks, templates, web controller, and documentation, plus the runtime
dependencies, including Google Cloud CLI 594.0.0 for GCP migrations. It pins
the Ubuntu base image digest and APT snapshot, installs
the Python requirements and Ansible collections, builds the nbdkit VDDK plugin,
and stores the fixed libguestfs appliance in a sparse-aware compressed archive.
The controller unpacks it under `/tmp` at startup. The VDDK user kit and
virtio-win ISO still install on demand under the mounted artifact root.
Broadcom's OVF Tool is not included; standard export migrations download it on
demand. Broadcom's page states that downloading, installing, or using it means
agreeing to its terms, and this project defaults acceptance to true when an
operator runs these workflows. See the
[runtime-image recipe](../doc/ubuntu-24.04-container-dependencies.md) for the
versions and build inputs.

To build or refresh the local image, run `sudo docker compose --env-file
web/.env -f web/compose.yaml build controller`. `MIGRATE_V2V_RUNTIME_IMAGE` can
name a different tag for the image produced by this build. Run the service
with a UID/GID that can read the vault password file and write the artifact
root. The image carries the code, so it can run without mounting a source checkout.
Artifacts, catalogs, vaults, tools, and web state live under
`/work/migrate-v2v` and remain outside the image.

The build context is the repository root. `.dockerignore` leaves out local
credentials, vaults, runtime data, private local scripts, and tests; those stay
on the host or in the mounted artifact directory.

The supplied Compose service runs as the host UID with all capabilities
dropped. Where the web forms take files, see
[Files selected in the forms](README.md#files-selected-in-the-forms).

## Setup

These instructions cover a new x86_64 (amd64) Ubuntu 24.04 host. Install
Docker Engine, the Docker CLI, Buildx, and the Compose plugin from Docker's
signed APT repository. For other supported Ubuntu releases or distributions,
follow the matching [official Docker Engine instructions](https://docs.docker.com/engine/install/).
The examples use `sudo docker`; access to the Docker socket is root-equivalent,
including membership in the `docker` group. This Compose deployment runs Caddy
in a container, so it does not create a host `caddy` account or install Caddy
on the host.

Install Docker's packages:

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc" \
  | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

If installing on a host that already has container software, first check for
the conflicting distribution packages listed in Docker's install guide. Remove
only packages you have confirmed are installed and no longer needed. A fresh
minimal Ubuntu host does not need this cleanup.

The first image build needs several gigabytes for build layers, caches, and the
runtime image. On a clean host with a small root filesystem and a larger
`/work`, configure both Docker and containerd storage roots before building:

```sh
sudo systemctl stop docker.socket docker.service containerd.service
sudo install -d -m 0755 /etc/docker /etc/containerd /work/docker /work/containerd
printf '%s\n' '{"data-root":"/work/docker"}' | sudo tee /etc/docker/daemon.json
sudo containerd config default \
  | sed "s#root = '/var/lib/containerd'#root = '/work/containerd'#" \
  | sudo tee /etc/containerd/config.toml >/dev/null
sudo systemctl start containerd docker
```

Verify the engine, Compose plugin, and storage location:

```sh
sudo docker run --rm hello-world
sudo docker compose version
sudo docker info --format '{{.DockerRootDir}}'
sudo grep '^root =' /etc/containerd/config.toml
```

This is for a new Docker installation before it has image or container data.
It puts Docker data under `/work/docker` and containerd image and snapshot data
under `/work/containerd`; the last command verifies containerd's configured
root. Ensure `/work` has several gigabytes free. If Docker has already been
used, stop it and copy its existing data before changing either configured
root.

1. Prepare the persistent artifact directory and Compose environment. Use the
   account that will own the web state and artifacts; the container runs with
   that account's numeric UID and GID. The vault directory must already contain
   the encrypted vaults the controller will use. The vault password file must
   be readable by that account.

   ```sh
   sudo install -d -o "$USER" -g "$(id -gn)" -m 0750 /work/migrate-v2v
   mkdir -p /work/migrate-v2v/vaults
   cp web/.env.example web/.env
   chmod 600 web/.env
   id -u
   id -g
   ```

   Edit `web/.env`: set `HOST_UID` and `HOST_GID` to the numbers printed above,
   set `ARTIFACT_ROOT_HOST` to `/work/migrate-v2v`, and set
   `VAULT_PASSWORD_FILE_HOST` to the absolute path of the vault password file.
   For access from another computer on the LAN, also set `WEB_ADDRESS` to this
   host's LAN IP (for example, `192.168.1.119`); the LAN Compose override below
   publishes HTTPS on that address. Otherwise leave it as `127.0.0.1` for
   local-only access.

2. Build the runtime image:

   ```sh
   sudo docker compose --env-file web/.env -f web/compose.yaml build controller
   ```

   For standard OVF/OVA exports, the image does not include OVF Tool; the
   wrapper downloads the pinned version on demand. `OVFTOOL_ACCEPT_LICENSE`
   defaults to `1` because Broadcom's page treats downloading, installing, or
   using the tool as agreement to its terms.

3. Create the initial web account. You are prompted for a password of 12 to
   16384 characters:

   ```sh
   sudo docker compose --env-file web/.env -f web/compose.yaml run --rm --no-deps controller python3 web/app.py user add operator
   ```

   The account is stored in the persistent web state directory and is separate
   from the Linux account used to run Docker. To add another account or
   reset a password, run the same command with a different `NAME`; use `user delete
NAME` to remove one and `user list` to show the account names.

   The account file is `${ARTIFACT_ROOT_HOST}/web-state/users.json`; it contains
   password hashes, so a password cannot be read back from it. Run the same
   `user add` command again to set a new password. The vault password file set
   by `VAULT_PASSWORD_FILE_HOST` decrypts Ansible vaults and is not used for
   website sign-in. Caddy has no separate website password file.

4. Start the site with Caddy. For local-only access, run:

   ```sh
   sudo docker compose --env-file web/.env -f web/compose.yaml up -d
   ```

   For access from another computer on the LAN, include the LAN override:

   ```sh
   sudo docker compose --env-file web/.env \
     -f web/compose.yaml -f web/compose.lan.yaml up -d
   ```

5. Open `https://localhost:8443` for local-only access, or
   `https://WEB_ADDRESS:8443` from a LAN client. The `https` service is a Caddy
   container using the repository's [Caddyfile](Caddyfile) and a persistent,
   per-install local certificate authority. Browsers will need to trust its
   root certificate to remove the warning; the certificate is available at
   `/data/caddy/pki/authorities/local/root.crt` inside the `https` container.
   To copy the public root certificate to the current directory, run:

   ```sh
   sudo docker compose --env-file web/.env \
     -f web/compose.yaml exec -T https \
     cat /data/caddy/pki/authorities/local/root.crt > caddy-root.crt
   ```

   Import `caddy-root.crt` into the trust store of each client that will open
   the site. Keep the Caddy data volume, which also contains its private CA
   key, on the server. For LAN access, allow TCP port 8443 through any host or
   network firewall.
   See [Caddy in the Compose stack](caddy.md#caddy-in-the-compose-stack).

Check service state and logs with:

```sh
sudo docker compose --env-file web/.env -f web/compose.yaml ps
sudo docker compose --env-file web/.env -f web/compose.yaml logs --tail=100 controller https
```

Use the same Compose file options for `logs`, `exec`, and `down` that you used
for `up`. `down` stops and removes the containers and private network, but
keeps the artifact bind mount and Caddy's named certificate volumes. Do not add
`--volumes` unless you intend to delete the Caddy CA and certificates.

Migration and launch wrappers can also run directly in the same image, for
example:

```sh
sudo docker compose --env-file web/.env -f web/compose.yaml run --rm controller \
  ./bin/launch-openstack-instance.sh ...
```

## Optional Compose overrides

**KVM acceleration.** On a Linux host with `/dev/kvm`, guest conversion can use
hardware acceleration. Set `KVM_GID` in `web/.env` to the device's group ID
(`stat -c '%g' /dev/kvm`), then add `-f web/compose.kvm.yaml` to the Compose
commands. Without that device, libguestfs uses software emulation, which is
slower.

**LAN address.** By default Compose publishes HTTPS on `127.0.0.1` only. To also
serve this controller's own LAN IP, set `WEB_ADDRESS` in `web/.env` to that IP
and start with both files:

```sh
sudo docker compose --env-file web/.env -f web/compose.yaml -f web/compose.lan.yaml up -d
```

Then open `https://CONTROLLER_IP:8443`. The configured IP must belong to this
controller. The controller HTTP process has no published port; the Caddy
container is the sole entry point. How Caddy uses the address for TLS is
described in [caddy.md](caddy.md#addresses-and-tls-names).

## Running Caddy outside the container

The `https` service is optional. To front the containerized controller with a
Caddy you run yourself, you must publish the controller's port 8080 to a
host-local address (the supplied file does not), and point your Caddy's
`CADDY_UPSTREAM` at it. This is a customization of `compose.yaml`, not a
shipped option; if you want Caddy on the host, running the app on the host as
well is usually simpler. See [caddy.md](caddy.md).
