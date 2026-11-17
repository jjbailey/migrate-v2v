#!/usr/bin/env python3
# web/facts.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Project-scoped launch choices from the same facts tools used by dialogs."""

import json
import os
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
_CACHE = OrderedDict()
_LOCK = threading.Lock()
_TTL = 60
# A failed lookup is remembered only briefly: long enough that one page's
# parallel lookups don't all retry a dead endpoint, short enough that
# "Update choices" works again soon after a transient cloud or credential
# error.
_NEGATIVE_TTL = 5
_CACHE_LIMIT = 256

def _cache_store(key, value, now):
    _CACHE[key] = (now, value)
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_LIMIT:
        _CACHE.popitem(last=False)

def query(cloud, project, *flags):
    """Run one fixed facts command. Return None on failure, never credential data."""
    key = (cloud, project, flags)
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and now - cached[0] < (_TTL if cached[1] is not None else _NEGATIVE_TTL):
            _CACHE.move_to_end(key)
            return cached[1]
        if cached:
            del _CACHE[key]
    if not project or str(project).startswith("-"):
        return None
    protected_options = {"--subnet", "--zone", "--network", "--region"}
    if any(str(flags[index + 1]).startswith("-")
           for index, flag in enumerate(flags[:-1])
           if flag in protected_options):
        return None
    environment = {name: os.environ[name] for name in (
        "PATH", "HOME", "LANG", "LC_ALL", "DIB7_VENV_BIN", "MIGRATE_V2V_VAULT_DIR",
        "VAULT_PASSWORD_FILE", "ANSIBLE_LOCAL_TEMP", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
        "https_proxy", "http_proxy", "no_proxy") if name in os.environ}
    command = [sys.executable, str(BIN / f"{cloud}-project-facts.py"), project, *flags, "--json"]
    try:
        result = subprocess.run(command, cwd=BIN.parent, env=environment, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=60, check=True)
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            with _LOCK:
                _cache_store(key, None, now)
            return None
    except (OSError, subprocess.SubprocessError, ValueError):
        with _LOCK:
            _cache_store(key, None, now)
        return None
    with _LOCK:
        _cache_store(key, data, now)
    return data

def _rows(data, section, value="name", detail=()):
    rows = []
    for item in (data or {}).get(section) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get(value) or "").strip()
        if not name:
            continue
        extra = ", ".join(str(item.get(field)) for field in detail if item.get(field))
        rows.append((name, f"{name} · {extra}" if extra else name))
    return rows

def choices(cloud, project, current):
    """Return (field options, field defaults, sections that could not load)."""
    options, defaults, unavailable = {}, {}, []
    if cloud == "aws":
        with ThreadPoolExecutor(max_workers=2) as pool:
            subnets_future = pool.submit(query, cloud, project, "--subnets")
            keys_future = pool.submit(query, cloud, project, "--keypairs")
            subnets = subnets_future.result()
            keys = keys_future.result()
        available = {str(row.get("id")) for row in (subnets or {}).get("subnets") or []
                     if row.get("state", "available") in ("", "available")}
        options["AWS_SUBNET_ID"] = [(sid, label) for sid, label in
                                    _rows(subnets, "subnets", "id", ("name", "cidr", "az", "vpc_id"))
                                    if sid in available]
        options["AWS_KEY_NAME"] = _rows(keys, "keypairs", detail=("type", "fingerprint"))
        options["AWS_INSTANCE_TYPE"] = [(name, name) for name in (
            "t3.micro", "t3.small", "t3.medium", "t3.large", "t3.xlarge", "t3a.medium",
            "t3a.large", "m5.large", "m5.xlarge", "m5.2xlarge", "m6i.large", "m6i.xlarge",
            "c5.large", "c5.xlarge", "c6i.large", "r5.large", "r5.xlarge")]
        subnet = current.get("AWS_SUBNET_ID") or (options["AWS_SUBNET_ID"][0][0] if options["AWS_SUBNET_ID"] else "")
        if subnet:
            defaults["AWS_SUBNET_ID"] = subnet
            groups = query(cloud, project, "--subnet", subnet)
            options["AWS_SECURITY_GROUPS_JSON"] = _rows(groups, "security_groups", "id", ("name", "rules", "vpc_id"))
            if groups is None:
                unavailable.append("security groups")
        if subnets is None:
            unavailable.append("subnets")
        if keys is None:
            unavailable.append("key pairs")
    elif cloud == "azure":
        keys = query(cloud, project, "--keypairs")
        options["AZURE_SSH_KEY_NAME"] = _rows(keys, "keypairs", detail=("type", "fingerprint"))
        if keys is None:
            unavailable.append("SSH keys")
    elif cloud == "gcp":
        with ThreadPoolExecutor(max_workers=2) as pool:
            zones_future = pool.submit(query, cloud, project, "--zones")
            networks_future = pool.submit(query, cloud, project, "--networks")
            zones = zones_future.result()
            networks = networks_future.result()
        options["GCP_ZONE"] = _rows(zones, "zones", detail=("region",))
        options["GCP_NETWORK"] = _rows(networks, "networks", detail=("mode", "routing"))
        known_zones = {name for name, _ in options["GCP_ZONE"]}
        known_networks = {name for name, _ in options["GCP_NETWORK"]}
        zone = current.get("GCP_ZONE") or ("us-central1-a" if "us-central1-a" in known_zones
                                          else options["GCP_ZONE"][0][0] if known_zones else "us-central1-a")
        network = current.get("GCP_NETWORK") or ("default" if "default" in known_networks
                                                else options["GCP_NETWORK"][0][0] if known_networks else "default")
        defaults.update(GCP_ZONE=zone, GCP_NETWORK=network)
        with ThreadPoolExecutor(max_workers=2) as pool:
            types_future = pool.submit(query, cloud, project, "--machine-types", "--zone", zone)
            subnets_future = pool.submit(query, cloud, project, "--subnetworks", "--zone", zone, "--network", network)
            types = types_future.result()
            subnets = subnets_future.result()
        options["GCP_MACHINE_TYPE"] = _rows(types, "machine_types", detail=("vcpus", "memory_mb"))
        options["GCP_SUBNETWORK"] = _rows(subnets, "subnetworks", detail=("cidr", "network"))
        if options["GCP_SUBNETWORK"] and "GCP_SUBNETWORK" not in current:
            defaults["GCP_SUBNETWORK"] = options["GCP_SUBNETWORK"][0][0]
        for title, data in (("zones", zones), ("networks", networks), ("machine types", types), ("subnetworks", subnets)):
            if data is None:
                unavailable.append(title)
    else:
        data = query(cloud, project, "--flavors", "--security-groups", "--networks", "--keypairs")
        options["OS_FLAVOR"] = _rows(data, "flavors", detail=("vcpus", "ram_mb", "disk_gb", "fits"))
        options["OS_NETWORK"] = _rows(data, "networks", detail=("access", "status", "subnets"))
        options["OS_KEY_NAME"] = _rows(data, "keypairs", detail=("type", "fingerprint"))
        options["OS_SECURITY_GROUPS_JSON"] = _rows(data, "security_groups", detail=("access", "rules"))
        options["OS_SECURITY_GROUP_IDS_JSON"] = _rows(data, "security_groups", "id", ("name", "access"))
        if data is None:
            unavailable.append("flavors, networks, key pairs and security groups")
    return options, defaults, unavailable
