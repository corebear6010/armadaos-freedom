"""Resolve the newest available source for every bundled Decky plugin.

Every future official OS image build must bundle the latest public release of
each release plugin and the current tip of each git plugin, so this helper is
the single place that decides those versions:

* the catalogue in decky/plugin-sources.json (schemaVersion 1) lists release,
  git and in-tree plugins;
* every release entry is re-resolved on each invocation through the GitHub
  "latest release" endpoint, its single matching asset is downloaded, its
  announced sha256 digest is verified before anything is written, and the
  plugin version is read from the archive's own package.json;
* the git entry is resolved to one immutable branch-tip commit, checked out
  detached at exactly that commit, and its version is read from the checkout;
* the in-tree entry's version comes from its packagePath and reports the source
  HEAD;
* decky/bundled-plugins/plugins.lock.json (the ZIP install manifest, still the
  current array format) and decky/bundled-plugins/plugins.resolved.json are
  written atomically only after every entry resolved, so a source that cannot
  be resolved never falls back to an older archive or an older lock.

Only the standard library and the git CLI are used. Every network and git
access stays behind the injectable fetch_json/fetch_bytes/run_git hooks so the
tests never touch the network.
"""

import argparse
import fnmatch
import hashlib
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile

SCHEMA_VERSION = 1
CONFIG_PATH = "decky/plugin-sources.json"
LOCK_PATH = "decky/bundled-plugins/plugins.lock.json"
REPORT_PATH = "decky/bundled-plugins/plugins.resolved.json"
ASSETS_PATH = "decky/bundled-plugins/assets"

API_ROOT = "https://api.github.com/repos"
USER_AGENT = "armada-plugin-resolver"
HTTP_TIMEOUT = 60

# 46-install-bundled-plugins.sh installs every archive with the same contract.
REQUIRED_FILES = ("plugin.json", "main.py", "dist/index.js")

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:([0-9a-fA-F]{64})$")


class ResolveError(Exception):
    """A plugin source could not be resolved to a verified artifact."""


def _sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def _commit(value, what):
    text = value.strip() if isinstance(value, str) else ""
    if not _COMMIT.match(text):
        raise ResolveError(
            "%s is not a lowercase 40 character commit hash: %r" % (what, value)
        )
    if text == "0" * 40:
        raise ResolveError("%s is an all-zero commit hash: %r" % (what, value))
    return text


def _text_field(document, field, what):
    """Return a required non-empty string field unchanged."""
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ResolveError("%s has no %s string" % (what, field))
    return value


def _hex64(value, what):
    text = value.strip() if isinstance(value, str) else ""
    if not _HEX64.match(text):
        raise ResolveError(
            "%s is not a lowercase 64 character sha256: %r" % (what, value)
        )
    return text


def _inside(source, relative):
    """Resolve relative inside source and refuse paths that escape it."""
    root = os.path.realpath(source)
    target = os.path.realpath(os.path.join(root, relative))
    if target != root and not target.startswith(root + os.sep):
        raise ResolveError("configured path escapes the source tree: %s" % relative)
    return target


def _read_json(path, what):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as error:
        raise ResolveError("cannot read %s: %s" % (what, error))
    except ValueError as error:
        raise ResolveError("%s is not valid JSON: %s" % (what, error))


def _version_of(document, what):
    if not isinstance(document, dict):
        raise ResolveError("%s is not a JSON object" % what)
    version = document.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ResolveError("%s has no version string" % what)
    return version.strip()


def _slug(text):
    cleaned = "".join(char if char.isalnum() or char in "._-" else "-" for char in text)
    cleaned = cleaned.strip("-.")
    if not cleaned:
        raise ResolveError("cannot derive an asset file name from %r" % (text,))
    return cleaned


