#!/usr/bin/env python3
# bin/check-artifact-space.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Estimate local migration artifact space from govc vm.info JSON."""

import argparse
import json
import os
import sys
from pathlib import Path

def disk_capacity(device):
    value = device.get("capacityInBytes")
    if value is None and device.get("capacityInKB") is not None:
        value = int(device["capacityInKB"]) * 1024
    if value is None or int(value) <= 0:
        return None
    return int(value)

def existing_path(path):
    current = Path(path).expanduser().resolve()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current

def report(info, artifact_root):
    requested_path = Path(artifact_root).expanduser().resolve()
    anchor = existing_path(artifact_root)
    stats = os.statvfs(anchor)
    base = {
        "artifact_root": str(Path(artifact_root).expanduser()),
        "space_checked_on": str(anchor),
        "available_bytes": stats.f_bavail * stats.f_frsize,
        "artifact_root_writable": os.access(anchor, os.W_OK | os.X_OK),
        "artifact_root_is_directory": not requested_path.exists() or requested_path.is_dir(),
    }
    machines = info.get("virtualMachines") or []
    if len(machines) != 1:
        return {**base, "known": False,
                "reason": f"expected one VM record, found {len(machines)}"}
    devices = (((machines[0].get("config") or {}).get("hardware") or {}).get("device") or [])
    disks = [device for device in devices if isinstance(device, dict) and (
        device.get("_typeName") == "VirtualDisk"
        or ("capacityInBytes" in device or "capacityInKB" in device)
        and isinstance(device.get("backing"), dict)
    )]
    capacities = [disk_capacity(disk) for disk in disks]
    if not capacities or any(value is None for value in capacities):
        return {**base, "known": False,
                "reason": "one or more source disk capacities are unavailable"}
    provisioned = sum(capacities)
    # Reserve 20% for the OVF, metadata, and export overhead. This intentionally
    # errs high for thin disks; it is guidance, not a hard admission check.
    estimate = provisioned + provisioned // 5
    return {
        **base,
        "known": True,
        "disk_count": len(capacities),
        "provisioned_bytes": provisioned,
        "estimated_bytes": estimate,
        "estimate_exceeds_available": estimate > base["available_bytes"],
    }

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True)
    args = parser.parse_args(argv)
    try:
        info = json.load(sys.stdin)
        if not isinstance(info, dict):
            raise ValueError("govc vm.info must be a JSON object")
        print(json.dumps(report(info, args.artifact_root)))
    except (OSError, ValueError, TypeError) as error:
        raise SystemExit(f"check-artifact-space: {error}") from error

if __name__ == "__main__":
    main()
