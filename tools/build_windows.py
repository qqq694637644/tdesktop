#!/usr/bin/env python3
"""GitHub Actions Windows build runner for Telegram Desktop.

This script intentionally mirrors the repository's documented Windows build
layout: a BuildPath directory that contains `tdesktop`, `Libraries` and
`ThirdParty`. The workflow calls this file in phases so GitHub Actions can
restore caches between environment discovery and the expensive prepare/build
steps.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
from typing import Iterable, Mapping, Sequence


SUPPORTED_ARCHITECTURES = {"x86", "x64", "arm64"}
SUPPORTED_CONFIGURATIONS = {"Debug", "Release", "RelWithDebInfo", "MinSizeRel"}


def log(message: str) -> None:
    print(f"[build-windows] {message}", flush=True)


def fail(message: str) -> None:
    raise SystemExit(f"[build-windows] ERROR: {message}")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def source_dir() -> Path:
    configured = env_value("TDESKTOP_SOURCE_DIR")
    return Path(configured).resolve() if configured else Path.cwd().resolve()


def tbuild_dir(src: Path) -> Path:
    configured = env_value("TBUILD")
    if configured:
        return Path(configured).resolve()
    # Expected source layout: <BuildPath>/tdesktop.
    return src.parent.resolve()


def require_file(path: Path, purpose: str) -> Path:
    if not path.is_file():
        fail(f"Missing {purpose}: {path}")
    return path


def require_dir(path: Path, purpose: str) -> Path:
    if not path.is_dir():
        fail(f"Missing {purpose}: {path}")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(parts: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def append_github_env(values: Mapping[str, str]) -> None:
    github_env = os.environ.get("GITHUB_ENV")
    for key, value in values.items():
        log(f"env {key}={value}")
    if not github_env:
        return
    with open(github_env, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def redact_command(args: Sequence[str | Path]) -> str:
    redacted: list[str] = []
    secret_values = [
        env_value("TDESKTOP_API_ID"),
        env_value("TDESKTOP_API_HASH"),
    ]
    for arg in args:
        text = str(arg)
        for secret in secret_values:
            if secret:
                text = text.replace(secret, "***")
        if text.startswith("TDESKTOP_API_ID="):
            text = "TDESKTOP_API_ID=***"
        elif text.startswith("TDESKTOP_API_HASH="):
            text = "TDESKTOP_API_HASH=***"
        redacted.append(text)
    return subprocess.list2cmdline(redacted)


def run(args: Sequence[str | Path], cwd: Path | None = None) -> None:
    log(f"run: {redact_command(args)}")
    subprocess.run([str(arg) for arg in args], cwd=str(cwd) if cwd else None, check=True)


def run_batch(args: Sequence[str | Path], cwd: Path | None = None) -> None:
    command_line = subprocess.list2cmdline([str(arg) for arg in args])
    log(f"run: call {redact_command(args)}")
    subprocess.run(f"call {command_line}", cwd=str(cwd) if cwd else None, shell=True, check=True)


def capture(args: Sequence[str | Path], cwd: Path | None = None) -> str:
    log(f"capture: {redact_command(args)}")
    completed = subprocess.run(
        [str(arg) for arg in args],
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return completed.stdout.strip()


def architecture() -> str:
    value = env_value("TDESKTOP_ARCHITECTURE", "x64")
    if value not in SUPPORTED_ARCHITECTURES:
        fail(f"Unsupported architecture '{value}'. Use one of: {', '.join(sorted(SUPPORTED_ARCHITECTURES))}")
    return value


def configuration() -> str:
    value = env_value("TDESKTOP_CONFIGURATION", "Release")
    if value not in SUPPORTED_CONFIGURATIONS:
        fail(f"Unsupported configuration '{value}'. Use one of: {', '.join(sorted(SUPPORTED_CONFIGURATIONS))}")
    return value


def qt_flavor() -> str:
    value = env_value("TDESKTOP_QT_FLAVOR", "default").lower()
    if value in {"", "default", "qt5", "legacy"}:
        return ""
    if value == "qt6":
        return "qt6"
    fail("Unsupported Qt flavor. Use 'qt6' or 'qt5'.")
    return ""


def generator() -> str:
    value = env_value("TDESKTOP_GENERATOR", "default")
    if value.lower() in {"", "default", "visual studio"}:
        return ""
    return value


def configure_arch_arg(arch: str, gen: str) -> str:
    if gen:
        # Ninja uses the architecture selected by the initialized VS environment.
        return ""
    return {"x86": "x86", "x64": "x64", "arm64": "arm"}[arch]


def msvc_arch_arg(arch: str) -> str:
    return {"x86": "x64_x86", "x64": "x64", "arm64": "arm64"}[arch]


def libraries_path(build_root: Path, arch: str) -> Path:
    # Keep the same convention as the repository's existing win.yml:
    # x64 dependencies live under Libraries\win64, x86/arm under Libraries.
    base = build_root / "Libraries"
    return base / "win64" if arch == "x64" else base


def read_sdk_version(src: Path) -> str:
    doc = require_file(src / "docs" / "building-win.md", "Windows build documentation")
    text = doc.read_text(encoding="utf-8")
    match = re.search(r"\*\*([^*]+)\*\* SDK version", text)
    if not match:
        fail(f"Could not read Windows SDK version from {doc}")
    return match.group(1)


def read_msvc_toolset(src: Path) -> str:
    doc = require_file(src / "docs" / "building-win.md", "Windows build documentation")
    text = doc.read_text(encoding="utf-8")
    match = re.search(r"-vcvars_ver=([0-9.]+)", text)
    if not match:
        fail(f"Could not read MSVC vcvars toolset from {doc}")
    return match.group(1)


def required_build_files(src: Path) -> list[Path]:
    return [
        require_file(src / "Telegram" / "build" / "prepare" / "prepare.py", "prepare.py"),
        require_file(src / "Telegram" / "build" / "prepare" / "win.bat", "Windows prepare script"),
        require_file(src / "Telegram" / "build" / "qt_version.py", "Qt version resolver"),
        require_file(src / "docs" / "building-win.md", "Windows build documentation"),
    ]


def official_parameters() -> bool:
    return env_bool("TDESKTOP_OFFICIAL_PARAMETERS", True)


def python_version() -> str:
    return ".".join(str(part) for part in sys.version_info[:3])


def cache_key(src: Path, sdk: str, toolset: str, arch: str, qt: str, gen: str) -> str:
    parts = [
        sdk,
        toolset,
        arch,
        qt or "default",
        gen or "default",
        f"official={official_parameters()}",
        f"python={python_version()}",
    ]
    for path in required_build_files(src):
        parts.append(path.as_posix())
        parts.append(sha256_file(path))
    return sha256_text(parts)[:32]


def build_metadata() -> dict[str, str]:
    src = source_dir()
    build_root = tbuild_dir(src)
    arch = architecture()
    config = configuration()
    qt = qt_flavor()
    gen = generator()
    sdk = read_sdk_version(src)
    toolset = read_msvc_toolset(src)
    required_build_files(src)

    third_party = build_root / "ThirdParty"
    libs = libraries_path(build_root, arch)
    third_party.mkdir(parents=True, exist_ok=True)
    libs.mkdir(parents=True, exist_ok=True)

    artifact_bits = ["Telegram", arch, config]
    artifact_bits.append(qt or "default")
    if gen:
        artifact_bits.append(gen.replace(" ", "-"))

    return {
        "TBUILD": str(build_root),
        "TDESKTOP_SOURCE_DIR": str(src),
        "TDESKTOP_THIRD_PARTY_PATH": str(third_party),
        "LibrariesPath": str(libs),
        "SDK": sdk,
        "MSVC_TOOLSET": toolset,
        "MSVC_ARCH": msvc_arch_arg(arch),
        "TDESKTOP_CONFIGURE_ARCH": configure_arch_arg(arch, gen),
        "TDESKTOP_RESOLVED_QT": qt,
        "TDESKTOP_RESOLVED_GENERATOR": gen,
        "TDESKTOP_ARTIFACT_NAME": "-".join(artifact_bits),
        "TDESKTOP_CACHE_KEY": cache_key(src, sdk, toolset, arch, qt, gen),
    }


def phase_github_env() -> None:
    append_github_env(build_metadata())


def phase_diagnose() -> None:
    src = source_dir()
    log(f"source: {src}")
    log(f"tbuild: {tbuild_dir(src)}")
    for command in (["python", "--version"], ["cmake", "--version"], ["ninja", "--version"]):
        try:
            output = capture(command)
            print(output)
        except (FileNotFoundError, subprocess.CalledProcessError):
            log(f"diagnostic command failed or not found: {subprocess.list2cmdline(command)}")
    try:
        print(capture(["where", "cl"]))
        print(capture(["cl", "/Bv"]))
    except (FileNotFoundError, subprocess.CalledProcessError):
        log("MSVC compiler is not visible yet.")


def phase_patch_cmake_msvc() -> None:
    program_files = os.environ.get("PROGRAMFILES")
    if not program_files:
        log("PROGRAMFILES is not set; skipping CMake MSVC debug flag patch.")
        return
    cmake_root = Path(program_files) / "CMake" / "share"
    candidates = sorted(cmake_root.glob("cmake*/Modules/Platform/Windows-MSVC.cmake"))
    if not candidates:
        log(f"No Windows-MSVC.cmake found under {cmake_root}; skipping patch.")
        return

    for path in candidates:
        text = path.read_text(encoding="utf-8")
        patched_lines = []
        changed = False
        for line in text.splitlines(keepends=True):
            if "CMAKE_${lang}_FLAGS_DEBUG_INIT" in line and "${_Zi}" in line:
                line = line.replace("${_Zi}", "")
                changed = True
            patched_lines.append(line)
        if changed:
            path.write_text("".join(patched_lines), encoding="utf-8")
            log(f"patched {path}")
        else:
            log(f"no patch needed for {path}")


def phase_prepare() -> None:
    src = source_dir()
    build_root = tbuild_dir(src)
    arch = architecture()
    qt = qt_flavor()
    require_dir(build_root, "BuildPath")
    required_build_files(src)

    command: list[str | Path] = [src / "Telegram" / "build" / "prepare" / "win.bat"]
    if not official_parameters():
        command.extend(["skip-release", "silent"])
    if qt:
        command.append(qt)
    run_batch(command, cwd=build_root)

    libs = libraries_path(build_root, arch)
    require_dir(libs, "prepared Libraries directory")


def api_arguments() -> list[str]:
    if env_bool("TDESKTOP_USE_TEST_API"):
        return ["-D", "TDESKTOP_API_TEST=ON"]

    api_id = env_value("TDESKTOP_API_ID")
    api_hash = env_value("TDESKTOP_API_HASH")
    if not api_id or not api_hash:
        fail(
            "TDESKTOP_API_ID and TDESKTOP_API_HASH are not set. "
            "Add repository secrets with those names or run the workflow with use_test_api=true."
        )
    return ["-D", f"TDESKTOP_API_ID={api_id}", "-D", f"TDESKTOP_API_HASH={api_hash}"]


def extra_cmake_arguments() -> list[str]:
    extra = env_value("TDESKTOP_EXTRA_CMAKE_ARGS")
    if not extra:
        return []
    return shlex.split(extra, posix=False)


def tag_input() -> str:
    return env_value("TDESKTOP_TAG_INPUT")


def patch_v701_cmake_helpers(src: Path) -> None:
    if tag_input() != "v7.0.1":
        return

    run_cmake = require_file(src / "cmake" / "run_cmake.py", "cmake helper run_cmake.py")
    text = run_cmake.read_text(encoding="utf-8")
    old = "cmake.extend(['-Werror=dev', '-Werror=deprecated', '--warn-uninitialized', '..' if not buildType else '../..'])"
    new = "cmake.extend(['--warn-uninitialized', '..' if not buildType else '../..'])"
    if old not in text:
        if "-Werror=dev" not in text and "-Werror=deprecated" not in text:
            log("v7.0.1 CMake helper warning-as-error patch is already applied.")
            return
        fail(
            "Could not apply the v7.0.1 CMake helper warning-as-error patch. "
            f"Unexpected contents in {run_cmake}."
        )

    run_cmake.write_text(text.replace(old, new, 1), encoding="utf-8")
    log(f"patched v7.0.1 CMake helper warning-as-error flags in {run_cmake}")


def phase_build() -> None:
    src = source_dir()
    config = configuration()
    arch_arg = env_value("TDESKTOP_CONFIGURE_ARCH") or configure_arch_arg(architecture(), generator())
    qt = env_value("TDESKTOP_RESOLVED_QT") or qt_flavor()
    gen = env_value("TDESKTOP_RESOLVED_GENERATOR") or generator()
    telegram_dir = require_dir(src / "Telegram", "Telegram source directory")
    configure_bat = require_file(telegram_dir / "configure.bat", "configure.bat")
    patch_v701_cmake_helpers(src)

    configure_args: list[str | Path] = [configure_bat]
    if gen:
        configure_args.extend(["-G", gen])
    if arch_arg:
        configure_args.append(arch_arg)
    if qt:
        configure_args.append(qt)
    configure_args.extend(api_arguments())
    if not official_parameters():
        configure_args.extend(
            [
                "-D",
                f"CMAKE_CONFIGURATION_TYPES={config}",
                "-D",
                "CMAKE_COMPILE_WARNING_AS_ERROR=ON",
                "-D",
                "CMAKE_MSVC_DEBUG_INFORMATION_FORMAT=",
                "-D",
                "DESKTOP_APP_DISABLE_AUTOUPDATE=OFF",
                "-D",
                "DESKTOP_APP_DISABLE_CRASH_REPORTS=OFF",
            ]
        )
    configure_args.extend(extra_cmake_arguments())

    run_batch(configure_args, cwd=telegram_dir)
    run(["cmake", "--build", src / "out", "--config", config, "--parallel"], cwd=telegram_dir)


def file_hashes(paths: Iterable[Path]) -> dict[str, str]:
    return {path.name: sha256_file(path) for path in paths}


def phase_artifact() -> None:
    src = source_dir()
    config = configuration()
    output_dir = src / "out" / config
    artifact_dir = src / "artifact"
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir)
    artifact_dir.mkdir(parents=True)

    required_outputs = [output_dir / "Telegram.exe", output_dir / "Updater.exe"]
    for path in required_outputs:
        require_file(path, "build output")
        shutil.copy2(path, artifact_dir / path.name)
        log(f"staged {path.name}")

    commit = "unknown"
    try:
        commit = capture(["git", "rev-parse", "HEAD"], cwd=src)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    metadata = build_metadata()
    manifest = {
        "commit": commit,
        "configuration": config,
        "architecture": architecture(),
        "qt": metadata["TDESKTOP_RESOLVED_QT"] or "default",
        "generator": metadata["TDESKTOP_RESOLVED_GENERATOR"] or "default",
        "official_parameters": official_parameters(),
        "outputs": file_hashes(required_outputs),
        "note": "Unsigned GitHub Actions build; byte identity with Telegram official releases is not guaranteed.",
    }
    (artifact_dir / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    with (artifact_dir / "SHA256SUMS.txt").open("w", encoding="utf-8") as handle:
        for filename, digest in manifest["outputs"].items():
            handle.write(f"{digest}  {filename}\n")

    append_github_env({"TDESKTOP_ARTIFACT_DIR": str(artifact_dir)})


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Telegram Desktop for Windows in GitHub Actions.")
    parser.add_argument(
        "phase",
        choices=["github-env", "diagnose", "patch-cmake-msvc", "prepare", "build", "artifact"],
        help="Workflow phase to execute.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    phases = {
        "github-env": phase_github_env,
        "diagnose": phase_diagnose,
        "patch-cmake-msvc": phase_patch_cmake_msvc,
        "prepare": phase_prepare,
        "build": phase_build,
        "artifact": phase_artifact,
    }
    phases[args.phase]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
