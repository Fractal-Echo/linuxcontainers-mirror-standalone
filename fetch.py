#!/usr/bin/env python3
"""
Crawls https://images.linuxcontainers.org/images/ and generates rootfs.json
with direct download URLs. No tarballs downloaded.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, unquote

import aiohttp
from bs4 import BeautifulSoup

BASE_URL = "https://images.linuxcontainers.org/images/"
JSON_OUT = Path("rootfs.json")

DISTROS = {
    "almalinux", "alpine", "alt", "amazonlinux", "archlinux",
    "busybox", "centos", "debian", "devuan", "fedora",
    "gentoo", "kali", "mint", "nixos", "openeuler",
    "opensuse", "openwrt", "oracle", "rockylinux",
    "slackware", "ubuntu", "voidlinux",
}
ARCH_MAP = {"amd64": "x86_64", "arm64": "aarch64", "armhf": "armhf"}
SKIP_FLAVORS = {"cloud"}
TARGET_FILE  = "rootfs.tar.xz"
DATED_RE     = re.compile(r"\d{8}_\d{2}(?:%3A|:)\d{2}/?$")

DISTRO_NAMES = {
    "almalinux":   "AlmaLinux",
    "alpine":      "Alpine Linux",
    "alt":         "ALT Linux",
    "amazonlinux": "Amazon Linux",
    "archlinux":   "Arch Linux",
    "busybox":     "BusyBox",
    "centos":      "CentOS",
    "debian":      "Debian GNU/Linux",
    "devuan":      "Devuan",
    "fedora":      "Fedora",
    "gentoo":      "Gentoo",
    "kali":        "Kali Linux",
    "mint":        "Linux Mint",
    "nixos":       "NixOS",
    "openeuler":   "openEuler",
    "opensuse":    "openSUSE",
    "openwrt":     "OpenWrt",
    "oracle":      "Oracle Linux",
    "rockylinux":  "Rocky Linux",
    "slackware":   "Slackware",
    "ubuntu":      "Ubuntu",
    "voidlinux":   "Void Linux",
}


@dataclass
class Entry:
    name:        str
    author:      str
    distro:      str
    description: str
    architecture: str
    download_url: str
    sha256:      str
    size_bytes:  int
    build_date:  str


_head_sem     = asyncio.Semaphore(8)
_head_timeout = aiohttp.ClientTimeout(total=15, sock_read=10)


async def fetch(session, url):
    async with session.get(url) as r:
        r.raise_for_status()
        return await r.text()


def links(html, base):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if h.startswith("?") or h == "../" or h.startswith("http"):
            continue
        out.append(urljoin(base, h))
    return out


def dirname(url):
    return unquote(url.rstrip("/").split("/")[-1])


def latest_dated(ls):
    dated = [l for l in ls if DATED_RE.search(l)]
    return sorted(dated)[-1] if dated else None


async def sha256sums(session, snapshot_url):
    try:
        text = await fetch(session, snapshot_url + "SHA256SUMS")
        out = {}
        for line in text.strip().splitlines():
            parts = line.split()
            if len(parts) == 2:
                out[parts[1].lstrip("*")] = parts[0]
        return out
    except Exception:
        return {}


async def head_size(session, url):
    try:
        decoded_url = unquote(url)
        async with _head_sem:
            async with session.head(decoded_url, allow_redirects=False,
                                    timeout=_head_timeout) as r:
                if r.status in (301, 302, 303, 307, 308):
                    location = r.headers.get("Location")
                    if location:
                        async with session.head(location, allow_redirects=True,
                                                timeout=_head_timeout) as r2:
                            return int(r2.headers.get("Content-Length", 0))
                return int(r.headers.get("Content-Length", 0))
    except Exception:
        return 0


def make_name(distro, version, flavor):
    base = f"{DISTRO_NAMES.get(distro, distro.title())} {version}"
    return base if flavor == "default" else f"{base} ({flavor})"


async def crawl_flavor(session, url, distro, version, arch, flavor, results):
    ls = links(await fetch(session, url), url)
    latest = latest_dated(ls)
    if not latest:
        return

    snapshot = dirname(latest)
    snap_links = links(await fetch(session, latest), latest)
    rootfs_url = next((l for l in snap_links if l.endswith(TARGET_FILE)), None)
    if not rootfs_url:
        return

    sums = await sha256sums(session, latest)
    size = await head_size(session, rootfs_url)
    build_date = snapshot.split("_")[0]
    name = make_name(distro, version, flavor)

    results.append(Entry(
        name=name,
        author="linuxcontainers",
        distro=DISTRO_NAMES.get(distro, distro.title()),
        description=f"Official LXC container image for {name}",
        architecture=arch,
        download_url=rootfs_url,
        sha256=sums.get(TARGET_FILE, ""),
        size_bytes=size,
        build_date=build_date,
    ))


async def crawl_arch(session, url, distro, version, arch, results):
    ls = links(await fetch(session, url), url)
    tasks = [
        crawl_flavor(session, l, distro, version, arch, dirname(l), results)
        for l in ls if l.endswith("/") and dirname(l) not in SKIP_FLAVORS
    ]
    await asyncio.gather(*tasks)


async def crawl_version(session, url, distro, version, results):
    ls = links(await fetch(session, url), url)
    tasks = [
        crawl_arch(session, l, distro, version, ARCH_MAP[dirname(l)], results)
        for l in ls if l.endswith("/") and dirname(l) in ARCH_MAP
    ]
    await asyncio.gather(*tasks)


async def crawl_distro(session, distro, url, results):
    print(f"  Scanning {distro}...")
    ls = links(await fetch(session, url), url)
    tasks = [
        crawl_version(session, l, distro, dirname(l), results)
        for l in ls if l.endswith("/")
    ]
    await asyncio.gather(*tasks)


async def main():
    headers   = {"User-Agent": "Mozilla/5.0 (compatible; rootfs-fetcher/1.0)"}
    connector = aiohttp.TCPConnector(limit=20)
    timeout   = aiohttp.ClientTimeout(total=3600, sock_read=60)

    results = []
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
        print("=== Crawling LXC index ===")
        html = await fetch(session, BASE_URL)
        ls   = links(html, BASE_URL)
        tasks = [
            crawl_distro(session, dirname(l), l, results)
            for l in ls if l.endswith("/") and dirname(l) in DISTROS
        ]
        await asyncio.gather(*tasks)

    results.sort(key=lambda e: e.name)
    payload = [e.__dict__ for e in results]
    JSON_OUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\n=== Done. rootfs.json written with {len(payload)} entries ===")


if __name__ == "__main__":
    asyncio.run(main())
