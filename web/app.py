#!/usr/bin/env python3
# web/app.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Small, dependency-free controller UI for the existing bin/ wrappers."""

import argparse
import base64
import getpass
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    from .forms import CLOUDS, F, exported_fields, wrapper_path
    from .facts import choices as launch_choices
except ImportError:  # direct python3 web/app.py invocation
    from forms import CLOUDS, F, exported_fields, wrapper_path
    from facts import choices as launch_choices

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
from catalog_common import read_snapshot, timestamp_sort_key
from vault_common import ansible_vault_bin  # noqa: E402

STATE = Path(os.environ.get("WEB_STATE_DIR", "/work/migrate-v2v/web-state"))
ARTIFACT_ROOT = Path(os.environ.get("ARTIFACT_ROOT", "/work/migrate-v2v"))
INPUT_DIR = Path(os.environ.get("WEB_INPUT_DIR", str(ARTIFACT_ROOT / "web-inputs")))
VAULT_DIR = Path(os.environ.get("MIGRATE_V2V_VAULT_DIR", str(ARTIFACT_ROOT / "vaults")))
CATALOG_DIR = Path(os.environ.get("MIGRATE_V2V_CATALOG_DIR", str(ARTIFACT_ROOT / "catalogs")))
PASSFILE = Path(os.environ.get("VAULT_PASSWORD_FILE", "/run/secrets/migrate-v2v-vault-password"))
USERS = STATE / "users.json"
KEY = STATE / "session.key"
DB = STATE / "jobs.sqlite3"
MAX_BODY = 64 * 1024
MAX_FIELD = 16 * 1024
MAX_LOGIN_NAME = 32
MAX_LOGIN_PASSWORD = MAX_FIELD
SESSION_AGE = 12 * 60 * 60
TERMINAL_JOB_STATUSES = ("completed", "failed", "interrupted", "cancelled")
LOGIN_FAILURES = {}
ACTIVE_PROCESS = None
ACTIVE_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
LOGIN_LOCK = threading.Lock()
LOGIN_HASH_SLOTS = threading.BoundedSemaphore(4)
VAULT_CACHE = {}
VAULT_CACHE_LOCK = threading.Lock()
VAULT_FILE_LOCKS = {}
VAULT_CACHE_TTL = 15

def esc(value):
    return html.escape(str(value), quote=True)

def secure_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)

