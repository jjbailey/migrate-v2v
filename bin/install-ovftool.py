#!/usr/bin/env python3
# bin/install-ovftool.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Download and install the tested Linux OVF Tool from Broadcom.

The ZIP is downloaded to a temporary directory, checked against the MD5
published on Broadcom's download page, extracted outside the repository, and
then discarded. Broadcom's download page and license terms are at PAGE_URL.
"""

import hashlib
import http.cookiejar
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

VERSION = "5.1.0"
PAGE_URL = (
    "https://developer.broadcom.com/tools/"
    "open-virtualization-format-ovf-tool/5.1.0"
)
FILENAME = "VMware-ovftool-5.1.0-25410048-lin.x86_64.zip"
MD5 = "ea1eb22fede461274eaff55a46318b63"
ARTIFACT_ID = 29848
PORTLET_ID = "SDK_AND_TOOL_DETAILS_INSTANCE_iwlk"
DEFAULT_INSTALL_DIR = Path(
    "/work/migrate-v2v/tools/ovftool-5.1.0"
)
USER_AGENT = "migrate-v2v OVF Tool installer"

def install_dir():
    return Path(
        os.environ.get("OVFTOOL_INSTALL_DIR") or DEFAULT_INSTALL_DIR
    ).expanduser().resolve()

def require_license_acceptance():
    accepted = os.environ.get("OVFTOOL_ACCEPT_LICENSE", "").strip().lower()
    if accepted in {"1", "yes", "true", "accepted"}:
        return

    if not sys.stdin.isatty():
        raise SystemExit(
            "Broadcom's OVF Tool license requires explicit acceptance. "
            f"Review {PAGE_URL}, then rerun interactively or set "
            "OVFTOOL_ACCEPT_LICENSE=1 for this invocation."
        )

    print("Broadcom's OVF Tool download and license terms:")
    print(f"  {PAGE_URL}")
    print("Only continue if you are authorized to accept those terms.")
    try:
        answer = input("Type ACCEPT to download and install OVF Tool: ")
    except EOFError as error:
        raise SystemExit("OVF Tool installation cancelled.") from error
    if answer.strip() != "ACCEPT":
        raise SystemExit("OVF Tool installation cancelled.")

def official_download_url():
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookies)
    )
    request = urllib.request.Request(
        PAGE_URL, headers={"User-Agent": USER_AGENT}
    )
    try:
        with opener.open(request, timeout=30) as response:
            page = response.read().decode("utf-8", "replace")
    except (OSError, urllib.error.URLError) as error:
        raise SystemExit(f"Could not open Broadcom's OVF Tool page: {error}") from error

    if FILENAME not in page or MD5 not in page.lower():
        raise SystemExit(
            "Broadcom's OVF Tool page no longer matches the pinned 5.1.0 "
            "Linux ZIP. Update this installer after validating the new release, "
            "or set OVFTOOL_ARCHIVE to a downloaded 5.1.0 ZIP."
        )

    token_match = re.search(
        r"(?:Liferay\.authToken\s*=\s*|authToken\s*[:=]\s*)"
        r"[\"']([^\"']+)[\"']",
        page,
    )
    if not token_match:
        raise SystemExit(
            "Could not get Broadcom's download-page token. Download the Linux "
            f"ZIP from {PAGE_URL} and set OVFTOOL_ARCHIVE to its path."
        )

    endpoint = PAGE_URL + "?" + urllib.parse.urlencode({
        "p_p_id": PORTLET_ID,
        "p_p_lifecycle": "2",
        "p_p_state": "normal",
        "p_p_mode": "view",
        "p_p_resource_id": "documentDownloadArtifact",
        "p_p_cacheability": "cacheLevelPage",
    })
    payload = json.dumps({
        "fileName": FILENAME,
        "artifactId": ARTIFACT_ID,
        "fileType": "Download",
        "source": "",
    }).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "X-CSRF-Token": token_match.group(1),
            "Referer": PAGE_URL,
        },
    )
    try:
        with opener.open(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError) as error:
        raise SystemExit(
            "Broadcom's download endpoint failed. If sign-in is required, "
            f"download the Linux ZIP from {PAGE_URL} and set "
            f"OVFTOOL_ARCHIVE to its path. Details: {error}"
        ) from error

    download_url = (result.get("data") or {}).get("downloadUrl")
    if not result.get("success") or not isinstance(download_url, str):
        raise SystemExit(
            "Broadcom did not return a download URL. If sign-in is required, "
            f"download the Linux ZIP from {PAGE_URL} and set "
            "OVFTOOL_ARCHIVE to its path."
        )

    parsed = urllib.parse.urlparse(download_url)
    if parsed.scheme != "https" or not (
        parsed.hostname == "broadcom.com"
        or (parsed.hostname or "").endswith(".broadcom.com")
    ):
        raise SystemExit("Broadcom returned an unexpected download host.")
    return download_url

def acquire_archive(destination):
    supplied = os.environ.get("OVFTOOL_ARCHIVE")
    if supplied:
        source = Path(supplied).expanduser()
        if not source.is_file():
            raise SystemExit(f"OVFTOOL_ARCHIVE is not a file: {source}")
        shutil.copyfile(source, destination)
        return

    download_url = official_download_url()
    request = urllib.request.Request(
        download_url, headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output)
    except (OSError, urllib.error.URLError) as error:
        raise SystemExit(f"Could not download OVF Tool from Broadcom: {error}") from error

def verify_archive(path):
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as archive:
        for block in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest().lower() != MD5:
        raise SystemExit(
            f"OVF Tool ZIP checksum mismatch for {path}; expected Broadcom's "
            f"published MD5 {MD5}."
        )

def safe_extract(archive, destination):
    root = destination.resolve()
    directories = []
    symlinks = []
    files = []

    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            name = PurePosixPath(item.filename)
            if name.is_absolute() or ".." in name.parts or "\\" in item.filename:
                raise SystemExit(f"Unsafe path in OVF Tool ZIP: {item.filename!r}")
            target = root.joinpath(*name.parts)
            resolved = target.resolve()
            if resolved != root and root not in resolved.parents:
                raise SystemExit(f"Unsafe path in OVF Tool ZIP: {item.filename!r}")

            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode):
                symlinks.append((item, target))
            elif item.is_dir():
                directories.append((item, target))
            else:
                files.append((item, target))

        for _, target in directories:
            target.mkdir(parents=True, exist_ok=True)

        for item, target in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(item) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            mode = (item.external_attr >> 16) & 0o777
            os.chmod(target, mode or 0o644)

        for item, target in symlinks:
            link_target = package.read(item).decode("utf-8")
            if os.path.isabs(link_target):
                raise SystemExit(f"Unsafe absolute symlink in OVF Tool ZIP: {item.filename!r}")
            resolved_link = (target.parent / link_target).resolve()
            if resolved_link != root and root not in resolved_link.parents:
                raise SystemExit(f"Unsafe symlink in OVF Tool ZIP: {item.filename!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(link_target, target)

        for item, target in directories:
            mode = (item.external_attr >> 16) & 0o777
            os.chmod(target, mode or 0o755)

def install_archive(archive, target_dir, temp_root):
    extracted = temp_root / "extracted"
    extracted.mkdir()
    safe_extract(archive, extracted)

    candidates = sorted(
        (path for path in extracted.rglob("ovftool")
         if path.is_file()),
        key=lambda path: len(path.parts),
    )
    if not candidates:
        raise SystemExit("OVF Tool ZIP did not contain an ovftool executable.")

    package_dir = candidates[0].parent
    executable = package_dir / "ovftool"
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    staged_dir = temp_root / "install"
    shutil.copytree(package_dir, staged_dir, symlinks=True)
    staged_executable = staged_dir / "ovftool"
    result = subprocess.run(
        [str(staged_executable), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise SystemExit(
            "The downloaded OVF Tool could not run on this host:\n"
            + result.stdout.strip()
        )
    print(result.stdout.strip())

    backup = temp_root / "previous-install"
    had_previous = target_dir.exists() or target_dir.is_symlink()
    if had_previous:
        os.replace(target_dir, backup)
    try:
        os.replace(staged_dir, target_dir)
    except OSError:
        if had_previous and (backup.exists() or backup.is_symlink()):
            os.replace(backup, target_dir)
        raise
    if backup.is_symlink() or backup.is_file():
        backup.unlink()
    elif backup.exists():
        shutil.rmtree(backup)

def main():
    if not sys.platform.startswith("linux") or platform.machine().lower() not in {
        "x86_64", "amd64"
    }:
        raise SystemExit(
            "The pinned Broadcom package is for Linux x86_64; "
            f"this host is {sys.platform}/{platform.machine()}."
        )

    target_dir = install_dir()
    executable = target_dir / "ovftool"
    if executable.is_file() and os.access(executable, os.X_OK):
        print(f"OVF Tool {VERSION} is already installed at {executable}")
        return 0

    require_license_acceptance()
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".ovftool-install-", dir=target_dir.parent
    ) as temporary:
        temp_root = Path(temporary)
        archive = temp_root / FILENAME
        print(f"Downloading OVF Tool {VERSION} from Broadcom...")
        acquire_archive(archive)
        verify_archive(archive)
        install_archive(archive, target_dir, temp_root)
    print(f"Installed OVF Tool {VERSION} at {executable}")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, zipfile.BadZipFile, urllib.error.URLError, subprocess.TimeoutExpired) as error:
        raise SystemExit(f"OVF Tool installation failed: {error}") from error
