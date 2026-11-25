#!/usr/bin/env python3
# bin/install-openvddk.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Download and install the latest OpenVDDK runtime kit for nbdkit's VDDK plugin.

The fixed-name archive is downloaded from the upstream GitHub release (or
copied from OPENVDDK_ARCHIVE), checked against that release's SHA256SUMS file,
and extracted outside the repository. The complete kit, including its notices,
is kept at OPENVDDK_INSTALL_DIR so a host and a container can share it through
/work/migrate-v2v.
"""

import argparse
import hashlib
import http.client
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

FILENAME = "openvddk-runtime.tar.gz"
RELEASE_URL = "https://github.com/openvddk/openvddk/releases/latest/download/"
DOWNLOAD_URL = RELEASE_URL + FILENAME
CHECKSUMS_URL = RELEASE_URL + "SHA256SUMS"
DEFAULT_INSTALL_DIR = Path("/work/migrate-v2v/tools/openvddk")
INSTALL_MARKER = ".migrate-v2v-openvddk-install"
USER_AGENT = "migrate-v2v OpenVDDK installer"

def install_dir(argument):
    value = argument or os.environ.get("OPENVDDK_INSTALL_DIR")
    return Path(value or DEFAULT_INSTALL_DIR).expanduser().resolve()

def download(url, destination, description):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output)
    except (OSError, http.client.HTTPException, urllib.error.URLError) as error:
        raise SystemExit(
            f"Could not download OpenVDDK {description} from GitHub: {error}"
        ) from error

def acquire_archive(destination):
    supplied = os.environ.get("OPENVDDK_ARCHIVE")
    if supplied:
        source = Path(supplied).expanduser()
        if not source.is_file():
            raise SystemExit(f"OPENVDDK_ARCHIVE is not a file: {source}")
        shutil.copyfile(source, destination)
    else:
        download(DOWNLOAD_URL, destination, "runtime kit")

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def checksum_from_manifest(path):
    matches = []
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except UnicodeError as error:
        raise SystemExit(
            f"OpenVDDK checksum manifest is not ASCII: {path}"
        ) from error
    for line in lines:
        fields = line.split(maxsplit=1)
        if len(fields) != 2 or fields[1].lstrip("*") != FILENAME:
            continue
        digest = fields[0].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SystemExit(f"Invalid SHA-256 for {FILENAME} in {path}.")
        matches.append(digest)
    if len(matches) != 1:
        raise SystemExit(
            f"Expected one SHA-256 entry for {FILENAME} in {path}, "
            f"found {len(matches)}."
        )
    return matches[0]

def verify_archive(path, expected):
    actual = sha256(path)
    if actual != expected:
        raise SystemExit(
            f"OpenVDDK archive checksum mismatch for {path}; "
            f"expected SHA-256 {expected}, got {actual}."
        )

def safe_extract(archive, destination):
    root = destination.resolve()
    directories = []
    symlinks = []
    files = []

    with tarfile.open(archive, "r:gz") as package:
        for item in package.getmembers():
            name = PurePosixPath(item.name)
            if (
                name.is_absolute()
                or ".." in name.parts
                or "\\" in item.name
            ):
                raise SystemExit(f"Unsafe path in OpenVDDK archive: {item.name!r}")
            target = root.joinpath(*name.parts)
            resolved = target.resolve()
            if resolved != root and root not in resolved.parents:
                raise SystemExit(f"Unsafe path in OpenVDDK archive: {item.name!r}")

            if item.isdir():
                directories.append((item, target))
            elif item.isfile():
                files.append((item, target))
            elif item.issym():
                link = PurePosixPath(item.linkname)
                if (
                    link.is_absolute()
                    or ".." in link.parts
                    or "\\" in item.linkname
                ):
                    raise SystemExit(
                        f"Unsafe symlink in OpenVDDK archive: {item.name!r} "
                        f"-> {item.linkname!r}"
                    )
                symlinks.append((item, target))
            else:
                raise SystemExit(
                    f"Unsupported file type in OpenVDDK archive: {item.name!r}"
                )

        for _, target in directories:
            target.mkdir(parents=True, exist_ok=True)

        for item, target in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            source = package.extractfile(item)
            if source is None:
                raise SystemExit(f"Could not read OpenVDDK archive member: {item.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            os.chmod(target, (item.mode & 0o777) or 0o644)

        for item, target in symlinks:
            resolved_link = (target.parent / item.linkname).resolve()
            if resolved_link != root and root not in resolved_link.parents:
                raise SystemExit(
                    f"Unsafe symlink in OpenVDDK archive: {item.name!r} "
                    f"-> {item.linkname!r}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(item.linkname, target)

        for item, target in directories:
            os.chmod(target, (item.mode & 0o777) or 0o755)

def library_files(kit_dir):
    lib_dir = kit_dir / "lib"
    if not lib_dir.is_dir():
        return []
    return [
        path for path in lib_dir.iterdir()
        if re.fullmatch(r"libvixDiskLib\.so\.\d+(?:\.\d+)*", path.name)
        and path.is_file()
        and not path.is_symlink()
    ]

def locate_kit(extracted):
    candidates = [
        path for path in extracted.iterdir()
        if path.is_dir()
        and len(library_files(path)) == 1
    ]
    if len(candidates) != 1:
        raise SystemExit("OpenVDDK archive did not contain one recognizable user kit.")
    return candidates[0]

def installed_kit(target_dir):
    marker = target_dir / INSTALL_MARKER
    build_info = target_dir / "BUILD.txt"
    is_openvddk = marker.is_file()
    if not is_openvddk and build_info.is_file():
        try:
            is_openvddk = build_info.read_text(encoding="utf-8").startswith(
                "OpenVDDK runtime kit "
            )
        except UnicodeError:
            pass
    return (
        is_openvddk
        and len(library_files(target_dir)) == 1
        and (target_dir / "lib64" / "libvixDiskLib.so.8").is_file()
    )

def install_archive(archive, target_dir, temporary):
    extracted = temporary / "extracted"
    extracted.mkdir()
    safe_extract(archive, extracted)
    kit_dir = locate_kit(extracted)

    staged_dir = temporary / "install"
    shutil.copytree(kit_dir, staged_dir, symlinks=True)
    lib64 = staged_dir / "lib64"
    if lib64.is_symlink() and os.readlink(lib64) == "lib":
        # Kits from 0.7.5 on ship lib64 -> lib themselves.
        if not (lib64 / "libvixDiskLib.so.8").is_file():
            raise SystemExit("OpenVDDK archive's lib64 link does not reach libvixDiskLib.so.8.")
    else:
        lib64.mkdir(exist_ok=True)
        alias = lib64 / "libvixDiskLib.so.8"
        if alias.is_symlink() and os.readlink(alias) == "../lib/libvixDiskLib.so.8":
            pass
        elif alias.exists() or alias.is_symlink():
            raise SystemExit(f"Unexpected OpenVDDK compatibility path in archive: {alias}")
        else:
            alias.symlink_to("../lib/libvixDiskLib.so.8")

    (staged_dir / INSTALL_MARKER).write_text(
        "Installed by migrate-v2v from the OpenVDDK latest runtime asset.\n",
        encoding="utf-8",
    )
    os.replace(staged_dir, target_dir)
    print(f"Installed the latest OpenVDDK runtime kit at {target_dir}")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install-dir",
        help="installation directory (default: OPENVDDK_INSTALL_DIR or "
             f"{DEFAULT_INSTALL_DIR})",
    )
    args = parser.parse_args()

    if not sys.platform.startswith("linux") or platform.machine().lower() not in {
        "x86_64", "amd64"
    }:
        raise SystemExit(
            "The OpenVDDK runtime kit supports Linux x86-64; "
            f"this host is {sys.platform}/{platform.machine()}."
        )

    target_dir = install_dir(args.install_dir)
    if (
        target_dir.is_dir()
        and not target_dir.is_symlink()
        and installed_kit(target_dir)
    ):
        print(f"An OpenVDDK runtime kit is already installed at {target_dir}")
        return 0

    if target_dir.exists() or target_dir.is_symlink():
        if (target_dir.is_dir() and not target_dir.is_symlink()
                and not any(target_dir.iterdir())):
            target_dir.rmdir()
        else:
            raise SystemExit(
                f"Refusing to replace the existing VDDK directory {target_dir}; "
                "choose another OPENVDDK_INSTALL_DIR."
            )

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".openvddk-install-", dir=target_dir.parent
    ) as temporary_name:
        temporary = Path(temporary_name)
        checksums = temporary / "SHA256SUMS"
        archive = temporary / FILENAME
        print("Downloading the latest OpenVDDK checksum manifest from GitHub...")
        download(CHECKSUMS_URL, checksums, "checksum manifest")
        expected_sha256 = checksum_from_manifest(checksums)
        print("Downloading the latest OpenVDDK runtime kit from GitHub...")
        acquire_archive(archive)
        verify_archive(archive, expected_sha256)
        install_archive(archive, target_dir, temporary)
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, http.client.HTTPException, tarfile.TarError, urllib.error.URLError) as error:
        raise SystemExit(f"OpenVDDK installation failed: {error}") from error