def init_state():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    INPUT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    ansible_temp = STATE / "ansible-tmp"
    ansible_temp.mkdir(mode=0o700, exist_ok=True)
    os.environ.setdefault("ANSIBLE_LOCAL_TEMP", str(ansible_temp))
    os.environ.setdefault("VAULT_PASSWORD_FILE", str(PASSFILE))
    if not KEY.exists():
        secure_file(KEY, secrets.token_bytes(32))
    if not USERS.exists():
        secure_file(USERS, b"{}\n")
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL,
            started TEXT, finished TEXT, user TEXT NOT NULL, mode TEXT NOT NULL,
            cloud TEXT NOT NULL, method TEXT NOT NULL, vm TEXT NOT NULL,
            target TEXT NOT NULL, status TEXT NOT NULL, values_json TEXT NOT NULL,
            exit_code INTEGER, log_path TEXT)""")

def db():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)
    return f"scrypt:32768:8:1:{salt.hex()}:{digest.hex()}"

def verify_password(password, encoded):
    try:
        scheme, n, r, p, salt, digest = encoded.split(":")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False

DUMMY_PASSWORD_HASH = password_hash(secrets.token_urlsafe(32))

def checked_input_path(value, allow_missing=False, allow_directory=False):
    """Resolve an input path inside the operator-managed input area."""
    if not value or not Path(value).is_absolute():
        return None
    try:
        candidate = Path(value).resolve(strict=not allow_missing)
        root = INPUT_DIR.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate.exists():
        readable_file = candidate.is_file() and os.access(candidate, os.R_OK)
        readable_directory = (
            allow_directory and candidate.is_dir()
            and os.access(candidate, os.R_OK) and os.access(candidate, os.X_OK)
        )
        if not (readable_file or readable_directory):
            return None
    elif not allow_missing:
        return None
    else:
        try:
            candidate.parent.resolve(strict=True).relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return None
        if not candidate.parent.is_dir():
            return None
    for private_root in (STATE, VAULT_DIR, CATALOG_DIR):
        private_path = private_root.resolve(strict=False)
        try:
            candidate.relative_to(private_path)
            return None
        except ValueError:
            pass
        if candidate.is_dir():
            try:
                private_path.relative_to(candidate)
                return None
            except ValueError:
                pass
    try:
        if candidate == PASSFILE.resolve(strict=False):
            return None
    except (OSError, RuntimeError):
        return None
    return candidate

def users():
    return json.loads(USERS.read_text())

def save_users(entries):
    fd, temp = tempfile.mkstemp(prefix=USERS.name + ".", dir=USERS.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            fd = -1
            json.dump(entries, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, USERS)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(temp):
            os.unlink(temp)

def sign(payload):
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(KEY.read_bytes(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{mac}"

def unsign(token):
    try:
        raw, mac = token.rsplit(".", 1)
        expected = hmac.new(KEY.read_bytes(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if not isinstance(data, dict) or int(data.get("exp", 0)) < time.time():
            return None
        return data
    except (ValueError, TypeError, json.JSONDecodeError):
        return None

def vault_keys(filename, map_name):
    """Return only entry names; decrypted credentials never enter a response."""
    vault = VAULT_DIR / filename
    def signature(path):
        try:
            info = path.stat()
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        except OSError:
            return None

    source_signature = (signature(vault), signature(PASSFILE))
    now = time.monotonic()
    # One lock per vault file, so a slow or hung decrypt of one vault does
    # not stall pages that only need another.
    with VAULT_CACHE_LOCK:
        file_lock = VAULT_FILE_LOCKS.setdefault(filename, threading.Lock())
    with file_lock:
        cached = VAULT_CACHE.get(filename)
        if cached and cached[0] == source_signature and now < cached[1]:
            return list(cached[2])
        names = []
        if vault.is_file() and PASSFILE.is_file():
            try:
                import yaml
            except ImportError:
                yaml = None
            if yaml is None:
                print("web: PyYAML is unavailable; cannot read vault entries", file=sys.stderr)
            else:
                try:
                    command = [ansible_vault_bin(), "view", str(vault),
                               "--vault-password-file", str(PASSFILE)]
                    result = subprocess.run(command, input=b"", capture_output=True,
                                            timeout=30, check=True)
                    document = yaml.safe_load(result.stdout)
                    entries = ((document or {}).get(map_name, {})
                               if isinstance(document, dict) else {})
                    if isinstance(entries, dict):
                        names = sorted(str(name) for name in entries)
                except (OSError, subprocess.SubprocessError, ValueError,
                        AttributeError, yaml.YAMLError, SystemExit) as error:
                    print(f"web: could not read {filename} vault entries: {error}", file=sys.stderr)
                    names = []
        elif vault.is_file():
            print(f"web: vault password file is missing; cannot read {filename} entries", file=sys.stderr)
        VAULT_CACHE[filename] = (source_signature, now + VAULT_CACHE_TTL, names)
        return list(names)

def targets(cloud, mode):
    return vault_keys(f"{cloud}.yml", f"{cloud}_projects")

def catalog_vms(cloud):
    command = [sys.executable, str(ROOT / "bin/list-image-catalog.py"), "--provider", cloud, "--json"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=True)
        return sorted({str(row.get("vm_name")) for row in json.loads(result.stdout) if row.get("vm_name")})
    except (OSError, subprocess.SubprocessError, ValueError):
        return []

def catalog_migrations(cloud, project=None):
    try:
        entries = read_snapshot(CATALOG_DIR / "migration-catalog.json", "migrations").get("migrations", [])
    except (OSError, ValueError):
        return []
    project_fields = {
        "aws": ("aws_project", "aws_account_id"),
        "azure": ("azure_subscription_id",),
        "gcp": ("gcp_project",),
        "openstack": ("openstack_project", "openstack_project_id"),
    }.get(cloud, ())
    selected = []
    project_selected = []
    for entry in entries:
        if (not isinstance(entry, dict) or entry.get("target_platform") != cloud
                or entry.get("status") != "completed" or not entry.get("vm_name")
                or not entry.get("clone_name")):
            continue
        selected.append(entry)
        if project and project in {str(entry.get(field) or "") for field in project_fields}:
            project_selected.append(entry)
    if project_selected:
        selected = project_selected
    return sorted(selected,
                  key=lambda entry: timestamp_sort_key(entry.get("migrated_at")),
                  reverse=True)

def launch_nic_macs(cloud, project, migration_id):
    """MACs of the source VM's NICs, in device order, for a chosen migration run.

    Empty when the count is unknown (no run chosen, or a catalog row written
    before MAC metadata existed), which keeps the single-NIC form.
    """
    if cloud != "openstack" or not migration_id:
        return []
    for entry in catalog_migrations(cloud, project):
        if str(entry.get("clone_name")) == migration_id:
            manifest = entry.get("artifact_manifest")
            macs = manifest.get("source_mac_addresses") if isinstance(manifest, dict) else None
            if not isinstance(macs, list):
                return []
            return [mac.strip() for mac in macs if isinstance(mac, str) and mac.strip()]
    return []

def catalog_migration_label(cloud, entry):
    project_fields = {
        "aws": ("aws_project", "aws_account_id"),
        "azure": ("azure_subscription_id",),
        "gcp": ("gcp_project",),
        "openstack": ("openstack_project", "openstack_project_id"),
    }.get(cloud, ())
    project = next((str(entry.get(field)) for field in project_fields if entry.get(field)), "project unknown")
    region_field = {"aws": "aws_region", "azure": "azure_location",
                    "gcp": "gcp_import_location", "openstack": "openstack_region"}.get(cloud)
    region = str(entry.get(region_field) or "region unknown")
    source = str(entry.get("source_vcenter_key") or "source vCenter unknown")
    date = str(entry.get("migrated_at") or "undated")[:10]
    return " · ".join((str(entry.get("vm_name", "")), source,
                       f"{project} / {region}", date,
                       str(entry.get("clone_name", ""))))

def target_key(cloud):
    return {"aws": "AWS_TARGET_PROJECT", "azure": "AZURE_TARGET_PROJECT", "gcp": "GCP_TARGET_PROJECT", "openstack": "OS_PROJECT_NAME"}[cloud]

def cloud_name(cloud):
    return {"aws": "AWS", "azure": "Azure", "gcp": "Google Cloud", "openstack": "OpenStack"}[cloud]

_ICON_FILE = {"aws": "aws.svg", "azure": "azure.svg", "gcp": "google-cloud.svg", "openstack": "openstack.svg"}
_ICON_CACHE = {}

def icon_html(cloud):
    """Return the inlined SVG icon for a cloud, scaled to 60x60."""
    if cloud not in _ICON_CACHE:
        svg = (ROOT / "web" / "icons" / _ICON_FILE[cloud]).read_text()
        svg = svg.replace("<svg ", '<svg width="60" height="60" ', 1)
        # Each icon is inlined on both the migrate and launch cards, so ids
        # from the source files would repeat in the page. Keep them when the
        # icon refers to its own ids (gradients, <use>), which need them.
        if "url(#" not in svg and 'href="#' not in svg:
            svg = re.sub(r'\s+id="[^"]*"', "", svg)
        _ICON_CACHE[cloud] = svg
    return _ICON_CACHE[cloud]

def validate(mode, cloud, method, posted):
    fields = exported_fields(mode, cloud, method)
    values = {}
    errors = []
    allowed_targets = targets(cloud, mode)
    if not allowed_targets:
        errors.append(f"No {cloud_name(cloud)} entries could be read from the vault.")
    # A source VM with several NICs is launched with one NIC per source NIC
    # unless the operator chooses a single NIC ; the NIC list then replaces
    # the single-network fields.
    nic_macs = launch_nic_macs(cloud, posted.get(target_key(cloud), "").strip(),
                               posted.get("MIGRATION_ID", "").strip()) if mode == "launch" else []
    multi_nic = len(nic_macs) > 1 and posted.get("_nic_multi", "yes") == "yes"
    for name in fields:
        meta = F[name]
        value = posted.get(name, "").strip()
        if name == "VIRTIO_WIN" and posted.get("GUEST_CONVERT") != "true":
            value = ""
        picks = posted.get(f"_pick_{name}", [])
        if meta.kind == "json" and picks and value in ("", "[]"):
            value = json.dumps(picks)
        if len(value) > MAX_FIELD:
            errors.append(f"{meta.label} is too long.")
            continue
        if meta.kind == "boolean" and value not in ("true", "false"):
            errors.append(f"Choose a value for {meta.label}.")
        if meta.kind == "choice" and value not in meta.choices:
            errors.append(f"Choose a valid {meta.label}.")
        if meta.required and not value and not (multi_nic and name == "OS_NETWORK"):
            errors.append(f"{meta.label} is required.")
        if meta.kind == "json" and value:
            try:
                items = json.loads(value)
                allowed_types = (str, dict) if name == "OS_NICS_JSON" else (str,)
                if not isinstance(items, list) or any(not isinstance(item, allowed_types) for item in items):
                    raise ValueError()
            except ValueError:
                errors.append(f"{meta.label} must be a JSON array.")
        if meta.kind == "path" and value:
            allow_missing_virtio_default = (
                name == "VIRTIO_WIN"
                and posted.get("GUEST_CONVERT") == "true"
                and Path(value) == INPUT_DIR / "virtio-win.iso"
            )
            resolved = checked_input_path(
                value,
                allow_missing=allow_missing_virtio_default,
                allow_directory=name == "VIRTIO_WIN",
            )
            if resolved is None:
                if name == "VIRTIO_WIN":
                    errors.append(f"{meta.label} must name a readable file or directory under {INPUT_DIR}, or the default virtio-win ISO path.")
                else:
                    errors.append(f"{meta.label} must name a readable file under {INPUT_DIR}.")
            else:
                value = str(resolved)
        values[name] = value
    project = values.get(target_key(cloud), "")
    if project not in allowed_targets:
        errors.append("Select an environment from the vault.")
    if mode == "migrate":
        sources = vault_keys("vsphere.yml", "vsphere_projects")
        source = values.get("VSPHERE_TARGET_VCENTER", "")
        if source and source not in sources:
            errors.append("Select a source vCenter from the vault.")
        if not source and len(sources) != 1 and not values.get("VCENTER_DATACENTER"):
            errors.append("Select a source vCenter.")
        if values.get("DISCARD_RUN_ID") and not re.fullmatch(r"[0-9]{1,20}", values["DISCARD_RUN_ID"]):
            errors.append("Failed run ID must contain 1 to 20 digits.")
        if values.get("DISCARD_RUN_ID") and posted.get("_confirm_discard") != "yes":
            errors.append("Confirm that the failed run should be discarded.")
        if source:
            values["VCENTER_DATACENTER"] = ""
    else:
        migration_id = values.get("MIGRATION_ID", "")
        available_migrations = catalog_migrations(cloud, project)
        selected_migration = next(
            (entry for entry in available_migrations
             if str(entry.get("clone_name")) == migration_id), None)
        if migration_id:
            if selected_migration is None:
                errors.append("Select a completed migration for the chosen project.")
            else:
                values["VM_NAME"] = str(selected_migration["vm_name"])
        elif values.get("VM_NAME") not in catalog_vms(cloud):
            errors.append("Select a VM with a completed migration in the catalog.")
        if project in allowed_targets:
            available, _, _ = launch_choices(cloud, project, values)
            for name in ("AWS_SECURITY_GROUPS_JSON", "OS_SECURITY_GROUPS_JSON", "OS_SECURITY_GROUP_IDS_JSON"):
                picked = posted.get(f"_pick_{name}", [])
                if picked and not set(picked).issubset({item[0] for item in available.get(name, [])}):
                    errors.append(f"Refresh {F[name].label} choices for the selected network.")
        if cloud == "azure" and values.get("AZURE_NO_PUBLIC_IP") == "true":
            values["AZURE_SSH_SOURCE_PREFIX"] = ""
        elif cloud == "azure" and not values.get("AZURE_SSH_SOURCE_PREFIX"):
            errors.append("Allow SSH from is required when assigning a public IP.")
        if cloud == "openstack":
            if posted.get("_nic_multi", "yes") not in ("yes", "no"):
                errors.append("Choose whether to create one NIC per source NIC.")
            if multi_nic:
                nic_networks = [posted.get(f"_nic_network_{index + 1}", "").strip()
                                for index in range(len(nic_macs))]
                if not all(nic_networks) or any(len(net) > MAX_FIELD for net in nic_networks):
                    errors.append("Choose a network for every NIC.")
                elif values.get("OS_NICS_JSON") not in ("", "[]"):
                    errors.append("Clear Custom NICs, or choose a single NIC: a network was already chosen for each source NIC.")
                else:
                    values["OS_NICS_JSON"] = json.dumps([{"net-name": net} for net in nic_networks],
                                                        separators=(",", ":"))
                for key in ("OS_FIXED_IPS_JSON", "OS_MAC_ADDRESSES_JSON"):
                    try:
                        count = len(json.loads(values.get(key) or "[]"))
                    except ValueError:
                        continue
                    if count and count != len(nic_macs):
                        errors.append(f"{F[key].label} must list exactly {len(nic_macs)} entries, one per NIC.")
            if values.get("OS_FIXED_IPS_JSON") not in ("", "[]") and values.get("OS_AUTO_IP") == "true":
                errors.append("Fixed IPs and an automatic floating IP cannot be combined.")
            needs_ports = values.get("OS_FIXED_IPS_JSON") not in ("", "[]") or values.get("OS_PRESERVE_MAC") == "true"
            if needs_ports and values.get("OS_SECURITY_GROUPS_JSON") in ("", "[]") and values.get("OS_SECURITY_GROUP_IDS_JSON") in ("", "[]"):
                errors.append("Fixed IPs and preserved MACs require a security group.")
            if values.get("OS_MAC_ADDRESSES_JSON") not in ("", "[]") and values.get("OS_PRESERVE_MAC") != "true":
                errors.append("Enable preserved MACs when specifying manual MAC addresses.")
            if sum(bool(values.get(key)) for key in ("OS_USER_DATA", "OS_USER_DATA_FILE")) + (values.get("OS_DHCP_USER_DATA") == "true") > 1:
                errors.append("Choose one cloud-init user-data source.")
            for key in ("OS_FIXED_IPS_JSON", "OS_MAC_ADDRESSES_JSON"):
                if values.get(key) not in ("", "[]"):
                    try:
                        items = json.loads(values[key])
                        for item in items:
                            if key == "OS_FIXED_IPS_JSON":
                                ipaddress.ip_address(item)
                            elif not re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", item):
                                raise ValueError()
                    except (ValueError, TypeError):
                        errors.append(f"{F[key].label} contains an invalid address.")
    return values, errors

def enqueue(user, mode, cloud, method, values):
    with db() as conn:
        cur = conn.execute("""INSERT INTO jobs
            (created,user,mode,cloud,method,vm,target,status,values_json)
            VALUES (datetime('now'),?,?,?,?,?,?, 'queued',?)""",
            (user, mode, cloud, method, values["VM_NAME"], values[target_key(cloud)], json.dumps(values)))
        return cur.lastrowid

def cancel_queued_job(job_id):
    """Cancel only a job the worker has not claimed yet."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("That job no longer exists.")
        if row["status"] != "queued":
            raise ValueError("Only a queued job can be cancelled. Running jobs may already have created cloud resources.")
        log_path = STATE / f"job-{job_id}.log"
        conn.execute(
            "UPDATE jobs SET status='cancelled', finished=datetime('now'), log_path=? WHERE id=? AND status='queued'",
            (str(log_path), job_id),
        )
    try:
        with open(log_path, "wb") as stream:
            os.chmod(log_path, 0o600)
            stream.write(b"Cancelled before the worker started this job.\n")
    except OSError:
        pass

def recovery_metadata(row):
    try:
        values = json.loads(row["values_json"])
        return values if isinstance(values, dict) else {}
    except (KeyError, TypeError, ValueError):
        return {}

def recovery_log(row):
    path = STATE / f"job-{int(row['id'])}.log"
    try:
        with open(path, "rb") as stream:
            stream.seek(max(0, stream.seek(0, 2) - 200000))
            return stream.read().decode(errors="replace")
    except (OSError, TypeError, ValueError):
        return ""

