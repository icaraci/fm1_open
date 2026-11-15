#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only WITH GPL-3.0-linking-exception
"""POSIX (macOS/Linux) port of tools/Build-Web.ps1.

Builds the browser simulator from the same C sources, with the same flags,
the same input set, and the same source hash as the PowerShell script, then
assembles build/preview/index.html. Firmware packaging stays Windows-only
(JieLi toolchain): this script refuses --package instead of pretending.

Usage:
    python3 tools/Build-Web.py --local-preview
    python3 tools/Build-Web.py --local-preview --check      # build + headless smoke test
    FM1_WASI_SDK=/path/to/wasi-sdk-27 python3 tools/Build-Web.py

The WASI SDK is resolved in this order: --wasi-sdk, $FM1_WASI_SDK, then
build/tools/wasi-sdk-27.0-<host> (downloaded and SHA-256 checked when the host
is in HOSTS). Other hosts must pass an explicit SDK directory.
"""

import argparse
import base64
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
WASI_VERSION = "27.0"
WASI_ARCHIVES = {
    ("Darwin", "arm64"): ("wasi-sdk-27.0-arm64-macos.tar.gz",
                          "055c3dc2766772c38e71a05d353e35c322c7b2c6458a36a26a836f9808a550f8",
                          "wasi-sdk-27.0-arm64-macos"),
    ("Darwin", "x86_64"): ("wasi-sdk-27.0-x86_64-macos.tar.gz",
                           "163dfd47f989b1a682744c1ae1f0e09a83ff5c4bbac9dcd8546909ab54cda5a1",
                           "wasi-sdk-27.0-x86_64-macos"),
}
RELEASE_URL = "https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-27/"
SOURCE_DIRS = ("app", "web", "third_party/synth")
PLACEHOLDER = re.compile(r"/\*(FM1_[A-Z0-9_]+)\*/")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def digest_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_inputs():
    """Mirror of the PowerShell input set: compile units plus their headers."""
    inputs = {}
    for name in SOURCE_DIRS:
        for path in sorted((ROOT / name).rglob("*")):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith("app/board/") or not path.is_file():
                continue
            if path.suffix not in (".c", ".h", ".inc"):
                continue
            inputs[rel] = sha256_file(path)
    for extra in ["tools/Build-Web.ps1"]:
        inputs[extra] = sha256_file(ROOT / extra)
    for header in sorted((ROOT / "sdk/include").glob("*.h")):
        inputs["sdk/include/" + header.name] = sha256_file(header)
    return inputs


def source_hash(inputs):
    names = sorted(inputs, key=lambda n: n.encode("utf-8"))
    return digest_text("\n".join(n + ":" + inputs[n] for n in names))


def compile_sources(inputs):
    return [n for n in sorted(inputs) if n.endswith(".c") and not n.startswith("sdk/")]


def release_version():
    text = (ROOT / "app/version.h").read_text(encoding="utf-8")
    match = re.search(r'(?m)^#define FM1_RELEASE_VERSION "(V(?:0[1-9]|[1-9][0-9]{1,2}))"\s*$', text)
    if not match:
        raise SystemExit("Set FM1_RELEASE_VERSION in app/version.h.")
    return match.group(1)


def toolchain_stamp(sdk):
    files = []
    for path in sorted(sdk.rglob("*")):
        if path.is_file():
            stat = path.stat()
            files.append("%s:%d:%d" % (path.relative_to(sdk).as_posix(), stat.st_size, stat.st_mtime_ns))
    return digest_text("\n".join([str(sdk), "builder=posix"] + files))


