#!/bin/sh
# Install pare from its GitHub release: the binary for this machine,
# checked against the release's SHA256SUMS.
#
#   curl -sSfL https://raw.githubusercontent.com/wspires/pare/main/install.sh | sh
#
# Environment:
#   PARE_VERSION      a release tag (v0.2.0); default: the latest release
#   PARE_INSTALL_DIR  where to put pare; default: ~/.local/bin
#   PARE_RELEASES     base URL of the releases (for testing a mirror or a
#                     local copy); default: GitHub's
set -eu

repo="wspires/pare"
releases="${PARE_RELEASES:-https://github.com/$repo/releases}"
dest="${PARE_INSTALL_DIR:-$HOME/.local/bin}"

say() { printf 'pare install: %s\n' "$*" >&2; }
die() { say "$*"; exit 1; }

fetch() { # URL FILE
    if command -v curl >/dev/null 2>&1; then
        curl -sSfL "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget -q "$1" -O "$2"
    else
        die "needs curl or wget"
    fi
}

case "$(uname -s)" in
    Linux) os=unknown-linux-musl ;;
    Darwin) os=apple-darwin ;;
    *) die "no prebuilt binary for $(uname -s); try: pipx install pare-cli, or cargo install pare-cli" ;;
esac
case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64 ;;
    arm64 | aarch64) arch=aarch64 ;;
    *) die "no prebuilt binary for $(uname -m); try: pipx install pare-cli, or cargo install pare-cli" ;;
esac
target="$arch-$os"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

version="${PARE_VERSION:-}"
if [ -z "$version" ]; then
    # The latest release's tag, from the redirect GitHub answers with.
    fetch "$releases/latest/download/SHA256SUMS" "$tmp/SHA256SUMS"
    name="$(grep -o "pare-v[^ ]*-$target\.tar\.gz" "$tmp/SHA256SUMS" | head -n 1)"
    [ -n "$name" ] || die "the latest release has no build for $target"
    base="$releases/latest/download"
else
    name="pare-$version-$target.tar.gz"
    base="$releases/download/$version"
    fetch "$base/SHA256SUMS" "$tmp/SHA256SUMS"
fi

say "downloading $name"
fetch "$base/$name" "$tmp/$name"

want="$(grep " \*\{0,1\}$name\$" "$tmp/SHA256SUMS" | cut -d ' ' -f 1)"
[ -n "$want" ] || die "$name is not in SHA256SUMS"
if command -v sha256sum >/dev/null 2>&1; then
    got="$(sha256sum "$tmp/$name" | cut -d ' ' -f 1)"
else
    got="$(shasum -a 256 "$tmp/$name" | cut -d ' ' -f 1)"
fi
[ "$got" = "$want" ] || die "checksum mismatch for $name"

tar xzf "$tmp/$name" -C "$tmp"
mkdir -p "$dest"
dir="${name%.tar.gz}"
cp "$tmp/$dir/pare" "$dest/pare"
chmod 755 "$dest/pare"
say "installed $("$dest/pare" --version) to $dest/pare"
case ":$PATH:" in
    *":$dest:"*) ;;
    *) say "$dest is not on your PATH; add it, e.g.: export PATH=\"$dest:\$PATH\"" ;;
esac