def recovery_hinted_run_ids(row):
    log = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", recovery_log(row))
    resume_ids = re.findall(r"-e(?:\s|\\n)+resume_run_id=([0-9]{1,20})\b", log, re.IGNORECASE)
    discard_ids = re.findall(r"-e(?:\s|\\n)+discard_run_id=([0-9]{1,20})\b", log, re.IGNORECASE)
    return {"resume_run_id": resume_ids[-1] if resume_ids else "",
            "discard_run_id": discard_ids[-1] if discard_ids else ""}

def recovery_run_key(row, run_id):
    return (row["cloud"], row["target"], row["vm"], row["method"], run_id)

def resolved_discard_runs(conn):
    """Find runs a web discard later confirmed had no remaining artifacts."""
    status_placeholders = ", ".join("?" for _ in TERMINAL_JOB_STATUSES)
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE mode='migrate' AND status IN ({status_placeholders})",
        TERMINAL_JOB_STATUSES,
    ).fetchall()
    resolved = {}
    for row in rows:
        metadata = recovery_metadata(row)
        run_id = metadata.get("_recovery_run_id", "")
        if metadata.get("_recovery_action") != "discard" or not re.fullmatch(r"[0-9]{1,20}", str(run_id)):
            continue
        log = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", recovery_log(row))
        if not re.search(
                rf"(?:Nothing to discard for run|No resources remain for run)\s+{re.escape(str(run_id))}\b",
                log, re.IGNORECASE):
            continue
        key = recovery_run_key(row, str(run_id))
        resolved.setdefault(key, {
            "run_id": str(run_id),
            "job_id": int(row["id"]),
            "source_job_id": metadata.get("_recovery_source_job_id"),
            "row": row,
        })
    return resolved

def recovery_actions(row, resolved_runs=None):
    """Return run IDs explicitly advertised as resumable or discardable."""
    actions = {"resume_run_id": "", "discard_run_id": ""}
    if row["mode"] != "migrate" or row["status"] not in ("failed", "interrupted"):
        return actions
    if recovery_metadata(row).get("_recovery_completed_by"):
        return actions

    hinted = recovery_hinted_run_ids(row)
    resume_id = hinted["resume_run_id"]
    discard_id = hinted["discard_run_id"]
    if resolved_runs and any(
            run_id and recovery_run_key(row, run_id) in resolved_runs
            for run_id in {resume_id, discard_id}):
        return actions
    if resume_id and discard_id and resume_id != discard_id:
        return actions
    if row["cloud"] in ("azure", "gcp", "openstack") and resume_id:
        actions["resume_run_id"] = resume_id
    if row["method"] == "vddk" and row["cloud"] in ("azure", "openstack") and discard_id:
        actions["discard_run_id"] = discard_id
    return actions

def recovery_group_actions(conn, row):
    """Combine actions reported by failed attempts for the same retained run."""
    resolved_runs = resolved_discard_runs(conn)
    actions = recovery_actions(row, resolved_runs)
    run_id = actions["resume_run_id"] or actions["discard_run_id"]
    if not run_id:
        return actions
    rows = conn.execute(
        """SELECT * FROM jobs WHERE mode='migrate' AND cloud=? AND vm=? AND target=?
           AND method=? AND status IN ('failed','interrupted')""",
        (row["cloud"], row["vm"], row["target"], row["method"]),
    ).fetchall()
    for candidate in rows:
        candidate_actions = recovery_actions(candidate, resolved_runs)
        for name, value in candidate_actions.items():
            if value == run_id:
                actions[name] = value
    return actions

def recovery_jobs():
    """List failed migrations, grouping retries of the same retained run."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE mode='migrate' AND status IN ('failed','interrupted') ORDER BY id DESC"
        ).fetchall()
        resolved_runs = resolved_discard_runs(conn)
        pending_rows = conn.execute(
            "SELECT id,values_json FROM jobs WHERE status IN ('queued','running')"
        ).fetchall()

    pending_by_run = {}
    for pending_row in pending_rows:
        pending_values = recovery_metadata(pending_row)
        run_id = pending_values.get("_recovery_run_id")
        if run_id:
            pending_by_run[run_id] = pending_row["id"]

    grouped = {}
    inactive = []
    rows_by_id = {int(row["id"]): row for row in rows}
    for row in rows:
        metadata = recovery_metadata(row)
        hinted = recovery_hinted_run_ids(row)
        possible_run_ids = {hinted["resume_run_id"], hinted["discard_run_id"],
                            str(metadata.get("_recovery_run_id", ""))}
        resolution = next((resolved_runs[recovery_run_key(row, run_id)]
                           for run_id in possible_run_ids
                           if run_id and recovery_run_key(row, run_id) in resolved_runs), None)
        if resolution:
            continue
        actions = recovery_actions(row, resolved_runs)
        run_id = actions["resume_run_id"] or actions["discard_run_id"]
        if run_id:
            key = (row["cloud"], row["target"], row["vm"], row["method"], run_id)
            if key not in grouped:
                grouped[key] = {"row": row, "actions": actions,
                                "pending_job_id": pending_by_run.get(run_id)}
            else:
                # A retry can report fewer actions than the original failure.
                # Keep the newest job's inputs while retaining every action
                # still advertised for this same run ID.
                current = grouped[key]["actions"]
                for name, value in actions.items():
                    if value == run_id:
                        current[name] = value
        else:
            inactive.append({"row": row, "actions": actions})
    for key, resolution in resolved_runs.items():
        source_id = resolution.get("source_job_id")
        display_row = rows_by_id.get(int(source_id)) if str(source_id or "").isdigit() else None
        if display_row is None:
            display_row = rows_by_id.get(resolution["job_id"], resolution["row"])
        inactive.append({"row": display_row,
                         "actions": {"resume_run_id": "", "discard_run_id": ""},
                         "resolved_run_id": resolution["run_id"],
                         "no_resources_job_id": resolution["job_id"]})

    active = list(grouped.values())
    active.sort(key=lambda item: int(item["row"]["id"]), reverse=True)
    inactive.sort(key=lambda item: int(item["row"]["id"]), reverse=True)
    return active + inactive

def queue_recovery(user, job_id, action):
    if action not in ("resume", "discard"):
        raise ValueError("Unknown recovery action.")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("That failed job no longer exists.")
        actions = recovery_group_actions(conn, row)
        field = "resume_run_id" if action == "resume" else "discard_run_id"
        run_id = actions[field]
        if not run_id:
            raise ValueError("That run is no longer available for this action.")

        # One recovery operation per retained run may be queued or running at
        # a time, even if older failed jobs refer to the same run ID.
        pending = conn.execute(
            "SELECT id,values_json FROM jobs WHERE status IN ('queued','running')"
        ).fetchall()
        for pending_row in pending:
            pending_values = recovery_metadata(pending_row)
            if pending_values.get("_recovery_run_id") == run_id:
                raise ValueError(f"A recovery action for run {run_id} is already queued or running as job #{pending_row['id']}.")

        values = recovery_metadata(row)
        if not values.get("VM_NAME") or not values.get(target_key(row["cloud"])):
            raise ValueError("The original job is missing its migration inputs.")
        values["DISCARD_RUN_ID"] = ""
        values["_recovery_action"] = action
        values["_recovery_run_id"] = run_id
        values["_recovery_source_job_id"] = int(job_id)
        cur = conn.execute("""INSERT INTO jobs
            (created,user,mode,cloud,method,vm,target,status,values_json)
            VALUES (datetime('now'),?,?,?,?,?,?, 'queued',?)""",
            (user, row["mode"], row["cloud"], row["method"], row["vm"], row["target"], json.dumps(values)))
        return cur.lastrowid

def mark_recovery_complete(conn, source_job_id, recovery_job_id):
    """Mark the failed run and any failed retries in its chain as resolved."""
    seen = set()
    current = source_job_id
    while current and current not in seen:
        seen.add(current)
        row = conn.execute("SELECT values_json FROM jobs WHERE id=?", (current,)).fetchone()
        if not row:
            break
        values = recovery_metadata(row)
        parent = values.get("_recovery_source_job_id")
        values["_recovery_completed_by"] = recovery_job_id
        conn.execute("UPDATE jobs SET values_json=? WHERE id=?", (json.dumps(values), current))
        current = parent

def worker():
    global ACTIVE_PROCESS
    while not STOP_EVENT.is_set():
        row = None
        try:
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if row:
                    log_path = STATE / f"job-{row['id']}.log"
                    conn.execute("UPDATE jobs SET status='running', started=datetime('now'), log_path=? WHERE id=?",
                                 (str(log_path), row["id"]))
        except Exception as error:
            sys.stderr.write(f"web worker: could not fetch queued job: {error}\n")
            STOP_EVENT.wait(1)
            continue
        if not row:
            STOP_EVENT.wait(1)
            continue
        log_path = STATE / f"job-{row['id']}.log"
        code = 1
        error_text = None
        stopped = False
        recovery_source_job_id = None
        try:
            with open(log_path, "wb", buffering=0) as log:
                os.chmod(log_path, 0o600)
                log.write(f"Running {row['mode']} {row['cloud']} for {row['vm']}\n".encode())
                try:
                    values = json.loads(row["values_json"])
                    if not isinstance(values, dict):
                        raise ValueError("job values are not a JSON object")
                    recovery_action = values.pop("_recovery_action", "")
                    recovery_run_id = values.pop("_recovery_run_id", "")
                    recovery_source_job_id = values.pop("_recovery_source_job_id", None)
                    env = {key: os.environ[key] for key in (
                        "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "ANSIBLE_ASYNC_DIR", "DIB7_VENV_BIN",
                        "ANSIBLE_COLLECTIONS_PATH", "ANSIBLE_LOCAL_TEMP", "WEB_INPUT_DIR",
                        "LIBGUESTFS_PATH", "OVFTOOL_ACCEPT_LICENSE", "OVFTOOL_INSTALL_DIR",
                        "OPENVDDK_INSTALL_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                        "https_proxy", "http_proxy", "no_proxy") if key in os.environ}
                    env.update({"ARTIFACT_ROOT": str(ARTIFACT_ROOT),
                                "MIGRATE_V2V_VAULT_DIR": str(VAULT_DIR),
                                "MIGRATE_V2V_CATALOG_DIR": str(CATALOG_DIR),
                                "VAULT_PASSWORD_FILE": str(PASSFILE),
                                "PYTHONUNBUFFERED": "1"})
                    env.update(values)
                    command = [str(wrapper_path(row["mode"], row["cloud"], row["method"]))]
                    if recovery_action:
                        if (recovery_action not in ("resume", "discard")
                                or not isinstance(recovery_run_id, str)
                                or not re.fullmatch(r"[0-9]{1,20}", recovery_run_id)
                                or not isinstance(recovery_source_job_id, int)):
                            raise ValueError("invalid queued recovery action")
                        extra_vars = ({"resume_run_id": recovery_run_id}
                                      if recovery_action == "resume" else
                                      {"discard_run_id": recovery_run_id,
                                       "discard_allow_empty": True})
                        command.extend(("-e", json.dumps(extra_vars)))
                        log.write(
                            f"Queued {recovery_action} of failed run {recovery_run_id} "
                            f"from job #{recovery_source_job_id}\n".encode()
                        )
                    # Check for shutdown under the same lock stop_active_process
                    # takes: otherwise a SIGTERM between the check and Popen
                    # finds no active process, and the job starts orphaned.
                    with ACTIVE_LOCK:
                        if STOP_EVENT.is_set():
                            stopped = True
                            raise RuntimeError("controller is shutting down")
                        ACTIVE_PROCESS = subprocess.Popen(
                            command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    code = ACTIVE_PROCESS.wait()
                    if STOP_EVENT.is_set():
                        stopped = True
                        log.write(b"Job stopped because the controller is shutting down\n")
                except Exception as error:
                    error_text = str(error)
                    log.write(f"Job failed before completion: {error}\n".encode(errors="replace"))
                finally:
                    with ACTIVE_LOCK:
                        ACTIVE_PROCESS = None
        except Exception as error:
            error_text = str(error)
            sys.stderr.write(f"web worker: job {row['id']} log error: {error}\n")
        finally:
            # A job ended by controller shutdown is "interrupted", the same
            # status a restart gives a job left running, whichever of the two
            # gets to write it first.
            if stopped:
                status = "interrupted"
            elif code == 0 and error_text is None:
                status = "completed"
            else:
                status = "failed"
            try:
                with db() as conn:
                    conn.execute(
                        "UPDATE jobs SET status=?, finished=datetime('now'), exit_code=?, log_path=? WHERE id=?",
                        (status, code, str(log_path), row["id"]))
                    if status == "completed" and recovery_source_job_id:
                        mark_recovery_complete(conn, recovery_source_job_id, row["id"])
            except Exception as error:
                sys.stderr.write(f"web worker: could not save job {row['id']} status: {error}\n")

def stop_active_process():
    with ACTIVE_LOCK:
        process = ACTIVE_PROCESS
    if not process:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    deadline = time.monotonic() + 10
    while True:
        if process.poll() is None:
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        if time.monotonic() >= deadline:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
        STOP_EVENT.wait(0.1)
    process.wait()

def trusted_client_ip(handler):
    """Use Caddy's forwarded client IP; the application port is proxy-private."""
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        candidate = forwarded.split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return handler.client_address[0]