def resolve_wasi_sdk(explicit):
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("FM1_WASI_SDK")
    if env:
        return Path(env).resolve()
    key = (sys.platform == "darwin" and "Darwin" or sys.platform, os.uname().machine)
    entry = WASI_ARCHIVES.get(key)
    if not entry:
        raise SystemExit("No pinned WASI SDK for %s/%s. Set FM1_WASI_SDK to an extracted "
                         "wasi-sdk-%s directory." % (os.uname().sysname, os.uname().machine, WASI_VERSION))
    archive_name, expected, dirname = entry
    cache = BUILD / "tools"
    target = cache / dirname
    if (target / "bin/clang").exists() and (target / "share/wasi-sysroot").exists():
        return target
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / archive_name
    if not archive.exists() or sha256_file(archive) != expected:
        print("Downloading %s; set FM1_WASI_SDK to reuse an extracted compiler."
              % archive_name, flush=True)
        with urllib.request.urlopen(RELEASE_URL + archive_name) as response, open(archive, "wb") as out:
            shutil.copyfileobj(response, out, 1 << 20)
    actual = sha256_file(archive)
    if actual != expected:
        raise SystemExit("WASI SDK archive SHA-256 mismatch: %s" % actual)
    with tarfile.open(archive, "r:gz") as tar:
        try:
            tar.extractall(cache, filter="data")
        except TypeError:                       # Python < 3.12 has no extraction filters
            tar.extractall(cache)
    return target


