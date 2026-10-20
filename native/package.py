"""Build a relocatable local macOS bundle; publishing/signing identity is separate."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "build" / "package"
APP = PACKAGE / "Melt.app"
NODE_VERSION = "22.19.0"
NODE_HASHES = {
    "arm64": "c59006db713c770d6ec63ae16cb3edc11f49ee093b5c415d667bb4f436c6526d",
    "x86_64": "3cfed4795cd97277559763c5f56e711852d2cc2420bda1cea30c8aa9ac77ce0c",
}


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def copy_locked_dependencies(destination):
    """Copy the locked environment, verifying installed wheel RECORD hashes."""
    import base64
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    project = tomllib.loads((REPO / "pyproject.toml").read_text())
    pending = list(project["project"]["dependencies"])
    needed, manifest = set(), []
    while pending:
        requirement = Requirement(pending.pop())
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        if name in needed:
            continue
        needed.add(name)
        distribution = importlib.metadata.distribution(name)
        package = next(p for p in lock["package"] if p["name"] == name and p["version"] == distribution.version)
        pending.extend(distribution.requires or [])
        installed = Path(distribution.locate_file("")).resolve()
        if installed != (REPO / ".venv/lib/python3.12/site-packages").resolve():
            raise SystemExit(f"{name} did not come from the locked project environment.")
        copied = []
        for entry in distribution.files or []:
            if entry.suffix == ".pyc":
                continue
            source = Path(distribution.locate_file(entry)).resolve()
            if not source.is_relative_to(installed):
                continue  # Installer-created console scripts are not runtime imports.
            relative = source.relative_to(installed)
            data = source.read_bytes()
            if entry.hash:
                actual = base64.urlsafe_b64encode(hashlib.new(entry.hash.mode, data).digest()).rstrip(b"=").decode()
                if actual != entry.hash.value:
                    raise SystemExit(f"Installed file failed its wheel RECORD check: {name}/{relative}")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(f"{relative}:{hashlib.sha256(data).hexdigest()}")
        manifest.append({"name": name, "version": distribution.version, "verifiedFiles": len(copied),
            "preSigningTreeSha256": hashlib.sha256("\n".join(sorted(copied)).encode()).hexdigest(),
            "lockedWheelHashes": [wheel["hash"] for wheel in package.get("wheels", [])]})
    return sorted(manifest, key=lambda item: item["name"])


def main():
    if sys.platform != "darwin" or sys.version_info[:3] != (3, 12, 3):
        raise SystemExit("Packaging requires macOS and the managed Python 3.12.3 interpreter.")
    python_root = Path(sys.base_prefix).resolve()
    if not (python_root / "BUILD").is_file():
        raise SystemExit("Use uv's relocatable managed Python (UV_PYTHON_PREFERENCE=only-managed).")
    os.chdir(REPO)
    run("bash", "native/build.sh")
    PACKAGE.mkdir(parents=True, exist_ok=True)
    if APP.exists():
        shutil.rmtree(APP)
    shutil.copytree(REPO / "build/native/Melt.app", APP, symlinks=True)
    resources = APP / "Contents/Resources"
    destination = resources / "Python"
    shutil.copytree(python_root, destination, symlinks=True)
    interpreter = destination / "bin/python3.12"
    # Every executable link must remain within the copied runtime.
    for link in destination.rglob("*"):
        if link.is_symlink() and os.path.isabs(os.readlink(link)):
            target = link.resolve()
            relative = target.relative_to(python_root)
            link.unlink(); link.symlink_to(os.path.relpath(destination / relative, link.parent))
    requirements = PACKAGE / "requirements.txt"
    run("uv", "export", "--locked", "--no-dev", "--no-emit-project", "--format", "requirements-txt", "--output-file", requirements)
    dependencies = copy_locked_dependencies(destination / "lib/python3.12/site-packages")
    run("uv", "build", "--wheel", "--out-dir", PACKAGE / "wheels")
    wheel = next((PACKAGE / "wheels").glob("melt-0.1.0-*.whl"))
    import zipfile
    with zipfile.ZipFile(wheel) as source:
        if any(Path(name).is_absolute() or ".." in Path(name).parts for name in source.namelist()):
            raise SystemExit("The application wheel contains an unsafe path.")
        source.extractall(destination / "lib/python3.12/site-packages")
    runtime = destination / "lib/python3.12/site-packages/runtime"
    for adapter in [runtime / "storage/pi_adapter", runtime / "rendering/pele_adapter"]:
        run("npm", "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund", cwd=adapter)
    arch = platform.machine()
    node_arch = "arm64" if arch == "arm64" else "x64"
    archive = PACKAGE / f"node-v{NODE_VERSION}-darwin-{node_arch}.tar.gz"
    expected = NODE_HASHES[arch]
    if not archive.is_file() or digest(archive) != expected:
        run("curl", "-4", "--fail", "--location", f"https://nodejs.org/dist/v{NODE_VERSION}/{archive.name}", "--output", archive)
    if digest(archive) != expected:
        raise SystemExit("The pinned Node archive did not match its SHA-256 hash.")
    with tarfile.open(archive) as source:
        source.extractall(PACKAGE / "node", filter="data")
    node = PACKAGE / "node" / archive.name.removesuffix(".tar.gz")
    (resources / "Node/bin").mkdir(parents=True)
    shutil.copy2(node / "bin/node", resources / "Node/bin/node")
    (resources / "Licenses").mkdir(exist_ok=True)
    shutil.copy2(node / "LICENSE", resources / "Licenses/Node.txt")
    (resources / "Runtime").mkdir()
    from runtime.execution.environment import MODEL_CREDENTIAL_VARIABLES, CREDENTIAL_SUFFIXES
    configuration = {"format": "melt-runtime/v1", "repository": "Runtime",
        "python": "Python/bin/python3.12", "nodeDirectory": "Node/bin", "ghosttyResources": "Ghostty",
        "credentialNames": sorted(MODEL_CREDENTIAL_VARIABLES), "credentialSuffixes": CREDENTIAL_SUFFIXES}
    (resources / "Runtime.json").write_text(json.dumps(configuration, indent=2) + "\n")
    # Entry-point shebangs from installers are absolute. The app invokes the
    # interpreter directly; keep only its relocatable executable and aliases.
    for entry in (destination / "bin").iterdir():
        if entry.name not in {"python3.12", "python3", "python"}:
            entry.unlink()
    for name in ["include", "share/man"]:
        shutil.rmtree(destination / name, ignore_errors=True)
    for path in destination.rglob("*.a"):
        path.unlink()
    # Sign every copied Mach-O locally before signing the outer bundle.
    magics = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}
    for path in resources.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                macho = handle.read(4) in magics
            if macho:
                identifiers = [line.strip() for line in subprocess.check_output(
                    ["otool", "-D", str(path)], text=True).splitlines() if line and not line.endswith(":")]
                if any(value.startswith("/") and not value.startswith(("/usr/lib/", "/System/Library/")) for value in identifiers):
                    run("install_name_tool", "-id", "@rpath/" + path.name, path)
                run("codesign", "--force", "--sign", "-", path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    manifest = {"format": "melt-package/v1", "architecture": arch, "python": platform.python_version(),
        "pythonBuild": (python_root / "BUILD").read_text().strip(), "node": NODE_VERSION,
        "nodeArchiveSha256": expected, "wheelSha256": digest(wheel), "dependencies": dependencies,
        "ghosttyRevision": (REPO / "build/dependencies/ghostty/macos/GhosttyKit.xcframework/melt-revision").read_text().strip(),
        "signing": "ad-hoc; Developer ID and notarization have not been performed"}
    (resources / "Package.json").write_text(json.dumps(manifest, indent=2) + "\n")
    run("codesign", "--force", "--sign", "-", APP)
    run("codesign", "--verify", "--deep", "--strict", APP)
    # Import in isolated mode from a working directory outside the checkout.
    run(interpreter, "-I", "-B", "-c", "import melt, runtime.desktop, pyarrow, pandas, matplotlib; print('Bundled runtime imports passed')", cwd=PACKAGE)
    run("codesign", "--verify", "--deep", "--strict", APP)
    print(APP)


if __name__ == "__main__":
    main()
