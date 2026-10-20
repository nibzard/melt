#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$(uname -s)" == Darwin ]] || { echo 'GhosttyKit requires macOS.' >&2; exit 2; }
# The internal embedding ABI is deliberately pinned, including its C header.
ghostty_revision=c7cbc9bfd44cb11a9c7eae00f1166624671e363b
ghostty_root="$PWD/build/dependencies/ghostty"
mkdir -p build/dependencies
if [[ ! -d "$ghostty_root/.git" ]]; then
    git init "$ghostty_root"
    git -C "$ghostty_root" remote add origin https://github.com/ghostty-org/ghostty.git
fi
if [[ "$(git -C "$ghostty_root" rev-parse HEAD 2>/dev/null || true)" != "$ghostty_revision" ]]; then
    git -C "$ghostty_root" fetch --depth 1 origin "$ghostty_revision"
    git -C "$ghostty_root" checkout --detach "$ghostty_revision"
fi
if [[ "$(zig version 2>/dev/null || true)" == 0.16.0 ]]; then
    ghostty_zig="$(command -v zig)"
else
    case "$(uname -m)" in
        arm64) ghostty_arch=aarch64; ghostty_sha=b23d70deaa879b5c2d486ed3316f7eaa53e84acf6fc9cc747de152450d401489 ;;
        x86_64) ghostty_arch=x86_64; ghostty_sha=0387557ed1877bc6a2e1802c8391953baddba76081876301c522f52977b52ba7 ;;
        *) echo 'Unsupported Ghostty build architecture.' >&2; exit 2 ;;
    esac
    ghostty_zig="$PWD/build/dependencies/zig-$ghostty_arch-macos-0.16.0/zig"
    if [[ ! -x "$ghostty_zig" ]]; then
        ghostty_tar="$PWD/build/dependencies/zig-0.16.0.tar.xz"
        curl -fL "https://ziglang.org/download/0.16.0/zig-$ghostty_arch-macos-0.16.0.tar.xz" -o "$ghostty_tar"
        printf '%s  %s\n' "$ghostty_sha" "$ghostty_tar" | shasum -a 256 -c -
        tar -xf "$ghostty_tar" -C build/dependencies
    fi
fi
# Run from the source root: translate-c requires an absolute build root.
cd "$ghostty_root"
"$ghostty_zig" build -Demit-macos-app=false -Demit-xcframework=true \
    -Dxcframework-target=native -Doptimize=ReleaseFast \
    --prefix "$OLDPWD/build/dependencies/ghostty-install"
printf '%s\n' "$ghostty_revision" > macos/GhosttyKit.xcframework/melt-revision
