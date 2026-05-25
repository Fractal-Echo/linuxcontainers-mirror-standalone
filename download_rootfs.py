#!/usr/bin/env python3
"""
Downloads rootfs.tar.xz from https://images.linuxcontainers.org/images/
into ./tarballs/ and generates rootfs.json alongside.

Fails immediately on any error (network, sha256 mismatch, disk full, etc.)
"""

import asyncio
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, unquote

import aiohttp
from bs4 import BeautifulSoup

BASE_URL    = "https://images.linuxcontainers.org/images/"
OUT_DIR     = Path("tarballs")
JSON_OUT    = Path("rootfs.json")
CONCURRENCY = 4

DISTROS = {
    "almalinux", "alpine", "alt", "amazonlinux", "archlinux",
    "busybox", "centos", "debian", "devuan", "fedora",
    "gentoo", "kali", "mint", "nixos", "openeuler",
    "opensuse", "openwrt", "oracle", "plamo", "rockylinux",
    "slackware", "ubuntu", "voidlinux",
}
ARCH_MAP = {
    "amd64": "x86_64",
    "arm64": "aarch64",
    "armhf": "armhf",
}
SKIP_FLAVORS = {"cloud"}
TARGET_FILE  = "rootfs.tar.xz"
DATED_RE     = re.compile(r"\d{8}_\d{2}(?:%3A|:)\d{2}/?$")

# Human-friendly distro display names
DISTRO_NAMES = {
    "almalinux":  "AlmaLinux",
    "alpine":     "Alpine Linux",
    "alt":        "ALT Linux",
    "amazonlinux":"Amazon Linux",
    "archlinux":  "Arch Linux",
    "busybox":    "BusyBox",
    "centos":     "CentOS",
    "debian":     "Debian GNU/Linux",
    "devuan":     "Devuan",
    "fedora":     "Fedora",
    "gentoo":     "Gentoo",
    "kali":       "Kali Linux",
    "mint":       "Linux Mint",
    "nixos":      "NixOS",
    "openeuler":  "openEuler",
    "opensuse":   "openSUSE",
    "openwrt":    "OpenWrt",
    "oracle":     "Oracle Linux",
    "plamo":      "Plamo Linux",
    "rockylinux": "Rocky Linux",
    "slackware":  "Slackware",
    "ubuntu":     "Ubuntu",
    "voidlinux":  "Void Linux",
}


@dataclass
class RootfsEntry:
    url:        str
    filename:   str           # final renamed tarball
    distro:     str           # e.g. alpine
    version:    str           # e.g. 3.21
    arch:       str           # mapped: aarch64/x86_64/armhf
    flavor:     str           # default/openrc/systemd/musl/etc.
    snapshot:   str           # e.g. 20260525_13:01
    sha256:     str | None
    size_bytes: int = field(default=0)


# helpers

async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as r:
        r.raise_for_status()
        return await r.text()


