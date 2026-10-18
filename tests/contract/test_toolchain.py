"""Toolchain pins for the Linux round (backlog task T001).

These tests prove that the interpreter, the direct dependencies, and
the committed lockfile agree. They fail when the environment drifts
away from `pyproject.toml` and `uv.lock`.
"""

from __future__ import annotations

import importlib.metadata
import re
import sys
import tomllib

# Apple and Ghostty frameworks must stay out of round-1 sources
# (SPEC.md section 2.2). Round 2 introduces the native host.
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {"AppKit", "Cocoa", "Foundation", "Ghostty", "Quartz", "SwiftUI", "objc"}
)

_IMPORT_LINE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)")


def _load_toml(repo_root, name):
    with (repo_root / name).open("rb") as handle:
        return tomllib.load(handle)


def _pinned_dependencies(repo_root) -> dict[str, str]:
    """Return every exactly pinned dependency from pyproject.toml."""
    project = _load_toml(repo_root, "pyproject.toml")
    requirements = list(project["project"]["dependencies"])
    for group in project.get("dependency-groups", {}).values():
        requirements.extend(group)

    pins: dict[str, str] = {}
    for requirement in requirements:
        name, separator, version = requirement.partition("==")
        assert separator, f"dependency {requirement!r} is not pinned exactly"
        assert name not in pins, f"dependency {name!r} is pinned twice"
        pins[name] = version
    return pins


def test_interpreter_is_the_pinned_generation(repo_root):
    project = _load_toml(repo_root, "pyproject.toml")["project"]
    assert project["requires-python"] == ">=3.12,<3.13"
    assert sys.version_info[:2] == (3, 12), (
        f"round 1 pins Python 3.12, found {sys.version.split()[0]}"
    )


def test_required_dependencies_are_pinned(repo_root):
    pins = _pinned_dependencies(repo_root)
    # The five dependencies named by backlog task T001.
    assert set(pins) == {"pydantic", "pyarrow", "pandas", "matplotlib", "pytest"}


def test_installed_versions_match_the_pins(repo_root):
    pins = _pinned_dependencies(repo_root)
    drift = []
    for name, pinned in sorted(pins.items()):
        installed = importlib.metadata.version(name)
        if installed != pinned:
            drift.append(f"{name}: installed {installed}, pinned {pinned}")
    assert not drift, "environment does not match pyproject.toml:\n" + "\n".join(drift)


def test_lockfile_exists_and_pins_the_same_versions(repo_root):
    lock_path = repo_root / "uv.lock"
    assert lock_path.is_file(), "uv.lock is missing; run `make lock`"

    lock = _load_toml(repo_root, "uv.lock")
    locked = {package["name"]: package["version"] for package in lock["package"]}

    drift = []
    for name, pinned in sorted(_pinned_dependencies(repo_root).items()):
        if name not in locked:
            drift.append(f"{name}: absent from uv.lock")
        elif locked[name] != pinned:
            drift.append(f"{name}: lock has {locked[name]}, pyproject pins {pinned}")
    assert not drift, "uv.lock disagrees with pyproject.toml:\n" + "\n".join(drift)


def test_lockfile_records_hashes(repo_root):
    """Every registry package in uv.lock carries a content hash."""
    lock = _load_toml(repo_root, "uv.lock")
    without_hash = []
    for package in lock["package"]:
        source = package.get("source")
        if isinstance(source, dict):
            from_registry = "registry" in source
        else:
            from_registry = source in (None, "registry")
        if not from_registry:
            continue
        sdist_hash = package.get("sdist", {}).get("hash")
        wheel_hashes = [wheel.get("hash") for wheel in package.get("wheels", [])]
        if not sdist_hash and not any(wheel_hashes):
            without_hash.append(package["name"])
    assert without_hash == [], (
        "lock entries without content hashes: " + ", ".join(without_hash)
    )


def test_round1_sources_import_no_apple_frameworks(repo_root):
    offenders = []
    for base in ("python", "runtime", "scripts"):
        for path in sorted((repo_root / base).rglob("*.py")):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                match = _IMPORT_LINE.match(line)
                if match is None:
                    continue
                root = match.group(1).split(".")[0]
                if root in FORBIDDEN_IMPORT_ROOTS:
                    offenders.append(
                        f"{path.relative_to(repo_root)}:{line_number}: {line.strip()}"
                    )
    assert not offenders, (
        "round-1 sources must not import Apple or Ghostty frameworks:\n"
        + "\n".join(offenders)
    )


def test_check_script_is_executable(repo_root):
    check = repo_root / "scripts" / "check.sh"
    assert check.is_file()
    assert check.stat().st_mode & 0o111, "scripts/check.sh must stay executable"


def test_single_check_command_is_documented(repo_root):
    """The one-command entry point stays in README.md and the Makefile."""
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    assert "make check" in readme
    assert "scripts/check.sh" in readme