# -- HTTP defaults -------------------------------------------------------


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """Drop credentials when a redirect leaves the API host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            old_host = urllib.parse.urlsplit(req.full_url).hostname
            if urllib.parse.urlsplit(newurl).hostname != old_host:
                redirected.remove_header("Authorization")
        return redirected


_opener = None


def _opener_for_requests():
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(_RedirectGuard())
    return _opener


def _request(url, *, api):
    headers = {"User-Agent": USER_AGENT}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        # Only the JSON API host ever receives the token; binary downloads to
        # any other host never carry Authorization.
        if token and urllib.parse.urlsplit(url).hostname == "api.github.com":
            headers["Authorization"] = "Bearer " + token
    try:
        request = urllib.request.Request(url, headers=headers)
        with _opener_for_requests().open(request, timeout=HTTP_TIMEOUT) as response:
            return response.read()
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise ResolveError("request failed for %s: %s" % (url, error))


def default_fetch_json(url):
    payload = _request(url, api=True)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ResolveError("%s did not return JSON: %s" % (url, error))


def default_fetch_bytes(url):
    return _request(url, api=False)


def default_run_git(argv):
    completed = subprocess.run(
        ["git"] + [str(item) for item in argv],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.decode("utf-8", "replace")


# -- catalogue -----------------------------------------------------------


def _load_config(source, config):
    if config is None:
        config = _read_json(_inside(source, CONFIG_PATH), CONFIG_PATH)
    if not isinstance(config, dict):
        raise ResolveError("catalogue must be a JSON object")
    if config.get("schemaVersion") != SCHEMA_VERSION:
        raise ResolveError(
            "unsupported catalogue schemaVersion: %r" % (config.get("schemaVersion"),)
        )
    return config


def _section(config, name, required):
    entries = config.get(name, [])
    if not isinstance(entries, list):
        raise ResolveError("catalogue %s must be a list" % name)
    for entry in entries:
        if not isinstance(entry, dict):
            raise ResolveError("catalogue %s entries must be objects" % name)
        for field in required:
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ResolveError("catalogue %s entry is missing %s" % (name, field))
    return entries


# -- release plugins -----------------------------------------------------


def _archive_version(directory, payload, what):
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise ResolveError("%s is not a zip archive: %s" % (what, error))
    with archive:
        names = set(archive.namelist())
        for required in REQUIRED_FILES:
            member = "%s/%s" % (directory, required)
            if member not in names:
                raise ResolveError("%s is missing %s" % (what, member))
        package = "%s/package.json" % directory
        if package not in names:
            raise ResolveError("%s is missing %s" % (what, package))
        try:
            document = json.loads(archive.read(package).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ResolveError("%s has an unreadable %s: %s" % (what, package, error))
    return _version_of(document, package)


def _resolve_release(entry, fetch_json, fetch_bytes):
    repository = entry["repository"]
    pattern = entry["assetPattern"]

    release = fetch_json("%s/%s/releases/latest" % (API_ROOT, repository))
    if not isinstance(release, dict):
        raise ResolveError("latest release for %s is not an object" % repository)
    if release.get("draft") or release.get("prerelease"):
        raise ResolveError("latest release for %s is a draft or prerelease" % repository)
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        raise ResolveError("latest release for %s has no tag name" % repository)
    tag = tag.strip()

    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ResolveError("latest release for %s has no asset list" % repository)
    matches = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and isinstance(asset.get("name"), str)
        and fnmatch.fnmatchcase(asset["name"], pattern)
    ]
    if len(matches) != 1:
        raise ResolveError(
            "%s release %s has %d assets matching %s"
            % (repository, tag, len(matches), pattern)
        )
    asset = matches[0]

    digest = asset.get("digest")
    match = _DIGEST.match(digest.strip()) if isinstance(digest, str) else None
    if match is None:
        raise ResolveError(
            "%s asset %s has no sha256:<64hex> digest" % (repository, asset["name"])
        )
    expected = match.group(1).lower()

    url = asset.get("browser_download_url")
    if not isinstance(url, str) or not url.strip():
        raise ResolveError("%s asset %s has no download url" % (repository, asset["name"]))
    url = url.strip()

    payload = fetch_bytes(url)
    if not isinstance(payload, (bytes, bytearray)):
        raise ResolveError("%s asset %s is not a byte payload" % (repository, asset["name"]))
    payload = bytes(payload)

    announced = asset.get("size")
    if isinstance(announced, int) and announced >= 0 and announced != len(payload):
        raise ResolveError(
            "%s asset %s is %d bytes, expected %d"
            % (repository, asset["name"], len(payload), announced)
        )
    actual = _sha256(payload)
    if actual != expected:
        raise ResolveError(
            "%s asset %s has sha256 %s, expected %s"
            % (repository, asset["name"], actual, expected)
        )

    version = _archive_version(entry["directory"], payload, "%s %s" % (repository, tag))

    archive = "%s/%s-%s.zip" % (ASSETS_PATH, _slug(entry["directory"]), actual)
    lock_entry = {
        "name": entry["name"],
        "directory": entry["directory"],
        "version": version,
        "url": url,
        "sha256": actual,
        "archive": archive,
    }
    report_entry = {
        "name": entry["name"],
        "directory": entry["directory"],
        "kind": "release",
        "repository": repository,
        "version": version,
        "releaseTag": tag,
        "sha256": actual,
        "url": url,
        "archive": archive,
    }
    return lock_entry, report_entry, payload


# -- git and in-tree plugins ---------------------------------------------


def _resolve_git(source, entry, fetch_json, run_git):
    repository = entry["repository"]
    branch = entry["branch"]
    path = entry["path"]

    tip = fetch_json(
        "%s/%s/commits/%s"
        % (API_ROOT, repository, urllib.parse.quote(branch, safe=""))
    )
    if not isinstance(tip, dict):
        raise ResolveError("branch tip for %s is not an object" % repository)
    commit = _commit(tip.get("sha"), "branch tip for %s %s" % (repository, branch))

    worktree = _inside(source, path)
    run_git(["-C", source, "submodule", "update", "--init", "--", path])

    status = run_git(["-C", worktree, "status", "--porcelain", "--untracked-files=no"])
    if isinstance(status, (bytes, bytearray)):
        status = status.decode("utf-8", "replace")
    if status.strip():
        raise ResolveError(
            "%s has tracked changes; refusing to overwrite them" % path
        )

    run_git(["-C", worktree, "fetch", "origin", commit])
    run_git(["-C", worktree, "checkout", "--detach", commit])

    head = _commit(
        run_git(["-C", worktree, "rev-parse", "HEAD"]), "%s HEAD" % path
    )
    if head != commit:
        raise ResolveError("%s is at %s after checkout, expected %s" % (path, head, commit))

    package_relative = entry["packagePath"]
    package = _inside(worktree, package_relative)
    version = _version_of(_read_json(package, package_relative), package_relative)

    return {
        "name": entry["name"],
        "directory": entry["directory"],
        "kind": "git",
        "repository": repository,
        "branch": branch,
        "commit": commit,
        "version": version,
    }


def _resolve_in_tree(source, entry, commit):
    package_relative = entry["packagePath"]
    package = _inside(source, package_relative)
    version = _version_of(_read_json(package, package_relative), package_relative)
    return {
        "name": entry["name"],
        "directory": entry["directory"],
        "kind": "in-tree",
        "version": version,
        "commit": commit,
    }


# -- writing -------------------------------------------------------------


def _write_bytes(path, payload):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".resolved-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _json_bytes(document):
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


# -- entry point ---------------------------------------------------------


def resolve_plugin_sources(
    source,
    *,
    config=None,
    fetch_json=None,
    fetch_bytes=None,
    run_git=None,
):
    """Resolve every bundled plugin and return the resolution report.

    source is the checkout to resolve against. config is the parsed catalogue
    and defaults to CONFIG_PATH inside source. fetch_json(url) returns a decoded
    JSON document, fetch_bytes(url) returns the raw asset bytes, and run_git(argv)
    runs git with argv (excluding "git") and returns stdout. Nothing is written
    until every entry resolved and verified.
    """
    fetch_json = fetch_json or default_fetch_json
    fetch_bytes = fetch_bytes or default_fetch_bytes
    run_git = run_git or default_run_git

    source = os.fspath(source)
    if not os.path.isdir(source):
        raise ResolveError("source is not a directory: %s" % source)

    commit = _commit(run_git(["-C", source, "rev-parse", "HEAD"]), "source HEAD")
    catalogue = _load_config(source, config)

    lock = []
    archives = []
    plugins = []

    for entry in _section(
        catalogue, "releases", ("name", "directory", "repository", "assetPattern")
    ):
        lock_entry, report_entry, payload = _resolve_release(entry, fetch_json, fetch_bytes)
        lock.append(lock_entry)
        archives.append((lock_entry["archive"], payload))
        plugins.append(report_entry)

    for entry in _section(
        catalogue,
        "git",
        ("name", "directory", "path", "repository", "branch", "packagePath"),
    ):
        plugins.append(_resolve_git(source, entry, fetch_json, run_git))

    for entry in _section(catalogue, "inTree", ("name", "directory", "packagePath")):
        plugins.append(_resolve_in_tree(source, entry, commit))

    lock_bytes = _json_bytes(lock)
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "sourceCommit": commit,
        "lockSha256": _sha256(lock_bytes),
        "plugins": plugins,
    }

    for archive, payload in archives:
        _write_bytes(_inside(source, archive), payload)
    _write_bytes(_inside(source, LOCK_PATH), lock_bytes)
    _write_bytes(_inside(source, REPORT_PATH), _json_bytes(report))
    return report


# -- validation of a resolved checkout ------------------------------------


def _report_index(plugins, what):
    """Index report plugins by (kind, name, directory), refusing duplicates."""
    if not isinstance(plugins, list):
        raise ResolveError("%s plugins must be a list" % what)
    index = {}
    for item in plugins:
        if not isinstance(item, dict):
            raise ResolveError("%s plugins must be objects" % what)
        kind = _text_field(item, "kind", "%s plugin" % what)
        name = _text_field(item, "name", "%s plugin" % what)
        directory = _text_field(item, "directory", "%s plugin" % what)
        key = (kind, name, directory)
        if key in index:
            raise ResolveError(
                "%s reports %s %s (%s) more than once" % (what, kind, name, directory)
            )
        index[key] = item
    return index


def validate_resolved_plugin_sources(source, *, installed_root=None):
    """Verify a resolved checkout without resolving anything again.

    Returns the generated lock list (the same array the resolver wrote to
    LOCK_PATH) after checking, in place and without network or git access:

    * decky/bundled-plugins/plugins.resolved.json exists and parsed, and its
      schemaVersion, sourceCommit and lockSha256 describe the checkout; a
      missing report is a hard failure that tells the caller to run
      build_files/resolve-plugin-sources.py, never a fallback to an older lock
      or an older bundled archive;
    * the lock on disk is byte-for-byte the lock the report declares via
      lockSha256, so a lock edited or regenerated after resolution is rejected
      instead of silently replacing the verified one;
    * the report covers every source the catalogue declares, each exactly once
      and by kind plus name and directory, with no entry left over from an
      older lock or report;
    * every release archive the lock points at is inside the source tree, its
      bytes hash to the recorded sha256, and the version it declares inside the
      ZIP matches the version the report and lock record (an outer-hash-
      consistent but internally stale archive is still rejected);
    * the git plugin's packagePath under its configured path still reports the
      resolved version, and every in-tree packagePath still reports its
      recorded version under the source commit;
    * when installed_root is given, every git and in-tree plugin directory
      installed there reports the version the report records (the 46 installer
      copies release archives at install time, so installed_root need not hold
      them yet).

    The git report commit is only checked for the nonzero 40 character hex
    shape: this function reads no Git metadata and cannot prove which commit a
    working tree came from. Immutability of that commit is established by the
    resolver's detached checkout, not by this check.

    installed_root is optional because the release archives are installed later;
    the check must work inside an image context where no network or git exists.
    """
    source = os.fspath(source)
    if not os.path.isdir(source):
        raise ResolveError("source is not a directory: %s" % source)

    catalogue = _load_config(source, None)

    report_path = _inside(source, REPORT_PATH)
    if not os.path.isfile(report_path):
        raise ResolveError(
            "no %s in this checkout: execute build_files/resolve-plugin-sources.py "
            "through the supported build entry before packaging so every plugin is "
            "resolved; the previous %s and any bundled archive are never reused"
            % (REPORT_PATH, LOCK_PATH)
        )
    report = _read_json(report_path, REPORT_PATH)
    if not isinstance(report, dict):
        raise ResolveError("%s must be a JSON object" % REPORT_PATH)
    if report.get("schemaVersion") != SCHEMA_VERSION:
        raise ResolveError(
            "%s has unsupported schemaVersion %r"
            % (REPORT_PATH, report.get("schemaVersion"))
        )
    source_commit = _commit(report.get("sourceCommit"), "%s sourceCommit" % REPORT_PATH)
    lock_sha = _hex64(report.get("lockSha256"), "%s lockSha256" % REPORT_PATH)

    lock_path = _inside(source, LOCK_PATH)
    try:
        with open(lock_path, "rb") as handle:
            lock_bytes = handle.read()
    except OSError as error:
        raise ResolveError("cannot read %s: %s" % (LOCK_PATH, error))
    actual_lock_sha = _sha256(lock_bytes)
    if actual_lock_sha != lock_sha:
        raise ResolveError(
            "%s has sha256 %s but %s recorded %s: the lock changed after resolution"
            % (LOCK_PATH, actual_lock_sha, REPORT_PATH, lock_sha)
        )
    try:
        lock = json.loads(lock_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ResolveError("%s is not valid JSON: %s" % (LOCK_PATH, error))
    if not isinstance(lock, list) or not lock:
        raise ResolveError("%s must be a non-empty array" % LOCK_PATH)

    releases = _section(
        catalogue, "releases", ("name", "directory", "repository", "assetPattern")
    )
    git_entries = _section(
        catalogue,
        "git",
        ("name", "directory", "path", "repository", "branch", "packagePath"),
    )
    tree_entries = _section(catalogue, "inTree", ("name", "directory", "packagePath"))

    declared = (
        [("release", entry) for entry in releases]
        + [("git", entry) for entry in git_entries]
        + [("in-tree", entry) for entry in tree_entries]
    )
    reported = _report_index(report.get("plugins"), REPORT_PATH)

    expected = {(kind, entry["name"], entry["directory"]) for kind, entry in declared}
    extras = sorted(key for key in reported if key not in expected)
    if extras:
        raise ResolveError(
            "%s reports %s, which the catalogue does not declare"
            % (REPORT_PATH, ", ".join("%s %s (%s)" % key for key in extras))
        )
    missing = sorted(key for key in expected if key not in reported)
    if missing:
        raise ResolveError(
            "%s does not report %s; every configured source must be resolved"
            % (REPORT_PATH, ", ".join("%s %s (%s)" % key for key in missing))
        )

    if len(lock) != len(releases):
        raise ResolveError(
            "%s has %d entries but %d release sources are configured"
            % (LOCK_PATH, len(lock), len(releases))
        )
    locked = {}
    for entry in lock:
        if not isinstance(entry, dict):
            raise ResolveError("%s entries must be objects" % LOCK_PATH)
        for field in ("name", "directory", "version", "url", "sha256", "archive"):
            _text_field(entry, field, "%s entry" % LOCK_PATH)
        directory = entry["directory"]
        if directory in locked:
            raise ResolveError(
                "%s resolves directory %r more than once" % (LOCK_PATH, directory)
            )
        locked[directory] = entry

    for entry in releases:
        item = reported[("release", entry["name"], entry["directory"])]
        what = "%s %s" % (LOCK_PATH, entry["name"])
        lock_entry = locked.get(entry["directory"])
        if lock_entry is None:
            raise ResolveError(
                "%s has no entry for release plugin %s" % (LOCK_PATH, entry["name"])
            )
        if item.get("repository") != entry["repository"]:
            raise ResolveError(
                "%s %s reports repository %r, configured %r"
                % (REPORT_PATH, entry["name"], item.get("repository"), entry["repository"])
            )
        for field in ("name", "directory", "version", "url", "sha256", "archive"):
            if item.get(field) != lock_entry[field]:
                raise ResolveError(
                    "%s %s is %r in %s but %r in %s"
                    % (
                        entry["name"],
                        field,
                        lock_entry[field],
                        LOCK_PATH,
                        item.get(field),
                        REPORT_PATH,
                    )
                )
        _text_field(item, "releaseTag", "%s %s" % (REPORT_PATH, entry["name"]))
        sha = _hex64(lock_entry["sha256"], "%s sha256" % what)

        archive_relative = lock_entry["archive"]
        try:
            with open(_inside(source, archive_relative), "rb") as handle:
                payload = handle.read()
        except OSError as error:
            raise ResolveError(
                "cannot read generated archive %s for %s: %s"
                % (archive_relative, entry["name"], error)
            )
        actual = _sha256(payload)
        if actual != sha:
            raise ResolveError(
                "generated archive %s for %s has sha256 %s, expected %s"
                % (archive_relative, entry["name"], actual, sha)
            )
        version = _archive_version(lock_entry["directory"], payload, archive_relative)
        if version != lock_entry["version"]:
            raise ResolveError(
                "generated archive %s for %s declares version %s inside the ZIP, "
                "expected %s"
                % (archive_relative, entry["name"], version, lock_entry["version"])
            )

    for entry in git_entries:
        item = reported[("git", entry["name"], entry["directory"])]
        if item.get("repository") != entry["repository"]:
            raise ResolveError(
                "%s %s reports repository %r, configured %r"
                % (REPORT_PATH, entry["name"], item.get("repository"), entry["repository"])
            )
        if item.get("branch") != entry["branch"]:
            raise ResolveError(
                "%s %s reports branch %r, configured %r"
                % (REPORT_PATH, entry["name"], item.get("branch"), entry["branch"])
            )
        _commit(item.get("commit"), "%s %s commit" % (REPORT_PATH, entry["name"]))
        version = _text_field(item, "version", "%s %s" % (REPORT_PATH, entry["name"]))
        package_relative = entry["packagePath"]
        package = _inside(_inside(source, entry["path"]), package_relative)
        actual = _version_of(_read_json(package, package_relative), package_relative)
        if actual != version:
            raise ResolveError(
                "%s reports version %s for git plugin %s but %s has %s"
                % (REPORT_PATH, version, entry["name"], package_relative, actual)
            )

    for entry in tree_entries:
        item = reported[("in-tree", entry["name"], entry["directory"])]
        commit = _commit(
            item.get("commit"), "%s %s commit" % (REPORT_PATH, entry["name"])
        )
        if commit != source_commit:
            raise ResolveError(
                "%s reports commit %s for in-tree plugin %s, expected the source "
                "commit %s" % (REPORT_PATH, commit, entry["name"], source_commit)
            )
        version = _text_field(item, "version", "%s %s" % (REPORT_PATH, entry["name"]))
        package_relative = entry["packagePath"]
        package = _inside(source, package_relative)
        actual = _version_of(_read_json(package, package_relative), package_relative)
        if actual != version:
            raise ResolveError(
                "%s reports version %s for in-tree plugin %s but %s has %s"
                % (REPORT_PATH, version, entry["name"], package_relative, actual)
            )

    if installed_root is not None:
        installed_root = os.fspath(installed_root)
        for kind, entry in declared:
            if kind == "release":
                continue
            item = reported[(kind, entry["name"], entry["directory"])]
            version = _text_field(item, "version", "%s %s" % (REPORT_PATH, entry["name"]))
            what = "installed %s/package.json" % entry["directory"]
            package = os.path.join(installed_root, entry["directory"], "package.json")
            actual = _version_of(_read_json(package, what), what)
            if actual != version:
                raise ResolveError(
                    "%s has version %s but %s resolved %s to %s"
                    % (what, actual, REPORT_PATH, entry["name"], version)
                )

    return lock


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Resolve the newest source for every bundled Decky plugin."
    )
    parser.add_argument(
        "--source",
        default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        help="checkout to resolve against (default: this script's repository)",
    )
    options = parser.parse_args(argv)

    try:
        report = resolve_plugin_sources(options.source)
    except (ResolveError, OSError, subprocess.SubprocessError) as error:
        print("plugin resolution failed: %s" % error, file=sys.stderr)
        return 1

    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
