"""Check a relocated bundle without importing its service from the checkout."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    bundle = Path(sys.argv[1]).resolve()
    resources = bundle / "Contents/Resources"
    configuration = json.loads((resources / "Runtime.json").read_text())
    assert configuration["format"] == "melt-runtime/v1"
    for field in ("repository", "python", "nodeDirectory", "ghosttyResources"):
        target = (resources / configuration[field]).resolve()
        assert target.is_relative_to(resources) and target.exists(), field
    manifest = json.loads((resources / "Package.json").read_text())
    interpreter = resources / configuration["python"]
    node = resources / configuration["nodeDirectory"] / "node"
    environment = dict(os.environ)
    environment["PATH"] = str(node.parent) + ":/usr/bin:/bin:/usr/sbin:/sbin"
    environment["PYTHONPATH"] = "/melt-checkout-must-not-be-imported"
    environment.pop("PYTHONHOME", None)
    with tempfile.TemporaryDirectory(prefix="melt-package-isolation-") as directory:
        script = """import importlib.metadata, json, pathlib, sys
import melt, runtime.desktop, pandas, pyarrow, matplotlib
root = pathlib.Path(sys.executable).resolve().parents[1]
assert all(pathlib.Path(module.__file__).resolve().is_relative_to(root)
           for module in (melt, runtime.desktop, pandas, pyarrow, matplotlib))
assert all(pathlib.Path(path).is_relative_to(root) for path in sys.path)
print(json.dumps({'python':sys.version.split()[0], 'packages': {
    name:importlib.metadata.version(name) for name in ('melt','pandas','pyarrow','matplotlib')}}))
"""
        result = subprocess.run([str(interpreter), "-I", "-B", "-c", script], env=environment,
            cwd=directory, check=True, capture_output=True, text=True)
        assert json.loads(result.stdout)["python"] == manifest["python"]
        version = subprocess.check_output([str(node), "--version"], env=environment, cwd=directory, text=True).strip()
        assert version == "v" + manifest["node"]
    magics = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}
    checked = 0
    for path in bundle.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as handle:
            if handle.read(4) not in magics:
                continue
        dependencies = subprocess.check_output(["otool", "-L", str(path)], text=True)
        for line in dependencies.splitlines()[1:]:
            if not line.startswith("\t"):
                continue  # Universal binaries print a header for each architecture.
            dependency = line.strip().split(" (", 1)[0]
            assert not dependency.startswith("/") or dependency.startswith(("/System/Library/", "/usr/lib/")), (path.relative_to(bundle), dependency)
        signature = subprocess.run(["codesign", "--verify", str(path)], capture_output=True, text=True)
        assert signature.returncode == 0, (str(path.relative_to(bundle)), signature.stderr)
        checked += 1
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)
    print(f"Relocated package passed isolated imports, pinned Node, {checked} Mach-O dependency/signature checks.")


if __name__ == "__main__":
    main()