def prune_login_failures(now):
    with LOGIN_LOCK:
        for key, times in list(LOGIN_FAILURES.items()):
            live = [timestamp for timestamp in times if now - timestamp < 300]
            if live:
                LOGIN_FAILURES[key] = live
            else:
                del LOGIN_FAILURES[key]
        if len(LOGIN_FAILURES) > 10000:
            oldest = sorted(LOGIN_FAILURES, key=lambda key: LOGIN_FAILURES[key][-1])
            for key in oldest[:len(LOGIN_FAILURES) - 10000]:
                del LOGIN_FAILURES[key]

class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        try:
            limit = int(os.environ.get("WEB_MAX_REQUEST_THREADS", "64"))
        except ValueError:
            limit = 64
        self.request_slots = threading.BoundedSemaphore(max(8, min(limit, 256)))
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()

def page(title, body, user=None, token=""):
    nav = '<a href="/">Overview</a><a href="/jobs">Jobs</a><a href="/maintenance">Maintenance</a>' if user else ''
    account = f'<span class="user">{esc(user)}</span><form method="post" action="/logout">{csrf_input(token)}<button class="text-button">Sign out</button></form>' if user else ''
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · migrate-v2v</title><link rel="stylesheet" href="/static/style.css"></head>
<body><header class="topbar"><a class="brand" href="/"><span class="brand-mark">◆</span>migrate-v2v</a><nav>{nav}</nav><div class="account">{account}</div></header>
<main>{body}</main><footer>VMware to cloud <span>·</span> Snapshot safely. Export or stream. Land natively.</footer></body></html>'''

def csrf_input(token):
    return f'<input type="hidden" name="_csrf" value="{esc(token)}">'

def project_picker(mode, cloud, method, user, token, errors=()):
    available = targets(cloud, mode)
    options = ''.join(f'<option value="{esc(name)}">{esc(name)}</option>' for name in available)
    warning = '<div class="alert">' + '<br>'.join(esc(error) for error in errors) + '</div>' if errors else ''
    if not available:
        warning += f'<div class="alert">No {esc(cloud_name(cloud))} environments could be read from the vault.</div>'
    path = f'/new/{mode}/{cloud}/{method}'
    description = ("The project determines the available networks, security groups, sizes and keys."
                   if mode == "launch" else "Choose the destination environment before entering the source VM and vCenter.")
    body = f'''<div class="eyebrow">{esc(mode)} / {esc(cloud)}</div><div class="page-heading"><div><h1>Choose a project</h1>
<p>{esc(description)}</p></div><a class="back" href="/">← All environments</a></div>
{warning}<form class="panel form-panel" method="get" action="{path}"><div class="field"><label for="project">{esc(F[target_key(cloud)].label)}</label>
<select id="project" name="project" required><option value="">Select…</option>{options}</select></div>
<div class="form-actions"><span>{"Choices are loaded from the selected environment." if mode == "launch" else "The selected project will be used for this migration."}</span><button class="button">Continue</button></div></form>'''
    return page(f"Choose {mode} project", body, user, token)

def nic_fields_html(macs, posted, suggested):
    """Confirmation and one network per NIC for a multi-NIC source VM."""
    count = len(macs)
    multi = posted.get("_nic_multi", "yes")
    options = ''.join(
        f'<option value="{value}"{" selected" if multi == value else ""}>{esc(label)}</option>'
        for value, label in (("yes", f"Yes - create {count} NICs"), ("no", "No - create a single NIC")))
    listing = ', '.join(f'NIC {index + 1}: {mac}' for index, mac in enumerate(macs))
    html = (f'<div class="field"><label for="_nic_multi">Source VM had {count} NICs</label>'
            f'<select id="_nic_multi" name="_nic_multi">{options}</select>'
            f'<small>{esc(listing)}. Choose No to use the single network below instead. '
            'After choosing a different migration run, use Update choices.</small></div>')
    for index, mac in enumerate(macs):
        name = f"_nic_network_{index + 1}"
        # Only NIC 1 gets the default network ; a later NIC left on it would
        # silently share NIC 1's network, so the operator must choose.
        value = posted.get(name, F["OS_NETWORK"].default if index == 0 else "")
        datalist = ''
        list_id = ''
        if suggested:
            list_id = f' list="choices_{name}"'
            datalist = f'<datalist id="choices_{name}">' + ''.join(
                f'<option value="{esc(v)}" label="{esc(label)}"></option>' for v, label in suggested) + '</datalist>'
        html += (f'<div class="field"><label for="{name}">Network for NIC {index + 1}</label>'
                 f'<input id="{name}" name="{name}" type="text" value="{esc(value)}" autocomplete="off"{list_id}>'
                 f'{datalist}<small>Source MAC {esc(mac)}. Used when creating {count} NICs.</small></div>')
    return html

def form_html(mode, cloud, method, token, posted=None, errors=(), user=None):
    posted = posted or {}
    fields = exported_fields(mode, cloud, method)
    fresh = not posted or set(posted) == {target_key(cloud)}
    project = posted.get(target_key(cloud), "")
    if project not in targets(cloud, mode):
        return project_picker(mode, cloud, method, user, token, errors or (["Select an environment from the vault."] if project else []))
    choices, defaults, unavailable = launch_choices(cloud, project, posted) if mode == "launch" else ({}, {}, [])
    migrations = catalog_migrations(cloud, project) if mode == "launch" else []
    nic_macs = []
    if migrations:
        nic_macs = launch_nic_macs(cloud, project,
                                   posted.get("MIGRATION_ID") or str(migrations[0].get("clone_name", "")))
    sections = []
    for name in fields:
        if name == target_key(cloud):
            sections.append(f'<input type="hidden" name="{name}" value="{esc(project)}">')
            continue
        meta = F[name]
        if name == "OS_NETWORK" and len(nic_macs) > 1:
            sections.append(nic_fields_html(nic_macs, posted, choices.get("OS_NETWORK", [])))
        field_default = str(INPUT_DIR / "virtio-win.iso") if name == "VIRTIO_WIN" else meta.default
        value = posted.get(name, defaults.get(name, field_default))
        options = None
        if name == "VSPHERE_TARGET_VCENTER":
            options = [(t, t) for t in vault_keys("vsphere.yml", "vsphere_projects")]
        elif name == "VM_NAME" and mode == "launch" and migrations:
            selected_id = posted.get("MIGRATION_ID") or str(migrations[0].get("clone_name", ""))
            selected = next((row for row in migrations
                             if str(row.get("clone_name", "")) == selected_id), migrations[0])
            sections.append(f'<input type="hidden" id="VM_NAME" name="VM_NAME" value="{esc(selected.get("vm_name", ""))}">')
            continue
        elif name == "VM_NAME" and mode == "launch":
            options = [(v, v) for v in catalog_vms(cloud)]
        elif name == "MIGRATION_ID" and mode == "launch":
            options = [(str(row.get("clone_name", "")),
                        catalog_migration_label(cloud, row)) for row in migrations]
            if not posted.get("MIGRATION_ID") and options:
                value = options[0][0]
        elif meta.kind == "boolean":
            if name in ("AZURE_NO_PUBLIC_IP", "GCP_NO_ADDRESS"):
                options = [("false", "Yes"), ("true", "No")]
            else:
                options = [("true", "Yes"), ("false", "No")]
        elif meta.kind == "choice":
            options = [(v, v or "Vault default") for v in meta.choices]
        suggested = choices.get(name, [])
        if mode == "launch" and name in ("AWS_SECURITY_GROUPS_JSON", "OS_SECURITY_GROUPS_JSON", "OS_SECURITY_GROUP_IDS_JSON"):
            selected = posted.get(f"_pick_{name}", [])
            if fresh and not selected:
                selected = [v for v, label in suggested if v == "default" or "default ·" in label]
            picked = ''.join(
                f'<label class="choice-row"><input type="checkbox" name="_pick_{name}" value="{esc(v)}"'
                f'{" checked" if v in selected else ""}><span>{esc(label)}</span></label>'
                for v, label in suggested)
            chooser = f'<div class="choice-list" role="group" aria-label="{esc(meta.label)}">{picked}</div>' if suggested else ''
            control = chooser + f'<input id="{name}" name="{name}" type="text" value="{esc(value)}" autocomplete="off" aria-label="Manual JSON override"><small>Or enter a JSON array of names or IDs manually. A nonempty manual value overrides the selection.</small>'
        elif name == "MIGRATION_ID" and mode == "launch" and options:
            control = f'<select id="{name}" name="{name}" required>' + ''.join(
                f'<option value="{esc(v)}" data-vm="{esc(migrations[index].get("vm_name", ""))}"'
                f'{" selected" if value == v else ""}>{esc(label)}</option>'
                for index, (v, label) in enumerate(options)) + '</select>'
        elif options is not None:
            placeholder = '<option value="">Select…</option>' if meta.required or name == target_key(cloud) else ''
            control = f'<select id="{name}" name="{name}">{placeholder}' + ''.join(
                f'<option value="{esc(v)}"{" selected" if value == v else ""}>{esc(label)}</option>' for v, label in options) + '</select>'
        elif meta.kind == "textarea":
            control = f'<textarea id="{name}" name="{name}" rows="5">{esc(value)}</textarea>'
        else:
            list_id = f' list="choices_{name}"' if suggested else ''
            control = f'<input id="{name}" name="{name}" type="text" value="{esc(value)}" autocomplete="off"{list_id}>'
            if suggested:
                control += f'<datalist id="choices_{name}">' + ''.join(
                    f'<option value="{esc(v)}" label="{esc(label)}"></option>' for v, label in suggested) + '</datalist>'
        help_text = f'<small>{esc(meta.help)}</small>' if meta.help else ''
        required = meta.required
        if cloud == "azure" and name == "AZURE_SSH_SOURCE_PREFIX":
            no_public_ip = posted.get(
                "AZURE_NO_PUBLIC_IP",
                defaults.get("AZURE_NO_PUBLIC_IP", F["AZURE_NO_PUBLIC_IP"].default),
            )
            required = no_public_ip != "true"
        sections.append(f'<div class="field"><label for="{name}">{esc(meta.label)}{" <b>*</b>" if required else ""}</label>{control}{help_text}</div>')
    error_html = '<div class="alert">' + '<br>'.join(esc(e) for e in errors) + '</div>' if errors else ''
    if unavailable:
        error_html += '<div class="notice">Could not load ' + esc(", ".join(unavailable)) + '. Enter known values manually.</div>'
    if "DISCARD_RUN_ID" in fields:
        checked = " checked" if posted.get("_confirm_discard") == "yes" else ""
        sections.append(f'<div class="field"><label class="check-label"><input type="checkbox" name="_confirm_discard" value="yes"{checked}> Confirm discarding the failed run if a run ID is entered</label></div>')
    action = f'/new/{mode}/{cloud}/{method}'
    project_banner = f'<div class="project-banner panel"><span><small>{esc(F[target_key(cloud)].label)}</small><strong>{esc(project)}</strong></span><a href="{action}">Change project</a></div>'
    refresh = '<button class="text-button" type="submit" name="_refresh" value="1" formnovalidate>Update choices</button>' if mode == "launch" and cloud in ("aws", "gcp", "openstack") else ''
    return page(f'{mode.title()} to {cloud_name(cloud)}', f'''<div class="eyebrow">{esc(mode)} / {esc(cloud)} / {esc(method)}</div>