def parse_links(html: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    out  = []
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if h.startswith("?") or h == "../" or h.startswith("http"):
            continue
        out.append(urljoin(base, h))
    return out


def latest_dated(links: list[str]) -> str | None:
    dated = [l for l in links if DATED_RE.search(l)]
    return sorted(dated)[-1] if dated else None


def dir_name(url: str) -> str:
    return unquote(url.rstrip("/").split("/")[-1])


async def parse_sha256sums(session, snapshot_url: str) -> dict[str, str]:
    try:
        text = await fetch_text(session, snapshot_url + "SHA256SUMS")
        out  = {}
        for line in text.strip().splitlines():
            parts = line.split()
            if len(parts) == 2:
                out[parts[1].lstrip("*")] = parts[0]
        return out
    except Exception:
        return {}


def make_name(distro: str, version: str, flavor: str) -> str:
    """Human display name: e.g. 'Alpine Linux 3.23 (default)'"""
    base = f"{DISTRO_NAMES.get(distro, distro.title())} {version}"
    return base if flavor == "default" else f"{base} ({flavor})"


def make_description(distro: str, version: str, flavor: str) -> str:
    name = make_name(distro, version, flavor)
    return f"Official LXC container image for {name}"


def entry_to_json(e: RootfsEntry) -> dict:
    build_date = e.snapshot.split("_")[0]  # 20260525_13:01 -> 20260525
    return {
        "name":         make_name(e.distro, e.version, e.flavor),
        "distro":       DISTRO_NAMES.get(e.distro, e.distro.title()),
        "description":  make_description(e.distro, e.version, e.flavor),
        "architecture": e.arch,
        "file":         e.filename,
        "download_url": "",
        "sha256":       e.sha256 or "",
        "size_bytes":   e.size_bytes,
        "build_date":   build_date,
        "author":       "linuxcontainers",
    }


# crawl

async def crawl_flavor(session, url, distro, version, arch, flavor, results):
    html   = await fetch_text(session, url)
    links  = parse_links(html, url)
    latest = latest_dated(links)
    if not latest:
        return

    snapshot       = dir_name(latest)
    snapshot_links = parse_links(await fetch_text(session, latest), latest)
    rootfs_url     = next((l for l in snapshot_links if l.endswith(TARGET_FILE)), None)
    if not rootfs_url:
        return

    sha256s  = await parse_sha256sums(session, latest)
    # colons are invalid in GitHub release asset names
    safe_snapshot = snapshot.replace(":", "-")
    filename = f"{distro}-{version}-{arch}-{flavor}-{safe_snapshot}.tar.xz"
    results.append(RootfsEntry(
        url=rootfs_url, filename=filename,
        distro=distro, version=version, arch=arch,
        flavor=flavor, snapshot=snapshot,
        sha256=sha256s.get(TARGET_FILE),
    ))


async def crawl_arch(session, url, distro, version, arch, results):
    links = parse_links(await fetch_text(session, url), url)
    tasks = []
    for l in links:
        if not l.endswith("/"):
            continue
        flavor = dir_name(l)
        if flavor in SKIP_FLAVORS:
            continue
        tasks.append(crawl_flavor(session, l, distro, version, arch, flavor, results))
    await asyncio.gather(*tasks)


async def crawl_version(session, url, distro, version, results):
    links = parse_links(await fetch_text(session, url), url)
    tasks = []
    for l in links:
        if not l.endswith("/"):
            continue
        arch_orig = dir_name(l)
        if arch_orig not in ARCH_MAP:
            continue
        tasks.append(crawl_arch(session, l, distro, version, ARCH_MAP[arch_orig], results))
    await asyncio.gather(*tasks)


async def crawl_distro(session, distro, url, results):
    print(f"  Scanning {distro}...")
    links = parse_links(await fetch_text(session, url), url)
    tasks = [
        crawl_version(session, l, distro, dir_name(l), results)
        for l in links if l.endswith("/")
    ]
    await asyncio.gather(*tasks)


async def fetch_all_entries(session) -> list[RootfsEntry]:
    results: list[RootfsEntry] = []
    html  = await fetch_text(session, BASE_URL)
    links = parse_links(html, BASE_URL)
    tasks = [
        crawl_distro(session, dir_name(l), l, results)
        for l in links if l.endswith("/") and dir_name(l) in DISTROS
    ]
    await asyncio.gather(*tasks)
    return results


# download

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def download_one(
    session: aiohttp.ClientSession,
    entry:   RootfsEntry,
    sem:     asyncio.Semaphore,
    total:   int,
    idx:     int,
) -> None:
    """Downloads entry; raises on any failure so the caller can abort."""
    dest = OUT_DIR / entry.filename
    tag  = f"[{idx}/{total}]"

    if dest.exists():
        if entry.sha256 and sha256_file(dest) != entry.sha256:
            print(f"{tag} CORRUPT (re-downloading): {entry.filename}")
            dest.unlink()
        else:
            entry.size_bytes = dest.stat().st_size
            print(f"{tag} SKIP (exists): {entry.filename}")
            return

    async with sem:
        print(f"{tag} Downloading: {entry.filename}")
        tmp = dest.with_suffix(".part")
        try:
            async with session.get(entry.url) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 16):
                        f.write(chunk)
        except Exception as e:
            if tmp.exists():
                tmp.unlink()
            raise RuntimeError(f"{tag} ERROR: {entry.filename}: {e}") from e

        if entry.sha256:
            got = sha256_file(tmp)
            if got != entry.sha256:
                tmp.unlink()
                raise RuntimeError(
                    f"{tag} SHA256 MISMATCH: {entry.filename}\n"
                    f"  expected: {entry.sha256}\n"
                    f"  got:      {got}"
                )

        tmp.rename(dest)
        entry.size_bytes = dest.stat().st_size
        print(f"{tag} OK: {entry.filename}")


async def download_all(session, entries: list[RootfsEntry]) -> None:
    """Runs downloads concurrently; aborts everything on first failure."""
    sem   = asyncio.Semaphore(CONCURRENCY)
    total = len(entries)

    tasks = [
        download_one(session, e, sem, total, i + 1)
        for i, e in enumerate(entries)
    ]

    # gather with return_exceptions so we can report then abort cleanly
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors  = [r for r in results if isinstance(r, Exception)]
    if errors:
        for err in errors:
            print(str(err), file=sys.stderr)
        raise SystemExit(1)


# main

async def main():
    OUT_DIR.mkdir(exist_ok=True)

    headers   = {"User-Agent": "Mozilla/5.0 (compatible; rootfs-fetcher/1.0)"}
    connector = aiohttp.TCPConnector(limit=20)
    timeout   = aiohttp.ClientTimeout(total=3600, sock_read=300)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
        print("=== Fetching index ===")
        entries = await fetch_all_entries(session)
        entries.sort(key=lambda e: e.filename)
        print(f"\n=== Found {len(entries)} tarballs. Starting downloads ===\n")

        await download_all(session, entries)

    # write rootfs.json after all downloads succeeded
    payload = [entry_to_json(e) for e in entries]
    JSON_OUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\n=== Done. rootfs.json written with {len(payload)} entries ===")


if __name__ == "__main__":
    asyncio.run(main())
