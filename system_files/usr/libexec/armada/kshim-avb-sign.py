#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Replace an unsigned Android boot footer with a verified AVB signature.

The boot repacker intentionally emits an unsigned ``Algorithm: NONE`` footer.
This helper is the mandatory final stage for generated boot images: it copies
the image, erases the unsigned footer, signs the copy with SHA256_RSA4096,
checks the key fingerprint and payload bytes, and atomically replaces the
input only after verification succeeds.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

AVB_MAGIC = b"AVBf"
DEFAULT_ALGORITHM = "SHA256_RSA4096"
DEFAULT_KEY_PUBLIC_SHA256 = (
    "7728e30f50bfa5cea165f473175a08803f6a8346642b5aa10913e9d9e6defef6"
)
DEFAULT_KEY_PUBLIC_SHA1 = "2597c218aae470a130f61162feaae70afd97f011"
DEFAULT_RELEASE_STRING = "freedom-edl-direct"


class SigningError(Exception):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command, label):
    result = subprocess.run(
        command, capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise SigningError(
            "%s failed (%d)\nSTDOUT:\n%s\nSTDERR:\n%s"
            % (label, result.returncode, result.stdout, result.stderr)
        )
    return result.stdout


def load_avbtool(path):
    spec = importlib.util.spec_from_file_location("_kshim_avbtool", path)
    if spec is None or spec.loader is None:
        raise SigningError("cannot load avbtool: %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_stock_avb(avbtool, stock_boot):
    image = avbtool.ImageHandler(stock_boot, read_only=True)
    try:
        footer, header, descriptors, _ = avbtool.Avb()._parse_image(image)
    finally:
        image._image.close()

    if footer is None:
        raise SigningError("stock boot has no AVB footer")
    hashes = [
        item for item in descriptors
        if isinstance(item, avbtool.AvbHashDescriptor)
        and item.partition_name == "boot"
    ]
    if len(hashes) != 1:
        raise SigningError(
            "stock boot must contain exactly one boot hash descriptor"
        )
    hash_descriptor = hashes[0]
    properties = [
        item for item in descriptors
        if isinstance(item, avbtool.AvbPropertyDescriptor)
    ]
    return {
        "original_image_size": footer.original_image_size,
        "salt": bytes(hash_descriptor.salt).hex(),
        "hash_algorithm": hash_descriptor.hash_algorithm,
        "flags": hash_descriptor.flags,
        "rollback_index": header.rollback_index,
        "rollback_index_location": header.rollback_index_location,
        "release_string": header.release_string,
        "properties": [
            (item.key, bytes(item.value).decode("utf-8"))
            for item in properties
        ],
    }


def footer_values(image):
    if len(image) < 64:
        raise SigningError("image is too small for an AVB footer")
    magic, major, _minor, ois, vbo, vbs, _reserved = struct.unpack(
        ">4sIIQQQ28s", image[-64:]
    )
    if magic != AVB_MAGIC:
        raise SigningError("image has no AVB footer")
    if major != 1:
        raise SigningError("unsupported AVB footer major version")
    return ois, vbo, vbs


def extract_public_key(avbtool_py, key_path):
    fd, public_path = tempfile.mkstemp(prefix="kshim-avb-key-", suffix=".pub")
    os.close(fd)
    os.unlink(public_path)
    try:
        run(
            [
                sys.executable,
                "-B",
                avbtool_py,
                "extract_public_key",
                "--key",
                key_path,
                "--output",
                public_path,
            ],
            "extract_public_key",
        )
        return sha256_file(public_path)
    finally:
        try:
            os.unlink(public_path)
        except FileNotFoundError:
            pass


def parse_info(text):
    def one(pattern):
        match = re.search(pattern, text, re.M)
        if not match:
            raise SigningError("avbtool info_image output is missing %s" % pattern)
        return match.group(1)

    return {
        "algorithm": one(r"^\s*Algorithm:\s*(\S+)\s*$"),
        "public_key_sha1": one(r"^\s*Public key \(sha1\):\s*(\S+)\s*$").lower(),
        "release_string": one(r"^\s*Release String:\s*'(.*)'\s*$"),
    }


def sign(args):
    image = os.path.abspath(args.image)
    stock_boot = os.path.abspath(args.stock_boot)
    avbtool_py = os.path.abspath(args.avbtool)
    key = os.path.abspath(args.key)
    report_path = os.path.abspath(args.report) if args.report else None

    for label, path in (
        ("image", image),
        ("stock boot", stock_boot),
        ("avbtool", avbtool_py),
        ("key", key),
    ):
        if not os.path.isfile(path):
            raise SigningError("%s not found: %s" % (label, path))

    image_size = os.path.getsize(image)
    if image_size != args.partition_size:
        raise SigningError(
            "image size %d does not match partition size %d"
            % (image_size, args.partition_size)
        )

    avbtool = load_avbtool(avbtool_py)
    stock = parse_stock_avb(avbtool, stock_boot)
    if stock["hash_algorithm"] != "sha256":
        raise SigningError(
            "stock boot hash algorithm is %s, expected sha256"
            % stock["hash_algorithm"]
        )

    key_public_sha256 = extract_public_key(avbtool_py, key)
    if key_public_sha256 != args.expected_key_sha256.lower():
        raise SigningError(
            "AVB public key SHA256 mismatch: %s (expected %s)"
            % (key_public_sha256, args.expected_key_sha256.lower())
        )

    with open(image, "rb") as handle:
        input_bytes = handle.read()
    original_size, _vbmeta_offset, _vbmeta_size = footer_values(input_bytes)
    if original_size > image_size:
        raise SigningError("AVB original image size exceeds partition")
    before_sha = hashlib.sha256(input_bytes).hexdigest()
    payload = input_bytes[:original_size]

    output_dir = os.path.dirname(image)
    temporary_dir = tempfile.mkdtemp(prefix=".kshim-sign-", dir=output_dir)
    temporary = os.path.join(temporary_dir, "boot.img")
    try:
        shutil.copyfile(image, temporary)
        run(
            [
                sys.executable,
                "-B",
                avbtool_py,
                "erase_footer",
                "--image",
                temporary,
            ],
            "erase_footer",
        )

        command = [
            sys.executable,
            "-B",
            avbtool_py,
            "add_hash_footer",
            "--image",
            temporary,
            "--partition_size",
            str(args.partition_size),
            "--partition_name",
            args.partition_name,
            "--algorithm",
            args.algorithm,
            "--key",
            key,
            "--hash_algorithm",
            "sha256",
            "--salt",
            stock["salt"],
            "--rollback_index",
            str(stock["rollback_index"]),
            "--rollback_index_location",
            str(stock["rollback_index_location"]),
            "--internal_release_string",
            args.release_string,
            "--flags",
            str(stock["flags"]),
        ]
        for name, value in stock["properties"]:
            command.extend(("--prop", "%s:%s" % (name, value)))
        run(command, "add_hash_footer")

        run(
            [
                sys.executable,
                "-B",
                avbtool_py,
                "verify_image",
                "--image",
                temporary,
                "--key",
                key,
            ],
            "verify_image",
        )
        info = parse_info(
            run(
                [
                    sys.executable,
                    "-B",
                    avbtool_py,
                    "info_image",
                    "--image",
                    temporary,
                ],
                "info_image",
            )
        )
        if info["algorithm"] != args.algorithm:
            raise SigningError(
                "output algorithm %s, expected %s"
                % (info["algorithm"], args.algorithm)
            )
        if info["public_key_sha1"] != args.expected_key_sha1.lower():
            raise SigningError(
                "AVB public key SHA1 mismatch: %s" % info["public_key_sha1"]
            )
        if info["release_string"] != args.release_string:
            raise SigningError(
                "AVB release string %r, expected %r"
                % (info["release_string"], args.release_string)
            )

        with open(temporary, "rb") as handle:
            signed_bytes = handle.read()
        signed_original_size, _signed_vbo, _signed_vbs = footer_values(
            signed_bytes
        )
        if signed_original_size != original_size:
            raise SigningError(
                "signed original image size changed: %d -> %d"
                % (original_size, signed_original_size)
            )
        if signed_bytes[:original_size] != payload:
            raise SigningError("signing changed boot payload bytes")

        signed_stock = parse_stock_avb(avbtool, temporary)
        for field in (
            "salt",
            "hash_algorithm",
            "flags",
            "rollback_index",
            "rollback_index_location",
            "properties",
        ):
            if signed_stock[field] != stock[field]:
                raise SigningError(
                    "signed boot metadata changed: %s" % field
                )

        fd = os.open(temporary, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, image)
        directory_fd = os.open(
            output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)

    output_sha = sha256_file(image)
    report = {
        "schemaVersion": 1,
        "input_sha256": before_sha,
        "output_sha256": output_sha,
        "partition_size": args.partition_size,
        "original_image_size": original_size,
        "stock": {
            "salt": stock["salt"],
            "rollback_index": stock["rollback_index"],
            "rollback_index_location": stock["rollback_index_location"],
            "hash_algorithm": stock["hash_algorithm"],
            "flags": stock["flags"],
            "properties": [
                {"key": key_name, "value": value}
                for key_name, value in stock["properties"]
            ],
        },
        "avb": {
            "algorithm": info["algorithm"],
            "key_public_sha256": key_public_sha256,
            "key_public_sha1": info["public_key_sha1"],
            "release_string": info["release_string"],
            "signature_verified": True,
        },
    }
    if report_path:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")

    print(
        "signed %s: %s key_sha256=%s key_sha1=%s"
        % (image, output_sha, key_public_sha256, info["public_key_sha1"])
    )
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Force a verified AOSP AVB signature onto a boot image"
    )
    parser.add_argument("--image", required=True)
    parser.add_argument("--stock-boot", required=True)
    parser.add_argument("--avbtool", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--report")
    parser.add_argument("--partition-size", type=int, default=100663296)
    parser.add_argument("--partition-name", default="boot")
    parser.add_argument("--algorithm", default=DEFAULT_ALGORITHM)
    parser.add_argument("--release-string", default=DEFAULT_RELEASE_STRING)
    parser.add_argument(
        "--expected-key-sha256", default=DEFAULT_KEY_PUBLIC_SHA256
    )
    parser.add_argument("--expected-key-sha1", default=DEFAULT_KEY_PUBLIC_SHA1)
    args = parser.parse_args()
    try:
        return sign(args)
    except SigningError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