<div class="page-heading"><div><h1>{esc(mode.title())} · {esc(cloud_name(cloud))}</h1><p>Review the values before adding this operation to the queue.</p></div><a class="back" href="/">← All environments</a></div>
{project_banner}{error_html}<form class="panel form-panel" method="post" action="{action}">{csrf_input(token)}<div class="fields">{''.join(sections)}</div>
<div class="form-actions"><span>One operation runs at a time. {"Update choices after changing a subnet, zone or network." if refresh else ""}</span>{refresh}<button class="button" type="submit">Queue {esc(mode)}</button></div></form>''', user, token)

class Handler(BaseHTTPRequestHandler):
    server_version = "migrate-v2v-web"
    timeout = 15

    def log_message(self, fmt, *args):
        sys.stderr.write(f"web: {self.address_string()} {fmt % args}\n")

    def send(self, status, body="", headers=None, content_type="text/html; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def cookie(self):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            return jar["session"].value if "session" in jar else ""
        except cookies.CookieError:
            return ""

    def session(self):
        return unsign(self.cookie())

    def user(self):
        session = self.session()
        name = session.get("user") if session else None
        stored = users().get(name) if name else None
        version = hashlib.sha256(stored.encode()).hexdigest() if stored else None
        return name if version and hmac.compare_digest(session.get("auth", ""), version) else None

    def session_cookie(self, token, max_age=SESSION_AGE):
        secure = "; Secure" if os.environ.get("WEB_SECURE_COOKIE", "true").lower() == "true" else ""
        return f"session={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{secure}"

    def csrf(self):
        session = self.session()
        return session.get("csrf", "") if session else ""

    def redirect(self, path, headers=None):
        self.send(303, "", {"Location": path, **(headers or {})})

    def require_auth(self):
        if not self.user():
            self.redirect("/login")
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/static/style.css":
            return self.send(200, (ROOT / "web/static/style.css").read_bytes(), content_type="text/css; charset=utf-8")
        if path == "/login":
            if self.user():
                return self.redirect("/")
            session = self.session()
            if session and not session.get("user") and session.get("csrf"):
                csrf = session["csrf"]
                headers = None
            else:
                csrf = secrets.token_urlsafe(24)
                token = sign({"exp": int(time.time()) + 600, "csrf": csrf})
                headers = {"Set-Cookie": self.session_cookie(token, 600)}
            body = f'''<section class="login panel"><div class="eyebrow"><i></i>CONTROLLER ACCESS</div><h1>Sign in</h1><p>Use a local account for this controller.</p><form method="post" action="/login">{csrf_input(csrf)}<label>Username<input name="username" autocomplete="username" required></label><label>Password<input name="password" type="password" autocomplete="current-password" required></label><button class="button">Sign in</button></form></section>'''
            return self.send(200, page("Sign in", body), headers)
        if not self.require_auth():
            return
        user = self.user()
        csrf = self.csrf()
        if path == "/":
            cards = []
            for cloud in CLOUDS:
                for mode in ("migrate", "launch"):
                    methods = ("standard", "vddk") if mode == "migrate" and cloud in ("azure", "openstack") else ("standard",)
                    links = ''.join(f'<a href="/new/{mode}/{cloud}/{method}">{"Datastore direct" if method == "vddk" else "Open form"} <span>↗</span></a>' for method in methods)
                    cards.append(f'<article class="cloud-card"><div class="card-icon {cloud}">{icon_html(cloud)}</div><div><div class="card-kicker">{mode.upper()}</div><h3>{cloud_name(cloud)}</h3></div><div class="card-links">{links}</div></article>')
            with db() as conn:
                recent = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 5").fetchall()
            body = '''<section class="hero"><div class="eyebrow"><i></i>VMWARE TO CLOUD</div><h1>Move with clarity.</h1><p>Choose a cloud, migrate a VM, then launch from its completed image. Operations are queued and run one at a time.</p></section>'''
            body += '<div class="section-title"><h2>Migrate</h2><p>Create a cloud image from a vSphere VM.</p></div><div class="card-grid">' + ''.join(cards[::2]) + '</div>'
            body += '<div class="section-title"><h2>Launch</h2><p>Start an instance from a completed migration.</p></div><div class="card-grid">' + ''.join(cards[1::2]) + '</div>'
            body += '<div class="section-title"><h2>Recent jobs</h2><a href="/jobs">View all →</a></div>' + jobs_table(recent)
            return self.send(200, page("Overview", body, user, csrf))
        if path.startswith("/new/"):
            parts = path.strip("/").split("/")
            if len(parts) != 4:
                return self.send(404)
            _, mode, cloud, method = parts
            try:
                selected = parse_qs(urlsplit(self.path).query).get("project", [""])[-1]
                posted = {target_key(cloud): selected} if mode in ("migrate", "launch") and cloud in CLOUDS and selected else None
                return self.send(200, form_html(mode, cloud, method, csrf, posted=posted, user=user))
            except ValueError:
                return self.send(404)
        if path == "/jobs":
            with db() as conn:
                rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 100").fetchall()
            return self.send(200, page("Jobs", '<div class="page-heading"><div><div class="eyebrow"><i></i>VMWARE TO CLOUD / OPERATIONS</div><h1>Jobs</h1><p>Queued jobs run in submission order.</p></div></div>' + jobs_table(rows), user, csrf))
        if path == "/maintenance":
            return self.send(200, maintenance_home(user, csrf))
        if path == "/maintenance/catalog":
            return self.send(200, maintenance_catalog(user, csrf))
        if path == "/maintenance/catalog/raw":
            return self.send(200, maintenance_catalog_raw(user, csrf))
        if path == "/maintenance/jobs":
            query = parse_qs(urlsplit(self.path).query)
            cleared = None
            try:
                if "cleared" in query:
                    cleared = max(0, int(query["cleared"][-1]))
                log_errors = max(0, int(query.get("log_errors", ["0"])[-1]))
            except ValueError:
                cleared, log_errors = None, 0
            return self.send(200, maintenance_jobs(user, csrf, cleared, log_errors))
        if path == "/maintenance/failed":
            return self.send(200, maintenance_failed_jobs(user, csrf))
        # At most 18 digits: SQLite integers are 64-bit, and a longer id would
        # raise OverflowError in the query rather than simply not match.
        match = re.fullmatch(r"/jobs/(\d{1,18})", path)
        if match:
            job_id = int(match.group(1))
            with db() as conn:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return self.send(404)
            log = ""
            log_path = Path(row["log_path"]) if row["log_path"] else STATE / f"job-{row['id']}.log"
            if log_path.is_file():
                with open(log_path, "rb") as stream:
                    stream.seek(max(0, stream.seek(0, 2) - 200000))
                    log = stream.read().decode(errors="replace")
            body = f'''<div class="page-heading"><div><div class="eyebrow">JOB #{row['id']}</div><h1>{esc(row['mode'].title())} · {esc(row['vm'])}</h1><p>{esc(cloud_name(row['cloud']))} / {esc(row['method'])} / submitted by {esc(row['user'])}</p></div><a class="back" href="/jobs">← All jobs</a></div>
            <div class="job-summary panel"><span class="badge {esc(row['status'])}">{esc(row['status'])}</span><span>Created {esc(row['created'])} UTC</span><span>Started {esc(row['started'] or '—')}</span><span>Finished {esc(row['finished'] or '—')}</span></div>
            <h2>Output</h2><pre class="job-log">{esc(log or 'Waiting for output…')}</pre>
            <div class="job-footer"><a class="back" href="/jobs">← All jobs</a></div>'''
            if row["status"] == "queued":
                body += f'''<form method="post" action="/jobs/{job_id}/cancel" class="job-cancel-form">{csrf_input(csrf)}
