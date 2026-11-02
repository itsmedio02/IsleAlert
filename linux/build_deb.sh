#!/bin/sh
# Build the Linux package: linux/dist/isle-alert_<version>_all.deb
# The version comes from __version__ in isle_core.py, so it always matches the Windows app.
# Needs dpkg-deb (any Debian/Ubuntu machine, or the GitHub Actions ubuntu runner).
set -eu
cd "$(dirname "$0")/.."                                   # the repo root

VERSION=$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' isle_core.py)
[ -n "$VERSION" ] || { echo "No __version__ found in isle_core.py" >&2; exit 1; }

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
cp -r linux/package/. "$STAGE/"
LIB="$STAGE/usr/lib/isle-alert"
mkdir -p "$LIB"
cp isle_core.py linux/isle_alert.py linux/isle_alert_gui.py Hungry.wav Thirsty.wav Oxygen.wav "$LIB/"
sed -i "s/^Version: .*/Version: $VERSION/" "$STAGE/DEBIAN/control"

find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/usr/bin/isle-alert"

mkdir -p linux/dist
OUT="linux/dist/isle-alert_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT"
echo "Built $OUT"
