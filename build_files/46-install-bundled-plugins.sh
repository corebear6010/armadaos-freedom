#!/bin/bash
set -euxo pipefail

# Bundled Decky plugins, preinstalled into the image location the official
# armada-decky-sync seeds from. No new boot service is added; the existing
# armada-decky-sync copies /usr/share/decky-plugins/* into the user's homebrew
# tree.
#
# decky/plugin-sources.json is the truth for these plugins. Every build start
# resolves the newest release, branch-tip and in-tree source into
# decky/bundled-plugins/plugins.lock.json, decky/bundled-plugins/assets and
# decky/bundled-plugins/plugins.resolved.json. This step keeps no vendored or
# private older package policy: it installs only those generated archives, and
# only after validate_resolved_plugin_sources() re-checked the report, the lock
# bytes, every generated archive and the versions 45 already installed.
#
# Each package already carries a top-level directory named after the plugin, so
# its zip extracts straight into /usr/share/decky-plugins without an extra
# nesting level.
#
# Only upstream release files (licenses included) are installed: no
# subscriptions, personal configuration, commercial DLLs, or per-game configs.

work="$(mktemp -d /tmp/armada-bundled-plugins.XXXXXX)"
trap 'rm -rf "${work}"' EXIT

install -d -m 0755 /usr/share/decky-plugins

PLUGIN_DEST=/usr/share/decky-plugins \
PLUGIN_WORK="${work}" \
python3 - <<'PY'
import hashlib
import importlib.util
import os
import shutil
import zipfile

SOURCE = "/ctx"
RESOLVER = os.path.join(SOURCE, "build_files", "resolve-plugin-sources.py")
DEST = os.environ["PLUGIN_DEST"]
WORK = os.environ["PLUGIN_WORK"]
REQUIRED_FILES = ("plugin.json", "main.py", "dist/index.js")


def load_resolver():
    spec = importlib.util.spec_from_file_location(
        "armada_resolve_plugin_sources", RESOLVER
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the plugin resolver from {RESOLVER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def members(archive):
    for info in archive.infolist():
        name = info.filename
        if name.startswith("/") or os.path.isabs(name):
            raise SystemExit(f"unsafe absolute path in archive: {name}")
        if any(part == ".." for part in name.split("/")):
            raise SystemExit(f"unsafe relative path in archive: {name}")
        yield info


def extract(archive, dest):
    # zipfile.extract() drops Unix permissions, so apply the mode recorded in
    # each ZipInfo's external_attr (high 16 bits) to honour executable bits.
    for info in members(archive):
        target = os.path.join(dest, info.filename)
        if info.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with archive.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        mode = (info.external_attr >> 16) & 0o777
        if mode:
            os.chmod(target, mode)


def root_owned(path):
    os.chown(path, 0, 0)
    for dirpath, _dirnames, filenames in os.walk(path):
        os.chown(dirpath, 0, 0)
        for filename in filenames:
            os.chown(os.path.join(dirpath, filename), 0, 0)


def ensure_executable(plugin_dir, wanted):
    root = os.path.join(DEST, plugin_dir)
    preferred = os.path.join(root, "bin", wanted)
    if os.path.isfile(preferred):
        candidates = [preferred]
    else:
        candidates = [
            os.path.join(dirpath, filename)
            for dirpath, _dirnames, filenames in os.walk(root)
            for filename in filenames
            if filename == wanted
        ]
    if not candidates:
        raise SystemExit(f"expected {wanted} inside {plugin_dir} but found none")
    for path in candidates:
        os.chmod(path, 0o755)
        print(f"marked executable: {path}")


resolve = load_resolver()

# Without a resolution report there is nothing verified to install. The
# validator names resolve-plugin-sources.py and the supported build entry, so a
# direct build fails here instead of fetching a URL or reusing an older ZIP.
try:
    plugins = resolve.validate_resolved_plugin_sources(SOURCE, installed_root=DEST)
except resolve.ResolveError as error:
    raise SystemExit(f"resolved plugin validation failed: {error}")

if not isinstance(plugins, list) or not plugins:
    raise SystemExit("the resolved plugin lock is empty")

for plugin in plugins:
    name = plugin["name"]
    directory = plugin["directory"]
    archive_rel = plugin.get("archive")
    archive_path = os.path.join(WORK, f"{directory}.zip")

    print(f"installing {name} {plugin['version']}")
    if not archive_rel:
        raise SystemExit(
            f"{name} has no generated archive in the resolved lock; "
            "run build_files/resolve-plugin-sources.py through the supported "
            "build entry"
        )
    shutil.copyfile(os.path.join(SOURCE, archive_rel), archive_path)
    print(f"using generated package: {archive_rel}")

    actual = sha256(archive_path)
    expected = plugin["sha256"].lower()
    if actual != expected:
        raise SystemExit(f"sha256 mismatch for {name}: {actual} != {expected}")
    print(f"sha256 verified: {actual}")

    with zipfile.ZipFile(archive_path) as archive:
        extract(archive, DEST)

    plugin_root = os.path.join(DEST, directory)
    for required in REQUIRED_FILES:
        path = os.path.join(plugin_root, required)
        if not os.path.isfile(path):
            raise SystemExit(f"missing {required} in {name} ({directory})")

    root_owned(plugin_root)

    # DeckyClash's proxy core must stay executable for its tun service.
    if directory == "DeckyClash":
        ensure_executable(directory, "mihomo")

print("bundled plugins installed")
PY

# Keep the resolved lock and report in the image for traceability.
install -Dpm 0644 /ctx/decky/bundled-plugins/plugins.lock.json \
    /usr/share/armada/bundled-plugins/plugins.lock.json
install -Dpm 0644 /ctx/decky/bundled-plugins/plugins.resolved.json \
    /usr/share/armada/bundled-plugins/plugins.resolved.json
