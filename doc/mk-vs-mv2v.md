# Migratekit vs. migrate-v2v

Compared the repositories' `main` branches on 2026-11-07. This comparison
summarizes documented scope and behavior; it is not a performance benchmark.

- [Migratekit][mk]
- [migrate-v2v][mv]

## Primary goal

- **Migratekit:** Near-zero-downtime VMware-to-OpenStack migration, using
  online sync cycles followed by a cutover. ([README: how it works][mk-how])
- **migrate-v2v:** Migrate a vSphere VM to AWS, Azure, OpenStack, or GCP. The
  standard workflow exports disk artifacts; OpenStack and Azure also have
  datastore-direct streaming workflows. ([README][mv-readme])

## Destination support

- **Migratekit:** OpenStack only. ([README][mk])
- **migrate-v2v:** AWS, Azure, OpenStack, and GCP, with provider-specific
  migration and launch playbooks. ([README: flow and project
  structure][mv-flow])

## Migration and cutover

- **Migratekit:** `migrate` performs an initial full copy and later
  incremental cycles while the VMware VM stays online. `cutover` powers off
  the source, performs a final sync, then creates the OpenStack VM.
  ([README: how it works][mk-how])
- **migrate-v2v:** Workflows take a snapshot and copy the full disk set; they
  do not power off the source VM. Launching the destination VM is a separate
  operation after migration. ([README][mv-readme],
  [datastore-direct flow][mv-dd-how])

## Incremental data sync

- **Migratekit:** Subsequent migration cycles copy changes since the previous
  cycle; the documented cutover performs one final sync. ([README: how it
  works][mk-how])
- **migrate-v2v:** Incremental migration is not implemented. CBT can be
  enabled or required, but the datastore-direct guide describes each run as a
  full snapshot copy. ([datastore-direct: CBT][mv-dd-cbt])

## OpenStack result

- **Migratekit:** Cutover builds a server using the migrated volumes.
  ([README: cutover][mk-how])
- **migrate-v2v:** The migration imports disk images into Glance and records
  them in a migration catalog; a separate launch workflow creates the server.
  ([datastore-direct flow][mv-dd-how], [README: OpenStack
  launch][mv-os-launch])

## Implementation and interface

- **Migratekit:** Go CLI with `migrate` and `cutover` operations.
  ([Go module][mk-gomod], [README][mk-usage])
- **migrate-v2v:** Ansible playbooks with shell wrappers and Python helpers;
  wrappers accept environment variables and Ansible extra variables.
  ([README][mv-readme], [project tree][mv-tree])

## Container setup

- **Migratekit:** Provides a Docker image with its other dependencies. The
  documented run uses `--privileged`, host networking, `/dev`, and a
  read-only mount for the separately installed VDDK kit. ([README:
  usage][mk-usage], [Dockerfile][mk-docker])
- **migrate-v2v:** The README describes running on the Ansible control host,
  and the repository root currently has no Dockerfile. It documents
  `/work/migrate-v2v` as the shared bind mount location for artifacts,
  catalogs, and vaults if containerized. ([README][mv-readme], [repository
  root][mv-tree])

## VMware disk library and other external tools

- **Migratekit:** Requires VMware VDDK 8.0.2, downloaded separately from
  Broadcom and accepted under its EULA. ([README: installing VDDK][mk-vddk])
- **migrate-v2v:** The standard export path requires OVF Tool 5.1.0, which is
  not redistributed. Datastore-direct supports OpenVDDK 8.0.3, an independent
  read-only replacement; a properly licensed VMware VDDK can also be used.
  ([README: prerequisites][mv-prereq], [datastore-direct: installing
  VDDK][mv-dd-vddk])

## Project license

- **Migratekit:** Apache-2.0. ([LICENSE][mk-license])
- **migrate-v2v:** MIT. ([LICENSE][mv-license])

[mk]: https://github.com/vexxhost/migratekit
[mk-how]: https://github.com/vexxhost/migratekit#how-it-works
[mk-usage]: https://github.com/vexxhost/migratekit#usage
[mk-vddk]: https://github.com/vexxhost/migratekit#installing-vddk
[mk-gomod]: https://github.com/vexxhost/migratekit/blob/main/go.mod
[mk-docker]: https://github.com/vexxhost/migratekit/blob/main/Dockerfile
[mk-license]: https://github.com/vexxhost/migratekit/blob/main/LICENSE
[mv]: https://github.com/jjbailey/migrate-v2v
[mv-readme]: https://github.com/jjbailey/migrate-v2v/blob/main/README.md
[mv-flow]: https://github.com/jjbailey/migrate-v2v/blob/main/README.md#flow
[mv-os-launch]: https://github.com/jjbailey/migrate-v2v/blob/main/README.md#openstack-launch-networking
[mv-prereq]: https://github.com/jjbailey/migrate-v2v/blob/main/README.md#prerequisites
[mv-tree]: https://github.com/jjbailey/migrate-v2v/tree/main
[mv-dd-how]: https://github.com/jjbailey/migrate-v2v/blob/main/doc/datastore-direct.md#how-it-works
[mv-dd-cbt]: https://github.com/jjbailey/migrate-v2v/blob/main/doc/datastore-direct.md#cbt-changed-block-tracking
[mv-dd-vddk]: https://github.com/jjbailey/migrate-v2v/blob/main/doc/datastore-direct.md#installing-the-vddk-backend
[mv-license]: https://github.com/jjbailey/migrate-v2v/blob/main/LICENSE
