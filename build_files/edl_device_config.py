#!/usr/bin/env python3
"""Configuration-driven EDL device profiles.

The builder reads the declarative ``edl-devices.json`` document through this
module, validates it and answers the questions the build pipeline asks about a
model.

Public surface:

    load_configuration(path)
        Return the validated configuration document unchanged:
        {"schemaVersion": 1, "runtimeSource": <path>, "profiles": {MODEL: PROFILE}}
        with profiles, ``required``, ``forbidden``, ``extraArgs`` and
        ``runtimeCmakeArgs`` preserving document order, and plain JSON lists
        rather than tuples/frozensets. Unsafe or malformed documents raise
        ValueError.

    select_models(configuration, requested=None)
        Return a tuple of model identifiers in configuration order. ``requested``
        None selects every profile; an explicit list is rejected (ValueError)
        when empty, or when it names an unknown or duplicated model.

Command line:

    python build_files/edl_device_config.py --config PATH \
        --format {models,runtime-source,runtime-plan,runtime-args,readme,json} \
        [--model ID ...]

The module is standard-library only and stays importable without GI or OSTree.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCHEMA_VERSION = 1
DIAGNOSTIC_MODES = ("rd-break", "verbose")
CLI_FORMATS = (
    "models",
    "runtime-source",
    "runtime-plan",
    "runtime-args",
    "readme",
    "json",
)

STRING_LIST_FIELDS = ("required", "forbidden", "extraArgs", "runtimeCmakeArgs")


def _has_control_characters(value):
    return any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)


def _plain_text(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a nonempty string")
    if _has_control_characters(value):
        raise ValueError(f"{field} must not contain control characters")
    return value


def _relative_path(value, field):
    _plain_text(value, field)
    if value.startswith("/"):
        raise ValueError(f"{field} must be a relative path")
    if "\\" in value:
        raise ValueError(f"{field} must not contain backslashes")
    for segment in value.split("/"):
        if segment in ("", ".", ".."):
            raise ValueError(f"{field} must not contain empty, '.' or '..' segments")
    return value


def _model_id(value, field):
    _plain_text(value, field)
    if value in (".", ".."):
        raise ValueError(f"{field} must not be '.' or '..'")
    if "/" in value or "\\" in value:
        raise ValueError(f"{field} must be a single path component")
    return value


def _string_list(value, field):
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a JSON list of strings")
    for index, item in enumerate(value):
        _plain_text(item, f"{field}[{index}]")
    return value


def _validate_profile(model, profile):
    if not isinstance(profile, dict):
        raise ValueError(f"profile {model!r} must be a JSON object")

    _plain_text(profile.get("displayName"), f"profiles.{model}.displayName")

    compatible = profile.get("compatible")
    if not isinstance(compatible, dict):
        raise ValueError(f"profiles.{model}.compatible must be a JSON object")
    _plain_text(compatible.get("soc"), f"profiles.{model}.compatible.soc")
    _plain_text(compatible.get("device"), f"profiles.{model}.compatible.device")

    _relative_path(profile.get("dtb"), f"profiles.{model}.dtb")
    _relative_path(profile.get("runtimeConfig"), f"profiles.{model}.runtimeConfig")

    for field in STRING_LIST_FIELDS:
        _string_list(profile.get(field), f"profiles.{model}.{field}")

    budget = profile.get("bootPartitionBytes")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise ValueError(f"profiles.{model}.bootPartitionBytes must be a positive integer")

    mode = profile.get("diagnosticMode")
    if not isinstance(mode, str) or mode not in DIAGNOSTIC_MODES:
        raise ValueError(
            f"profiles.{model}.diagnosticMode must be one of {', '.join(DIAGNOSTIC_MODES)}"
        )


def load_configuration(path):
    """Return the validated configuration document loaded from *path*.

    The result is {"schemaVersion": 1, "runtimeSource": str,
    "profiles": {MODEL: PROFILE}}; profile lists stay lists in document order
    and every profile carries displayName, compatible{soc,device}, dtb,
    required, forbidden, extraArgs, runtimeConfig, runtimeCmakeArgs,
    bootPartitionBytes and diagnosticMode. Raises ValueError when the document
    is unsafe or malformed.
    """
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)

    if not isinstance(document, dict):
        raise ValueError("configuration root must be a JSON object")

    version = document.get("schemaVersion")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise ValueError(f"schemaVersion must be the integer {SCHEMA_VERSION}")

    _relative_path(document.get("runtimeSource"), "runtimeSource")

    profiles = document.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("profiles must be a nonempty JSON object")
    for model, profile in profiles.items():
        _model_id(model, f"profiles key {model!r}")
        _validate_profile(model, profile)

    return document


def select_models(configuration, requested=None):
    """Return the selected model identifiers as a tuple, in configuration order.

    ``requested`` None selects every profile in the configuration. An explicit
    list is validated first: an empty list, an unknown identifier and a
    duplicated identifier all raise ValueError.
    """
    profiles = configuration["profiles"]
    if requested is None:
        return tuple(profiles)

    try:
        items = list(requested)
    except TypeError as error:
        raise ValueError("requested models must be a sequence of model identifiers") from error

    if not items:
        raise ValueError("at least one model must be selected")

    selected = set()
    for item in items:
        if not isinstance(item, str):
            raise ValueError(
                f"model identifiers must be strings, got {type(item).__name__}"
            )
        if item not in profiles:
            raise ValueError(f"unknown model {item!r}")
        if item in selected:
            raise ValueError(f"model {item!r} selected more than once")
        selected.add(item)

    return tuple(model for model in profiles if model in selected)


def _output_lines(configuration, models, output_format):
    profiles = configuration["profiles"]

    if output_format == "models":
        return list(models)
    if output_format == "runtime-source":
        return [configuration["runtimeSource"]]
    if output_format == "runtime-plan":
        return [f"{model}\t{profiles[model]['runtimeConfig']}" for model in models]
    if output_format == "runtime-args":
        if len(models) != 1:
            raise ValueError(
                f"--format runtime-args requires exactly one selected model, got {len(models)}"
            )
        return list(profiles[models[0]]["runtimeCmakeArgs"])
    if output_format == "readme":
        return [f"{profiles[model]['displayName']} ({model})" for model in models]

    document = dict(configuration)
    document["profiles"] = {model: profiles[model] for model in models}
    return [json.dumps(document, indent=2)]


def main(argv=None):
    """CLI entry point; returns a process status (0 on success, nonzero on error)."""
    parser = argparse.ArgumentParser(
        description="Inspect the declarative EDL device profiles."
    )
    parser.add_argument("--config", required=True, type=Path, metavar="PATH")
    parser.add_argument("--format", required=True, choices=CLI_FORMATS)
    parser.add_argument("--model", action="append", metavar="ID")
    arguments = parser.parse_args(argv)

    try:
        configuration = load_configuration(arguments.config)
        models = select_models(configuration, arguments.model)
        lines = _output_lines(configuration, models, arguments.format)
    except (OSError, ValueError) as error:
        print(f"edl_device_config: error: {error}", file=sys.stderr)
        return 1

    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
