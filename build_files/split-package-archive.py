#!/usr/bin/env python3
"""Split an already-built ZIP archive into raw byte volumes.

The archive is streamed once, in bounded chunks, into part files of exactly
part_bytes each (the last one holds the remainder). No recompression and no
byte change: the concatenation of the volumes is byte-identical to the input.
Each volume and the effective whole-archive SHA256 are computed in the same
pass, and a manifest is written last, only after complete success.
"""

import argparse
import hashlib
import json
import os
import sys

DEFAULT_PART_BYTES = 1992294400
MAX_PART_BYTES = 2147483647  # every volume must stay strictly below 2 GiB
CHUNK_BYTES = 1024 * 1024  # 1 MiB upper bound per read
FILE_MODE = 0o644
SCHEMA_VERSION = 1
FORMAT_NAME = "raw-split"


def _volume_name(base_name, index):
    return "%s.%03d" % (base_name, index)


def _manifest_name(base_name):
    return base_name + ".parts.json"


def _open_exclusive(path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    return os.open(path, flags, FILE_MODE)


def split_archive(input_path, output_dir, part_bytes=DEFAULT_PART_BYTES, expected_sha256=None):
    """Split input_path into output_dir/<basename>.001, .002, ...; return the manifest."""
    source = os.fspath(input_path)
    out_dir = os.fspath(output_dir)

    if (
        isinstance(part_bytes, bool)
        or not isinstance(part_bytes, int)
        or part_bytes <= 0
        or part_bytes > MAX_PART_BYTES
    ):
        raise ValueError(
            "part_bytes must be a positive integer below 2 GiB (max %d)" % MAX_PART_BYTES
        )
    if not os.path.isfile(source):
        raise ValueError("input is not a regular file: %s" % source)

    size = os.path.getsize(source)
    if size <= 0:
        raise ValueError("input is empty: %s" % source)

    base_name = os.path.basename(source)
    manifest_name = _manifest_name(base_name)
    part_count = (size + part_bytes - 1) // part_bytes
    targets = [_volume_name(base_name, i) for i in range(1, part_count + 1)]
    targets.append(manifest_name)

    os.makedirs(out_dir, exist_ok=True)
    for name in targets:
        if os.path.lexists(os.path.join(out_dir, name)):
            raise FileExistsError("target already exists: %s" % name)

    expected = None
    if expected_sha256 is not None:
        expected = str(expected_sha256).strip().lower()

    whole_hash = hashlib.sha256()
    parts = []
    created = []
    try:
        with open(source, "rb") as src:
            remaining = size
            index = 0
            while remaining > 0:
                index += 1
                name = _volume_name(base_name, index)
                dest = os.path.join(out_dir, name)
                limit = min(part_bytes, remaining)

                fd = _open_exclusive(dest)
                created.append(dest)
                os.chmod(dest, FILE_MODE)
                part_hash = hashlib.sha256()
                written = 0
                with os.fdopen(fd, "wb") as out:
                    while written < limit:
                        chunk = src.read(min(CHUNK_BYTES, limit - written))
                        if not chunk:
                            raise IOError("unexpected end of input: %s" % source)
                        out.write(chunk)
                        part_hash.update(chunk)
                        whole_hash.update(chunk)
                        written += len(chunk)

                parts.append({
                    "fileName": name,
                    "bytes": written,
                    "sha256": part_hash.hexdigest(),
                })
                remaining -= written

        whole_sha256 = whole_hash.hexdigest()
        if expected is not None and expected != whole_sha256:
            raise ValueError(
                "archive sha256 mismatch: expected %s, got %s" % (expected, whole_sha256)
            )

        manifest = {
            "schemaVersion": SCHEMA_VERSION,
            "format": FORMAT_NAME,
            "archive": {
                "fileName": base_name,
                "bytes": size,
                "sha256": whole_sha256,
            },
            "partSizeBytes": part_bytes,
            "parts": parts,
        }

        manifest_path = os.path.join(out_dir, manifest_name)
        fd = _open_exclusive(manifest_path)
        created.append(manifest_path)
        os.chmod(manifest_path, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(manifest, indent=2) + "\n")
    except BaseException:
        for path in reversed(created):
            try:
                os.unlink(path)
            except OSError:
                pass
        raise

    return manifest


def build_parser():
    parser = argparse.ArgumentParser(
        description="Split a built ZIP archive into raw byte volumes without recompression."
    )
    parser.add_argument("--input", required=True, help="existing ZIP archive to split")
    parser.add_argument("--output", required=True, help="directory that receives the volumes")
    parser.add_argument(
        "--part-bytes",
        type=int,
        default=DEFAULT_PART_BYTES,
        help="volume size in bytes (default: %d)" % DEFAULT_PART_BYTES,
    )
    parser.add_argument(
        "--expected-sha256",
        default=None,
        help="expected SHA256 of the whole archive; reject the split on mismatch",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        manifest = split_archive(
            args.input,
            args.output,
            part_bytes=args.part_bytes,
            expected_sha256=args.expected_sha256,
        )
    except Exception as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    sys.stdout.write(json.dumps(manifest, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