def build_simulator(inputs, sdk):
    out = BUILD / "web"
    out.mkdir(parents=True, exist_ok=True)
    wasm = out / "simulator.wasm"
    metadata = out / "simulator.json"
    digest = source_hash(inputs)
    stamp = toolchain_stamp(sdk)
    if wasm.exists() and metadata.exists():
        try:
            cached = json.loads(metadata.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cached = None
        if (cached and cached.get("schema") == 2 and cached.get("builder") == "posix"
                and cached.get("toolchain_stamp") == stamp and cached.get("source_sha256") == digest
                and cached.get("wasm_sha256") == sha256_file(wasm)):
            print("Simulator cache hit")
            return wasm, digest, True
    for stale in (wasm, metadata):
        if stale.exists():
            stale.unlink()
    compiler = sdk / "bin/clang"
    if not compiler.exists() or not (sdk / "share/wasi-sysroot").is_dir():
        raise SystemExit("Set FM1_WASI_SDK to an extracted WASI SDK %s directory." % WASI_VERSION)
    if not (ROOT / "web/engine/Simulator.c").exists():
        raise SystemExit("Missing browser sources. Download the complete source tree.")
    options = ["--sysroot=%s" % (sdk / "share/wasi-sysroot"), "-std=c11", "-O2", "-fwrapv",
               "-fno-fast-math", "-ffp-contract=off", "-DFM1_HOST_TEST", "-DFM1_OPEN_WEB",
               "-Wall", "-Wextra", "-Werror", "-Wno-unused-function", "-Wno-array-parameter",
               "-mexec-model=reactor"]
    for name in ["web/engine", "web/synth", "third_party/synth", "sdk/include", "app/synth", "app"]:
        options.append("-I" + str(ROOT / name))
    options += [str(ROOT / name) for name in compile_sources(inputs)]
    options += ["-Wl,--strip-all", "-Wl,-z,stack-size=131072", "-Wl,--initial-memory=4194304",
                "-Wl,--max-memory=33554432", "-o", str(wasm)]
    print("Compile the browser application (%d units)" % len(compile_sources(inputs)))
    result = subprocess.run([str(compiler)] + options)
    if result.returncode != 0:
        for stale in (wasm, metadata):
            if stale.exists():
                stale.unlink()
        raise SystemExit("Simulator build failed (%d)." % result.returncode)
    record = {"schema": 2, "builder": "posix", "source_sha256": digest,
              "toolchain_stamp": stamp, "wasm_sha256": sha256_file(wasm)}
    metadata.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    return wasm, digest, False


def assemble_page(wasm, source_digest, version, source_url, local_preview, output):
    template = (ROOT / "sdk/Update.html.in").read_text(encoding="utf-8")
    if local_preview:
        template = re.sub(r'<a\b[^>]*href="/\*FM1_SOURCE_URL\*/"[^>]*>(.*?)</a>',
                          r"<span>\1 (local preview)</span>", template)
    simulation = {"schema": 1, "release_version": version, "sha256": sha256_file(wasm),
                  "source_sha256": source_digest, "sample_bank": "synthesized-demo",
                  "data": base64.b64encode(wasm.read_bytes()).decode("ascii")}
    notices = ["APP: GPLv3 with the FM1 SDK linking exception; no warranty. SDK and firmware "
               "resources have separate redistribution terms in THIRD_PARTY.md. Source: %s" % source_url]
    for name in ("THIRD_PARTY.md", "LICENSE.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        if name == "THIRD_PARTY.md" and not text.startswith("# Third-party notices"):
            raise SystemExit("Use the public distribution notices.")
        notices.append("\n===== %s =====\n%s" % (name, text))
    values = {
        "FM1_SOURCE_URL": html.escape(source_url, quote=True),
        "FM1_PAYLOAD": "const FM1_PACKAGE = null;",
        "FM1_SIMULATOR": "const FM1_SIMULATOR = " + json.dumps(simulation, separators=(",", ":")) + ";",
        "FM1_I18N": (ROOT / "sdk/I18n.js").read_text(encoding="utf-8"),
        "FM1_SCRIPT": (ROOT / "sdk/WebUpdate.js").read_text(encoding="utf-8"),
        "FM1_SIM_SCRIPT": (ROOT / "sdk/Simulator.js").read_text(encoding="utf-8"),
        "FM1_SAMPLE_SCRIPT": (ROOT / "sdk/Samples.js").read_text(encoding="utf-8"),
        "FM1_NOTICES": html.escape("\n".join(notices), quote=True),
    }
    page = PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
    if PLACEHOLDER.search(page):
        raise SystemExit("Unexpanded HTML placeholder.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="\n") as page_file:
        page_file.write(page)
    return simulation


def main():
    parser = argparse.ArgumentParser(description="Build the FM1 browser simulator on POSIX.")
    parser.add_argument("--local-preview", action="store_true",
                        help="preview page without a distribution source URL")
    parser.add_argument("--source-url", default=os.environ.get("FM1_SOURCE_URL", ""),
                        help="HTTPS URL of the matching source archive")
    parser.add_argument("--wasi-sdk", default=None, help="extracted wasi-sdk-27 directory")
    parser.add_argument("--out", default=None, help="output HTML path")
    parser.add_argument("--check", action="store_true", help="run the headless smoke test afterwards")
    parser.add_argument("--package", action="store_true", help="unsupported on POSIX")
    parser.add_argument("--json", action="store_true", help="print a build summary as JSON")
    args = parser.parse_args()
    if args.package:
        raise SystemExit("Packaging needs the Windows JieLi 2.5.3 toolchain; run build.bat --package on Windows.")
    source_url = args.source_url
    if not args.local_preview:
        if not source_url or not source_url.startswith("https://") or "@" in source_url.split("//", 1)[1].split("/", 1)[0]:
            raise SystemExit("Set FM1_SOURCE_URL or --source-url to an HTTPS source archive, or use --local-preview.")
    inputs = source_inputs()
    sdk = resolve_wasi_sdk(args.wasi_sdk)
    wasm, digest, cache_hit = build_simulator(inputs, sdk)
    version = release_version()
    output = Path(args.out) if args.out else BUILD / "preview/index.html"
    simulation = assemble_page(wasm, digest, version, source_url, args.local_preview, output)
    summary = {"file": str(output), "simulator": str(wasm), "simulator_sha256": simulation["sha256"],
               "source_sha256": digest, "simulator_bytes": wasm.stat().st_size,
               "release_version": version, "cache_hit": cache_hit}
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print("Preview: %s" % output)
        print("Simulator %s (%d bytes, cache %s)" % (simulation["sha256"][:16], wasm.stat().st_size,
                                                     "hit" if cache_hit else "miss"))
    if args.check:
        return subprocess.run(["node", str(ROOT / "tools/sim-headless.mjs"), "--wasm", str(wasm),
                               "--check"]).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
