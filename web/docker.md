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
demand after the operator accepts its license. See the
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

Install Docker Engine, Docker CLI, Buildx, and the Compose plugin if they are
missing. On Ubuntu, use [Docker's official Engine installation guide](https://docs.docker.com/engine/install/ubuntu/).
The examples below use `sudo docker`; access to the Docker socket is
root-equivalent, including membership in the `docker` group.

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

This is for a new Docker installation before it has image or container data.
If Docker has already been used, copy its existing containerd data before
changing the configured root.

1. Copy `web/.env.example` to `web/.env`. Set `HOST_UID` and `HOST_GID` to the
   numeric results of `id -u` and `id -g`, set `ARTIFACT_ROOT_HOST`, and set
   `VAULT_PASSWORD_FILE_HOST` to the absolute host path to the vault password
   file. Keep `.env` private (`chmod 600 web/.env`).

2. Build the runtime image:

   ```sh
   sudo docker compose --env-file web/.env -f web/compose.yaml build controller
   ```

   For standard OVF/OVA exports, first review and accept Broadcom's OVF Tool
   license, then set `OVFTOOL_ACCEPT_LICENSE=1` in `web/.env`. The image does
   not include OVF Tool; the wrapper downloads the pinned version on demand.

3. Create a local account:

   ```sh
   sudo docker compose --env-file web/.env -f web/compose.yaml run --rm controller python3 web/app.py user add operator
   ```

   Run the same command again to add or reset an account; use `user delete NAME`
   to remove one and `user list` to show them.

4. Start the site:

   ```sh
   sudo docker compose --env-file web/.env -f web/compose.yaml up -d
   ```

5. Open `https://localhost:8443`. The Compose file's `https` service is a
   Caddy container using the repository's [Caddyfile](Caddyfile) with a
   per-install local certificate authority; trust that root on each client
   that will open the site. See [Caddy in the Compose stack](caddy.md#caddy-in-the-compose-stack).

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
