#!/bin/bash
# container-entrypoint.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

set -eu

baked_appliance_path=/tmp/libguestfs-appliance
if [ "${LIBGUESTFS_PATH:-$baked_appliance_path}" = "$baked_appliance_path" ] \
    && [ ! -r "$baked_appliance_path/README.fixed" ]; then
    mkdir -p "$baked_appliance_path"
    tar --extract --xz --file /opt/libguestfs-appliance.tar.xz \
        --directory "$baked_appliance_path"
fi

exec "$@"