<button class="button danger-button" type="submit">Cancel queued job</button></form>'''
            if row["status"] in ("queued", "running"):
                body += '<meta http-equiv="refresh" content="5">'
            return self.send(200, page(f"Job {row['id']}", body, user, csrf))
        return self.send(404)

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send(400)
        if not 0 < length <= MAX_BODY:
            return self.send(413)
        try:
            raw = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            return self.send(400)
        posted = {k: (v if k.startswith("_pick_") else v[-1]) for k, v in raw.items()}
        submitted_csrf = posted.get("_csrf", "")
        if (not self.csrf() or not submitted_csrf.isascii()
                or not hmac.compare_digest(submitted_csrf, self.csrf())):
            return self.send(403, page("Invalid request", '<div class="alert">The form expired. Reload and try again.</div>'))
        if path == "/login":
            ip = trusted_client_ip(self)
            now = time.time()
            name, password = posted.get("username", ""), posted.get("password", "")
            if (not name or len(name) > MAX_LOGIN_NAME
                    or not 12 <= len(password) <= MAX_LOGIN_PASSWORD):
                return self.send(400, page(
                    "Invalid sign-in", '<div class="alert">Username or password is outside the allowed length.</div>'))
            failure_key = (ip, name.casefold())
            ip_key = (ip, None)
            prune_login_failures(now)
            if not LOGIN_HASH_SLOTS.acquire(blocking=False):
                return self.send(429, page(
                    "Try later", '<div class="alert">Sign-in is busy. Try again shortly.</div>'))
            # Count this attempt before the slow scrypt check, in the same
            # locked step as the limit test, so parallel requests cannot all
            # pass the check before any of them is recorded.
            try:
                with LOGIN_LOCK:
                    failures = LOGIN_FAILURES.get(failure_key, [])
                    ip_failures = LOGIN_FAILURES.get(ip_key, [])
                    limited = len(failures) >= 10 or len(ip_failures) >= 30
                    if not limited:
                        failures = LOGIN_FAILURES.setdefault(failure_key, failures)
                        ip_failures = LOGIN_FAILURES.setdefault(ip_key, ip_failures)
                        failures.append(now)
                        ip_failures.append(now)
                if limited:
                    return self.send(429, page("Try later", '<div class="alert">Too many sign-in attempts. Try again in five minutes.</div>'))
                stored = users().get(name, "")
                matched = verify_password(password, stored or DUMMY_PASSWORD_HASH)
            finally:
                LOGIN_HASH_SLOTS.release()
            if not stored or not matched:
                return self.send(401, page("Sign in", '<div class="alert">Invalid username or password.</div><a href="/login">Try again</a>'))
            with LOGIN_LOCK:
                LOGIN_FAILURES.pop(failure_key, None)
            token = sign({"user": name, "auth": hashlib.sha256(stored.encode()).hexdigest(),
                          "exp": int(now) + SESSION_AGE, "csrf": secrets.token_urlsafe(24)})
            return self.redirect("/", {"Set-Cookie": self.session_cookie(token)})
        if not self.require_auth():
            return
        if path == "/logout":
            return self.redirect("/login", {"Set-Cookie": self.session_cookie("", 0)})
        match = re.fullmatch(r"/jobs/(\d{1,18})/cancel", path)
        if match:
            job_id = int(match.group(1))
            try:
                cancel_queued_job(job_id)
            except ValueError as error:
                return self.send(400, page("Cannot cancel job", f'<div class="alert">{esc(error)}</div><a class="back" href="/jobs/{job_id}">Back to job</a>', self.user(), self.csrf()))
            return self.redirect(f"/jobs/{job_id}")
        if path == "/maintenance/jobs/clear":
            if posted.get("confirm") != "yes":
                return self.send(400, maintenance_jobs(
                    self.user(), self.csrf(), error="Check the confirmation box to clear finished jobs."))
            cleared, log_errors = clear_finished_jobs()
            return self.redirect(f"/maintenance/jobs?cleared={cleared}&log_errors={log_errors}")
        match = re.fullmatch(r"/maintenance/failed/(\d{1,18})/(resume|discard)", path)
        if match:
            job_id, action = int(match.group(1)), match.group(2)
            if action == "discard" and posted.get("confirm_discard") != "yes":
                return self.send(400, maintenance_failed_jobs(
                    self.user(), self.csrf(), error="Confirm that the retained run should be discarded."))
            try:
                recovery_job_id = queue_recovery(self.user(), job_id, action)
            except ValueError as error:
                return self.send(400, maintenance_failed_jobs(self.user(), self.csrf(), error=str(error)))
            return self.redirect(f"/jobs/{recovery_job_id}")
        match = re.fullmatch(r"/new/(migrate|launch)/(aws|azure|gcp|openstack)/(standard|vddk)", path)
        if match:
            mode, cloud, method = match.groups()
            try:
                if mode == "launch" and posted.get("_refresh") == "1":
                    return self.send(200, form_html(mode, cloud, method, self.csrf(), posted, user=self.user()))
                values, errors = validate(mode, cloud, method, posted)
            except ValueError:
                return self.send(404)
            if errors:
                return self.send(400, form_html(mode, cloud, method, self.csrf(), posted, errors, self.user()))
            job_id = enqueue(self.user(), mode, cloud, method, values)
            return self.redirect(f"/jobs/{job_id}")
        return self.send(404)

def jobs_table(rows):
    if not rows:
        return '<div class="panel empty">No jobs yet.</div>'
    items = ''.join(f'''<tr><td><a href="/jobs/{r['id']}">#{r['id']}</a></td><td>{esc(r['mode'].title())}</td><td>{esc(cloud_name(r['cloud']))}</td><td>{esc(r['vm'])}</td><td><span class="badge {esc(r['status'])}">{esc(r['status'])}</span></td><td>{esc(r['created'])} UTC</td></tr>''' for r in rows)
    return f'<div class="table-wrap panel"><table><thead><tr><th>Job</th><th>Mode</th><th>Cloud</th><th>VM</th><th>Status</th><th>Created</th></tr></thead><tbody>{items}</tbody></table></div>'

def job_status_counts():
    with db() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS total FROM jobs GROUP BY status").fetchall()
    return {row["status"]: row["total"] for row in rows}

def maintenance_home(user, token):
    counts = job_status_counts()
    finished = sum(counts.get(status, 0) for status in TERMINAL_JOB_STATUSES)
    active = counts.get("queued", 0) + counts.get("running", 0)
    recovery_count = sum(
        bool((item["actions"]["resume_run_id"] or item["actions"]["discard_run_id"])
             and not item.get("pending_job_id"))
        for item in recovery_jobs()
    )
    body = f'''<div class="page-heading"><div><div class="eyebrow"><i></i>VMWARE TO CLOUD / CONTROLLER</div><h1>Maintenance</h1>
<p>Review the catalogs, recover failed runs, or clear finished jobs.</p></div></div>
<div class="maintenance-grid">
<article class="maintenance-card panel"><div class="eyebrow">CATALOGS</div><h2>Migration and launch records</h2>
<p>View concise provider summaries for printing, or inspect the complete catalog JSON.</p>
<a class="button" href="/maintenance/catalog">Open printable catalog</a></article>
<article class="maintenance-card panel"><div class="eyebrow">MIGRATION RECOVERY</div><h2>Manage failed jobs</h2>
<p>{recovery_count} failed migration run{"s" if recovery_count != 1 else ""} have a resume or discard action available.</p>
<a class="button" href="/maintenance/failed">Review failed jobs</a></article>
<article class="maintenance-card panel"><div class="eyebrow">JOB QUEUE</div><h2>Clear finished jobs</h2>
<p>{finished} finished; {active} queued or running. Recovery-ready failed runs stay available until handled.</p>
<a class="button" href="/maintenance/jobs">Manage job queue</a></article>
</div>'''
    return page("Maintenance", body, user, token)

