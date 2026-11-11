#!/usr/bin/env python3
# bin/install-openvddk.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Download and install the OpenVDDK user kit for nbdkit's VDDK plugin.

The pinned archive is downloaded from the upstream GitHub release (or copied
from OPENVDDK_ARCHIVE), checked against the published SHA-256, and extracted
outside the repository. The complete kit, including its notices, is kept at
OPENVDDK_INSTALL_DIR so a host and a container can share it through
/work/migrate-v2v.
"""

import argparse
import hashlib
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

VERSION = "0.7.4"
COMMIT = "94926da2f764"
FILENAME = f"openvddk-evalkit-{VERSION}-{COMMIT}.tar.gz"
DOWNLOAD_URL = (
    "https://github.com/openvddk/openvddk/releases/download/"
    f"v{VERSION}/{FILENAME}"
)
ARCHIVE_SHA256 = (
    "4f50188fcfe9dc8c0e523a05359bee16c12f990f812332548f5b00a4c2252687"
)
LIBRARY_SHA256 = (
    "d39bf157aae96283c8f0ccfadc26b9aec104a5ef9708c4d15cc6945db1dc7daf"
)
DEFAULT_INSTALL_DIR = Path("/work/migrate-v2v/tools/openvddk")
USER_AGENT = "migrate-v2v OpenVDDK installer"

def install_dir(argument):
    value = argument or os.environ.get("OPENVDDK_INSTALL_DIR")
    return Path(value or DEFAULT_INSTALL_DIR).expanduser().resolve()

def acquire_archive(destination):
    supplied = os.environ.get("OPENVDDK_ARCHIVE")
    if supplied:
        source = Path(supplied).expanduser()
        if not source.is_file():
            raise SystemExit(f"OPENVDDK_ARCHIVE is not a file: {source}")
        shutil.copyfile(source, destination)
        return

    request = urllib.request.Request(
        DOWNLOAD_URL, headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output)
    except (OSError, urllib.error.URLError) as error:
        raise SystemExit(f"Could not download OpenVDDK from GitHub: {error}") from error

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def verify_archive(path):
    actual = sha256(path)
    if actual != ARCHIVE_SHA256:
        raise SystemExit(
            f"OpenVDDK archive checksum mismatch for {path}; "
            f"expected SHA-256 {ARCHIVE_SHA256}, got {actual}."
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

def locate_kit(extracted):
    candidates = [
        path for path in extracted.iterdir()
        if path.is_dir()
        and (path / "lib" / "libvixDiskLib.so.8.0.3").is_file()
    ]
    if len(candidates) != 1:
        raise SystemExit("OpenVDDK archive did not contain one recognizable user kit.")
    return candidates[0]

def install_archive(archive, target_dir, temporary):
    extracted = temporary / "extracted"
    extracted.mkdir()
    safe_extract(archive, extracted)
    kit_dir = locate_kit(extracted)

    library = kit_dir / "lib" / "libvixDiskLib.so.8.0.3"
    actual_library_sha256 = sha256(library)
    if actual_library_sha256 != LIBRARY_SHA256:
        raise SystemExit(
            "OpenVDDK library checksum mismatch; "
            f"expected {LIBRARY_SHA256}, got {actual_library_sha256}."
        )

    staged_dir = temporary / "install"
    shutil.copytree(kit_dir, staged_dir, symlinks=True)
    lib64 = staged_dir / "lib64"
    lib64.mkdir()
    (lib64 / "libvixDiskLib.so.8").symlink_to("../lib/libvixDiskLib.so.8")

    existing_library = target_dir / "lib" / "libvixDiskLib.so.8.0.3"
    if existing_library.is_file() and sha256(existing_library) == LIBRARY_SHA256:
        print(f"OpenVDDK {VERSION} is already installed at {target_dir}")
        return
    if target_dir.exists() or target_dir.is_symlink():
        if (
            target_dir.is_dir()
            and not target_dir.is_symlink()
            and not any(target_dir.iterdir())
        ):
            target_dir.rmdir()
        else:
            raise SystemExit(
                f"Refusing to replace the existing VDDK directory {target_dir}; "
                "choose another OPENVDDK_INSTALL_DIR."
            )

    os.replace(staged_dir, target_dir)
    print(f"Installed OpenVDDK {VERSION} at {target_dir}")

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
            "The pinned OpenVDDK kit supports Linux x86-64; "
            f"this host is {sys.platform}/{platform.machine()}."
        )

    target_dir = install_dir(args.install_dir)
    existing_library = target_dir / "lib" / "libvixDiskLib.so.8.0.3"
    if existing_library.is_file() and sha256(existing_library) == LIBRARY_SHA256:
        print(f"OpenVDDK {VERSION} is already installed at {target_dir}")
        return 0

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".openvddk-install-", dir=target_dir.parent
    ) as temporary_name:
        temporary = Path(temporary_name)
        archive = temporary / FILENAME
        print(f"Downloading OpenVDDK {VERSION} from GitHub...")
        acquire_archive(archive)
        verify_archive(archive)
        install_archive(archive, target_dir, temporary)
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, tarfile.TarError, urllib.error.URLError) as error:
        raise SystemExit(f"OpenVDDK installation failed: {error}") from error
