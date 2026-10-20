# Sourced by native build/check scripts from the repository root.
ghostty_kit="$PWD/build/dependencies/ghostty/macos/GhosttyKit.xcframework"
ghostty_revision=c7cbc9bfd44cb11a9c7eae00f1166624671e363b
if [[ "$(cat "$ghostty_kit/melt-revision" 2>/dev/null || true)" != "$ghostty_revision" ]]; then
    bash native/ghostty-setup.sh
fi
ghostty_slice="$ghostty_kit/macos-$(uname -m)"
GHOSTTY_FLAGS=(-I "$ghostty_slice/Headers" "$ghostty_slice/libghostty-internal.a"
    -framework Carbon -framework Metal -framework QuartzCore -framework CoreText
    -framework CoreVideo -framework IOKit -lc++)