def maintenance_failed_jobs(user, token, error=""):
    items = recovery_jobs()
    rows = []
    for item in items:
        row = item["row"]
        actions = item["actions"]
        resume_id = actions["resume_run_id"]
        discard_id = actions["discard_run_id"]
        hinted = recovery_hinted_run_ids(row)
        run_ids = sorted({value for value in (*hinted.values(), resume_id, discard_id) if value})
        if item.get("resolved_run_id"):
            run_cell = esc(item["resolved_run_id"])
        elif run_ids:
            run_cell = ", ".join(esc(value) for value in run_ids)
        else:
            run_cell = "Not found in job output"

        if item.get("resolved_run_id"):
            no_resources_job_id = item["no_resources_job_id"]
            action_html = (
                f'<span class="recovery-unavailable">Job '
                f'<a href="/jobs/{no_resources_job_id}">#{no_resources_job_id}</a> '
                'confirmed no artifacts remain; no discard is needed.</span>'
            )
        elif item.get("pending_job_id"):
            action_html = f'<span class="recovery-pending">Recovery queued or running as <a href="/jobs/{item["pending_job_id"]}">job #{item["pending_job_id"]}</a>.</span>'
        else:
            controls = []
            if resume_id:
                controls.append(f'''<form method="post" action="/maintenance/failed/{row['id']}/resume">{csrf_input(token)}
<button class="button" type="submit">Queue resume</button></form>''')
            if discard_id:
                controls.append(f'''<form method="post" action="/maintenance/failed/{row['id']}/discard">{csrf_input(token)}
<label class="confirm-row"><input type="checkbox" name="confirm_discard" value="yes" required> Discard run {esc(discard_id)}</label>
<button class="button danger-button" type="submit">Queue discard</button></form>''')
            if controls:
                action_html = ''.join(controls)
            elif row["cloud"] == "aws":
                action_html = '<span class="recovery-unavailable">AWS migrations do not support run recovery.</span>'
            else:
                action_html = '<span class="recovery-unavailable">No supported resume or discard action was identified.</span>'

        metadata = recovery_metadata(row)
        resolved_by = metadata.get("_recovery_completed_by")
        if resolved_by:
            action_html = f'<span class="recovery-pending">Recovery completed by <a href="/jobs/{esc(resolved_by)}">job #{esc(resolved_by)}</a>.</span>'
        rows.append(f'''<tr><td><a href="/jobs/{row['id']}">#{row['id']}</a></td>
<td>{esc(cloud_name(row['cloud']))} / {esc(row['method'])}</td><td>{esc(row['vm'])}</td>
<td><span class="badge {esc(row['status'])}">{esc(row['status'])}</span></td>
<td class="recovery-run-id">{run_cell}</td><td class="recovery-actions">{action_html}</td></tr>''')

    notice = f'<div class="alert">{esc(error)}</div>' if error else ''
    table = (f'''<div class="table-wrap panel"><table><thead><tr><th>Job</th><th>Destination / method</th><th>VM</th>
<th>Status</th><th>Run ID</th><th>Actions</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>'''
             if rows else '<div class="panel empty">No failed or interrupted migration jobs to review.</div>')
    body = f'''<div class="page-heading"><div><div class="eyebrow">MAINTENANCE / MIGRATION RECOVERY</div><h1>Manage failed jobs</h1>
<p>This panel lists failed or interrupted migrations. Resume retained runs or discard them when the playbook offers that action; run IDs are read from saved job output.</p></div>
<a class="back" href="/maintenance">← Maintenance</a></div>
{notice}<div class="notice recovery-note">Resume is available for retained Azure, GCP, and OpenStack migration runs. Discard is available for datastore-direct Azure and OpenStack runs. Launch jobs are not listed; AWS migrations and failures without a retained run ID have no recovery action. Clearing finished jobs preserves runs with pending recovery actions.</div>
{table}'''
    return page("Manage failed jobs", body, user, token)

def load_catalog_document(filename, list_key):
    path = CATALOG_DIR / filename
    if not path.exists():
        return None, None
    try:
        catalog = read_snapshot(path, list_key)
        entries = catalog[list_key]
        if not isinstance(entries, list):
            raise ValueError(f"expected a {list_key} list")
        if any(not isinstance(entry, dict) for entry in entries):
            raise ValueError(f"{list_key} entries must be objects")
        return catalog, None
    except (OSError, TypeError, ValueError) as error:
        return None, f"{filename}: {error}"

def catalog_field(fields, label, value):
    if value is None or value == "" or value == []:
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            catalog_field(fields, f"{label} {index + 1}", item)
    elif isinstance(value, dict):
        fields.append((label, json.dumps(value, ensure_ascii=False, sort_keys=True)))
    else:
        fields.append((label, str(value)))

def migration_summary_fields(entry):
    cloud = str(entry.get("target_platform") or "").lower()
    project_keys = {
        "aws": ("aws_project", "aws_account_id"),
        "azure": ("azure_subscription_id",),
        "gcp": ("gcp_project",),
        "openstack": ("openstack_project", "openstack_project_id"),
    }.get(cloud, ())
    project_values = list(dict.fromkeys(
        str(entry[key]) for key in project_keys if entry.get(key)))
    project = ", ".join(project_values)
    region = {
        "aws": entry.get("aws_region"),
        "azure": entry.get("azure_location"),
        "gcp": entry.get("gcp_import_location"),
        "openstack": entry.get("openstack_region"),
    }.get(cloud)
    fields = []
    for label, value in (
        ("Status", entry.get("status")),
        ("Migrated at", entry.get("migrated_at")),
        ("Migration ID", entry.get("clone_name")),
        ("Source vCenter", entry.get("source_vcenter")),
        ("Datacenter", entry.get("source_datacenter")),
        ("Project/account", project),
        ("Region/location", region),
        ("Artifact type", entry.get("artifact_type")),
    ):
        catalog_field(fields, label, value)
    if cloud == "aws":
        catalog_field(fields, "AMI ID", entry.get("ami_id"))
        catalog_field(fields, "AMI name", entry.get("ami_name"))
    elif cloud == "openstack":
        ids = entry.get("openstack_image_ids") or []
        names = entry.get("openstack_image_names") or []
        for index in range(max(len(ids), len(names))):
            catalog_field(fields, f"Disk {index} ID", ids[index] if index < len(ids) else None)
            catalog_field(fields, f"Disk {index} name", names[index] if index < len(names) else None)
    elif cloud == "gcp":
        catalog_field(fields, "Boot image", entry.get("gcp_boot_image_name"))
        catalog_field(fields, "Boot image URL", entry.get("gcp_boot_image_selflink"))
        names = entry.get("gcp_data_image_names") or []
        links = entry.get("gcp_data_image_selflinks") or []
        for index in range(max(len(names), len(links))):
            catalog_field(fields, f"Disk {index + 1} name", names[index] if index < len(names) else None)
            catalog_field(fields, f"Disk {index + 1} URL", links[index] if index < len(links) else None)
    elif cloud == "azure":
        catalog_field(fields, "Boot image", entry.get("azure_boot_image_name"))
        catalog_field(fields, "Boot image ID", entry.get("azure_boot_image_id"))
        names = entry.get("azure_data_snapshot_names", entry.get("azure_data_image_names")) or []
        ids = entry.get("azure_data_snapshot_ids", entry.get("azure_data_image_ids")) or []
        for index in range(max(len(names), len(ids))):
            catalog_field(fields, f"Disk {index + 1} name", names[index] if index < len(names) else None)
            catalog_field(fields, f"Disk {index + 1} ID", ids[index] if index < len(ids) else None)
    return fields

def deployment_summary_fields(entry):
    cloud = str(entry.get("target_platform") or "").lower()
    fields = []
    common = (("Status", entry.get("status")), ("Launched at", entry.get("launched_at")))
    for label, value in common:
        catalog_field(fields, label, value)
    if cloud == "aws":
        details = (("Instance ID", "instance_id"), ("Instance type", "instance_type"),
                   ("AMI ID", "ami_id"), ("AWS account", "aws_account_id"),
                   ("Region", "aws_region"), ("Subnet", "vpc_subnet_id"))
    elif cloud == "openstack":
        details = (("Server ID", "openstack_server_id"), ("Project", "openstack_project"),
                   ("Region", "openstack_region"), ("Flavor", "openstack_flavor"),
                   ("Network", "openstack_network"), ("Image IDs", "openstack_image_ids"),
                   ("Data volume IDs", "openstack_data_volume_ids"))
    elif cloud == "gcp":
        details = (("Instance", "gcp_instance_name"), ("Project", "gcp_project"),
                   ("Zone", "gcp_zone"), ("Machine type", "gcp_machine_type"),
                   ("Boot image", "gcp_boot_image_name"), ("Data disks", "gcp_data_disk_names"))
    elif cloud == "azure":
        details = (("Azure VM", "azure_vm_name"), ("Subscription", "azure_subscription_id"),
                   ("Resource group", "azure_resource_group"), ("Location", "azure_location"),
                   ("VM size", "azure_vm_size"), ("Boot image ID", "azure_boot_image_id"),
                   ("Data disks", "azure_data_disk_names"))
    else:
        details = ()
    for label, key in details:
        catalog_field(fields, label, entry.get(key))
    return fields

def catalog_records(title, entries, summary_builder):
    if not entries:
        return f'<p class="catalog-empty">No {esc(title.lower())} entries.</p>'
    records = []
    for entry in entries:
        name = entry.get("vm_name") or entry.get("azure_vm_name") or "Unnamed VM"
        timestamp = entry.get("migrated_at" if title == "Migration" else "launched_at") or ""
        time_label = f" · {esc(timestamp)}" if timestamp else ""
        status = entry.get("status") or "unknown"
        details = ''.join(f'<tr><th scope="row">{esc(label)}</th><td><pre class="catalog-value">{esc(value)}</pre></td></tr>'
                          for label, value in summary_builder(entry))
        records.append(f'''<article class="catalog-entry panel"><header><div><h3>{esc(name)}</h3>
<p>{esc(title)}{time_label}</p></div><span class="badge">{esc(status)}</span></header>
<div class="table-wrap"><table><tbody>{details}</tbody></table></div></article>''')
    return ''.join(records)

