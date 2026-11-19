# HTTPS with Caddy

The controller web app (`web/app.py serve`) speaks plain HTTP, by default on
`127.0.0.1:8080`, and marks its login cookie `Secure`. A plain `http://` site
therefore cannot sign anyone in: browsers must reach it over HTTPS. The
repository uses [Caddy](https://caddyserver.com/) as the TLS-terminating
reverse proxy in front of it, configured by [Caddyfile](Caddyfile).

Caddy and the web app are independent choices. Run the app in a container or on
the Ansible control host (see [docker.md](docker.md) and
[Running the controller](README.md#running-the-controller)), and run Caddy
wherever suits you:

| Caddy runs                                | Typical use                    | Section                                                   |
| ----------------------------------------- | ------------------------------ | --------------------------------------------------------- |
| In the Compose stack                      | The supplied Docker deployment | [Caddy in the Compose stack](#caddy-in-the-compose-stack) |
| On the control host, in the foreground    | Testing                        | [Caddy on the control host](#caddy-on-the-control-host)   |
| On the control host, as a systemd service | Long-lived host install        | [Caddy on the control host](#caddy-on-the-control-host)   |

Any other reverse proxy that terminates HTTPS and forwards to the app works
too; the Caddyfile is just a short reference configuration.

## The Caddyfile

[web/Caddyfile](Caddyfile) is configured by three environment variables, each
with a default for local use:

| Variable            | Default                                      | Meaning                                                         |
| ------------------- | -------------------------------------------- | --------------------------------------------------------------- |
| `CADDY_HOSTS`       | `localhost:8443, 127.0.0.1:8443, [::1]:8443` | Site addresses to serve, each with port 8443                    |
| `CADDY_DEFAULT_SNI` | `localhost`                                  | Certificate name for clients that connect by IP and send no SNI |
| `CADDY_UPSTREAM`    | `127.0.0.1:8080`                             | Where the web app listens                                       |

It uses `tls internal`, so Caddy issues certificates from its own local
certificate authority; it does not contact a public CA. Its hard timeouts avoid
newer idle-timeout options so it also runs on older Caddy binaries (it is
written to work with Caddy 2.6; the Compose stack pins 2.8.4).

### Addresses and TLS names

`CADDY_HOSTS` is the list of names and IPs clients will use. Caddy issues a
local certificate for each, so every address users open must be listed. Clients
that open an IP-literal URL (`https://192.0.2.10:8443`) send no SNI, so Caddy
presents the certificate named by `CADDY_DEFAULT_SNI`; set it to the IP those
clients use. Caddy still checks that the HTTP `Host` matches. If the host's
addresses change, restart Caddy so it can issue certificates for the new ones.

### Trusting the local CA

Browsers will warn until the client trusts Caddy's local root certificate.
Copy `root.crt` from Caddy's data directory (see the section for your
deployment) and trust it on each client that will open the site. Keep the CA's
private key private.

## Caddy in the Compose stack

[compose.yaml](compose.yaml) runs Caddy 2.8.4 as the `https` service, upstream
`controller:8080` on a private Compose network. The controller has no published
port, so Caddy is the only entry point. By default it publishes
`127.0.0.1:8443`; [compose.lan.yaml](compose.lan.yaml) adds the address in
`WEB_ADDRESS` (see [docker.md](docker.md#optional-compose-overrides)).

The Caddyfile is mounted read-only. `CADDY_HOSTS`, `CADDY_DEFAULT_SNI` and
`CADDY_UPSTREAM` are set in the Compose file (LAN override included), so edit
those rather than the Caddyfile. Certificates and the local CA live in the
persistent `caddy-data` volume; the root certificate is at
`/data/caddy/pki/authorities/local/root.crt` inside it.

## Caddy on the control host

Install Caddy first, following <https://caddyserver.com/docs/install>; it is not
bundled in the repository. The web app must also run on the host (or its port
must otherwise be reachable at `CADDY_UPSTREAM`).

Caddy's local CA and certificates are kept under the running user's home, by
default `~/.local/share/caddy/pki/authorities/local/root.crt`, or under the
service account's home when run by systemd.

### For testing: `run-caddy-example.sh`

[examples/run-caddy-example.sh](../examples/run-caddy-example.sh) starts Caddy and
the web app together in the foreground. Copy it to `local/` first; `-n` keeps an
existing script:

```sh
cp -n examples/run-caddy-example.sh local/run-caddy.sh
./local/run-caddy.sh
```

Modify `local/run-caddy.sh` as required. It binds Caddy on all IPv4 and IPv6
interfaces and sets `CADDY_HOSTS` to every IP assigned to the host when it
starts, and `CADDY_DEFAULT_SNI` to the first IPv4 address. Open
`https://localhost:8443` or `https://<controller-ip>:8443`.

### As a service: systemd units

[examples/README.md](../examples/README.md#caddy-on-the-ansible-control-host)
describes the example units (`caddy.service.example`, `caddy-api.service.example`
and `migrate-v2v-web.service.example`), including how to set `PROJECT_HOME`,
`CADDY_HOSTS` and `CADDY_DEFAULT_SNI` in the unit.

### No Caddy (development only)

For temporary local development without HTTPS, set `WEB_SECURE_COOKIE=false`,
start `python3 web/app.py serve --host 127.0.0.1 --port 8080`, and open
`http://127.0.0.1:8080`. Do not do this on a shared or reachable host.
