#!/bin/sh
# Tests install.sh against a release laid out on disk the way GitHub serves
# one (download/<tag>/..., latest/download/...), built from a pare binary:
#
#   packaging/test-install.sh target/release/pare
#
# Run by CI; needs curl (for file:// URLs) and a Linux or macOS host.
set -eu

bin="$1"
root="$(cd "$(dirname "$0")/.." && pwd)"
version="v$("$bin" --version | cut -d ' ' -f 2)"
case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) target=x86_64-unknown-linux-musl ;;
    Linux-aarch64) target=aarch64-unknown-linux-musl ;;
    Darwin-arm64) target=aarch64-apple-darwin ;;
    Darwin-x86_64) target=x86_64-apple-darwin ;;
    *) echo "unsupported host" >&2; exit 1 ;;
esac

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
name="pare-$version-$target"
mkdir -p "$tmp/$name" "$tmp/releases/download/$version" "$tmp/releases/latest/download"
cp "$bin" "$tmp/$name/pare"
tar czf "$tmp/releases/download/$version/$name.tar.gz" -C "$tmp" "$name"
(cd "$tmp/releases/download/$version" && sha256sum ./*.tar.gz | sed 's| \./| |' > SHA256SUMS)
cp "$tmp/releases/download/$version/"* "$tmp/releases/latest/download/"

# A pinned version, then the latest.
PARE_RELEASES="file://$tmp/releases" PARE_VERSION="$version" PARE_INSTALL_DIR="$tmp/bin1" sh "$root/install.sh"
PARE_RELEASES="file://$tmp/releases" PARE_INSTALL_DIR="$tmp/bin2" sh "$root/install.sh"
for d in bin1 bin2; do
    [ "$("$tmp/$d/pare" --version)" = "pare ${version#v}" ] || { echo "wrong binary in $d" >&2; exit 1; }
done

# A tampered archive is refused.
printf 'x' >> "$tmp/releases/download/$version/$name.tar.gz"
if PARE_RELEASES="file://$tmp/releases" PARE_VERSION="$version" PARE_INSTALL_DIR="$tmp/bin3" sh "$root/install.sh" 2>/dev/null; then
    echo "a corrupted archive was installed" >&2
    exit 1
fi
echo "install.sh: ok"