def maintenance_catalog(user, token):
    migration_catalog, migration_error = load_catalog_document(
        "migration-catalog.json", "migrations")
    deployment_catalog, deployment_error = load_catalog_document(
        "deployment-catalog.json", "deployments")
    migrations = migration_catalog.get("migrations", []) if migration_catalog else []
    deployments = deployment_catalog.get("deployments", []) if deployment_catalog else []
    groups = {cloud: {"migrations": [], "deployments": []} for cloud in CLOUDS}
    for kind, entries in (("migrations", migrations), ("deployments", deployments)):
        for entry in entries:
            provider = str(entry.get("target_platform") or "unknown").lower()
            groups.setdefault(provider, {"migrations": [], "deployments": []})[kind].append(entry)

    errors = [migration_error, deployment_error]
    error_html = ''.join(f'<div class="alert">Could not read catalog: {esc(error)}</div>'
                         for error in errors if error)
    generated = []
    if migration_catalog and migration_catalog.get("generated_at"):
        generated.append(f'Migration catalog updated {esc(migration_catalog["generated_at"])}')
    if deployment_catalog and deployment_catalog.get("generated_at"):
        generated.append(f'Launch catalog updated {esc(deployment_catalog["generated_at"])}')
    metadata = '<p class="catalog-meta">' + ' · '.join(generated) + '</p>' if generated else ''

    ordered = list(CLOUDS) + sorted(provider for provider in groups if provider not in CLOUDS)
    sections = []
    for provider in ordered:
        entries = groups.get(provider, {"migrations": [], "deployments": []})
        if not entries["migrations"] and not entries["deployments"]:
            continue
        label = cloud_name(provider) if provider in CLOUDS else provider.title()
        migration_count = len(entries["migrations"])
        deployment_count = len(entries["deployments"])
        sections.append(f'''<section class="provider-section"><div class="provider-heading"><h2>{esc(label)}</h2>
<span>{migration_count} migrations · {deployment_count} launches</span></div>
<h3 class="catalog-kind">Migrations</h3>{catalog_records('Migration', entries['migrations'], migration_summary_fields)}
<h3 class="catalog-kind">Launches</h3>{catalog_records('Launch', entries['deployments'], deployment_summary_fields)}</section>''')
    if not sections and not error_html:
        sections.append('<div class="panel empty">The catalogs contain no records yet.</div>')

    body = f'''<div class="page-heading no-print"><div><div class="eyebrow">MAINTENANCE / CATALOG</div><h1>Catalog</h1>
<p>Migration and launch records grouped by cloud provider. Use your browser’s Print command to print or save as PDF.</p></div>
<div class="catalog-actions"><a class="back" href="/maintenance/catalog/raw">View raw JSON</a><a class="back" href="/maintenance">← Maintenance</a></div></div>
<div class="catalog-meta no-print">{metadata}</div>
<div class="print-heading"><h1>Migration and launch catalog</h1>{metadata}</div>
{error_html}{''.join(sections)}'''
    return page("Print catalog", body, user, token)

def maintenance_catalog_raw(user, token):
    catalogs = (
        ("migration-catalog.json", "migrations"),
        ("deployment-catalog.json", "deployments"),
    )
    sections = []
    errors = []
    for filename, list_key in catalogs:
        catalog, error = load_catalog_document(filename, list_key)
        if error:
            errors.append(f'<div class="alert">Could not read catalog: {esc(error)}</div>')
        elif catalog is None:
            sections.append(f'<section class="raw-catalog"><h2>{esc(filename)}</h2><p class="catalog-empty">This catalog does not exist yet.</p></section>')
        else:
            raw = json.dumps(catalog, ensure_ascii=False, indent=2)
            sections.append(f'<section class="raw-catalog"><h2>{esc(filename)}</h2><pre class="raw-catalog-json">{esc(raw)}</pre></section>')
    body = f'''<div class="page-heading no-print"><div><div class="eyebrow">MAINTENANCE / CATALOG</div><h1>Raw catalog JSON</h1>
<p>Complete catalog files as stored on this controller.</p></div>
<div class="catalog-actions"><a class="back" href="/maintenance/catalog">Summary view</a><a class="back" href="/maintenance">← Maintenance</a></div></div>
<div class="print-heading"><h1>Raw migration and launch catalogs</h1></div>
{''.join(errors)}{''.join(sections)}'''
    return page("Raw catalog JSON", body, user, token)

def clear_finished_jobs():
    statuses = TERMINAL_JOB_STATUSES
    placeholders = ",".join("?" for _ in statuses)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        resolved_runs = resolved_discard_runs(conn)
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders})", statuses
        ).fetchall()
        clear_rows = [row for row in rows
                      if not any(recovery_actions(row, resolved_runs).values())]
        clear_ids = [row["id"] for row in clear_rows]
        if clear_ids:
            clear_placeholders = ",".join("?" for _ in clear_ids)
            conn.execute(f"DELETE FROM jobs WHERE id IN ({clear_placeholders})", clear_ids)

    log_errors = 0
    for row in clear_rows:
        log_path = STATE / f"job-{row['id']}.log"
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            log_errors += 1
    return len(clear_rows), log_errors

def maintenance_jobs(user, token, cleared=None, log_errors=0, error=""):
    counts = job_status_counts()
    finished = sum(counts.get(status, 0) for status in TERMINAL_JOB_STATUSES)
    active = counts.get("queued", 0) + counts.get("running", 0)
    with db() as conn:
        failed_rows = conn.execute(
            "SELECT * FROM jobs WHERE mode='migrate' AND status IN ('failed','interrupted')"
        ).fetchall()
        resolved_runs = resolved_discard_runs(conn)
    preserved = sum(any(recovery_actions(row, resolved_runs).values()) for row in failed_rows)
    clearable = finished - preserved
    notice = ''
    if cleared is not None and cleared > 0:
        notice += f'<div class="notice">Removed {cleared} finished job{"s" if cleared != 1 else ""}.</div>'
    elif cleared == 0:
        notice += '<div class="notice">There were no clearable finished jobs to remove. Runs with pending recovery actions remain available.</div>'
    if log_errors:
        notice += f'<div class="alert">{log_errors} job output log{ "s" if log_errors != 1 else ""} could not be deleted.</div>'
    if error:
        notice += f'<div class="alert">{esc(error)}</div>'
    status_rows = ''.join(
        f'<tr><th>{esc(status.title())}</th><td>{counts.get(status, 0)}</td></tr>'
        for status in ("queued", "running", *TERMINAL_JOB_STATUSES)
    )
    disabled = " disabled" if clearable == 0 else ""
    body = f'''<div class="page-heading"><div><div class="eyebrow">MAINTENANCE / JOB QUEUE</div><h1>Manage job queue</h1>
<p>Remove clearable finished job records and output logs. Queued, running, and migration jobs with pending recovery actions are retained.</p></div>
<a class="back" href="/maintenance">← Maintenance</a></div>
{notice}<div class="maintenance-grid queue-grid"><section class="panel queue-summary"><h2>Current jobs</h2>
<div class="table-wrap"><table><tbody>{status_rows}</tbody></table></div>
<p>{active} queued or running; {finished} finished; {preserved} retained for migration recovery.</p></section>
<section class="panel queue-clear"><h2>Clear finished jobs</h2>
<p>This removes {clearable} completed or nonrecoverable job records and their saved output logs. Runs with resume or discard actions stay available.</p>
<form method="post" action="/maintenance/jobs/clear">{csrf_input(token)}
<label class="confirm-row"><input type="checkbox" name="confirm" value="yes" required> I understand this removes clearable finished job history.</label>
<button class="button" type="submit"{disabled}>Clear {clearable} finished job{ "s" if clearable != 1 else ""}</button></form></section></div>'''
    return page("Manage job queue", body, user, token)

def add_user(name):
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{2,31}", name):
        raise SystemExit("Username must be 3–32 letters, digits, dots, underscores or hyphens.")
    password = getpass.getpass("Password: ")
    if (not 12 <= len(password) <= MAX_LOGIN_PASSWORD
            or password != getpass.getpass("Confirm password: ")):
        raise SystemExit(f"Passwords must match and contain 12–{MAX_LOGIN_PASSWORD} characters.")
    entries = users()
    entries[name] = password_hash(password)
    save_users(entries)
    print(f"Saved local account {name}.")

def bootstrap_user():
    """Create the initial web account from caddy.yml, without replacing users."""
    import fcntl

    lock_path = STATE / "user-bootstrap.lock"
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = users()
        if entries:
            print("A local web account already exists; bootstrap skipped.")
            return

        vault = VAULT_DIR / "caddy.yml"
        if not vault.is_file():
            raise SystemExit(f"Initial web account vault is missing: {vault}")
        if not PASSFILE.is_file():
            raise SystemExit(f"Vault password file is missing: {PASSFILE}")
        try:
            import yaml
        except ImportError:
            raise SystemExit("PyYAML is required to read the initial web account vault.")
        try:
            result = subprocess.run(
                [ansible_vault_bin(), "view", str(vault), "--vault-password-file", str(PASSFILE)],
                input=b"", capture_output=True, timeout=30, check=True)
            data = yaml.safe_load(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError, yaml.YAMLError):
            raise SystemExit("Could not decrypt or parse the initial web account vault.")

        if not isinstance(data, dict):
            raise SystemExit("Initial web account vault must contain a YAML mapping.")
        name = data.get("web_user")
        password = data.get("web_password")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{2,31}", name):
            raise SystemExit("caddy.yml web_user must be a valid 3–32 character username.")
        if not isinstance(password, str) or not 12 <= len(password) <= MAX_LOGIN_PASSWORD:
            raise SystemExit(
                f"caddy.yml web_password must contain 12–{MAX_LOGIN_PASSWORD} characters.")
        save_users({name: password_hash(password)})
        print(f"Created initial local web account {name} from caddy.yml.")

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    user_cmd = sub.add_parser("user")
    user_cmd.add_argument("action", choices=("add", "bootstrap", "delete", "list"))
    user_cmd.add_argument("name", nargs="?")
    args = parser.parse_args()
    init_state()
    if args.command == "user":
        if args.action == "list":
            print("\n".join(sorted(users())))
        elif args.action == "bootstrap":
            if args.name:
                parser.error("user bootstrap does not take a name")
            bootstrap_user()
        elif not args.name:
            parser.error("user name required")
        elif args.action == "add":
            add_user(args.name)
        else:
            entries = users()
            entries.pop(args.name, None)
            save_users(entries)
        return
    if not users():
        raise SystemExit("Create an account first: python3 web/app.py user add OPERATOR")
    if not PASSFILE.is_file():
        raise SystemExit(f"Vault password file is missing: {PASSFILE}")
    import fcntl
    lock = open(STATE / "server.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another web controller is already running for this state directory")
    # A restart must not silently re-run a job that may have changed cloud state.
    with db() as conn:
        conn.execute("UPDATE jobs SET status='interrupted', finished=datetime('now') WHERE status='running'")
    server = BoundedThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Listening on {args.host}:{args.port}", flush=True)
    def handle_shutdown(signum, _frame):
        STOP_EVENT.set()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle_shutdown)
    try:
        threading.Thread(target=worker, daemon=True).start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP_EVENT.set()
        stop_active_process()
        server.server_close()

if __name__ == "__main__":
    main()
