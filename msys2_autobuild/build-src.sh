#!/bin/bash
# This script installs the makedepends for a PKGBUILD, runs the passed makepkg
# command, and then removes the makedepends. Required because makepkg doesn't
# handle makedepends automatically when building source packages.

set -o errexit -o nounset -o pipefail

if (($# == 0)); then
	printf 'Usage: %s makepkg-command [options...]\n' "$0" >&2
	exit 2
fi

# XXX: in theory we need "depends" as well, but this seems good enough for now.
srcinfo=$("$1" --printsrcinfo)
mapfile -t deps < <(awk '$1 == "makedepends" { print $3 }' <<< "$srcinfo")
pacman=${PACMAN:-pacman}

if ((${#deps[@]})); then
	before=$("$pacman" --query --quiet)

	cleanup() {
		status=$?
		after=$("$pacman" --query --quiet) || exit 1

		mapfile -t added < <(grep --invert-match --line-regexp --fixed-strings \
			--file <(printf '%s\n' "$before") <<< "$after")
		if ((${#added[@]})); then
			echo "Dependencies to be removed: ${added[*]}"
			"$pacman" --remove --nosave --unneeded --noconfirm --noprogressbar "${added[@]}" || exit 1
		fi
		exit "$status"
	}
	trap cleanup EXIT

	echo "Dependencies to be installed: ${deps[*]}"
	"$pacman" --sync --needed --asdeps --noconfirm --noprogressbar "${deps[@]}"
fi

"$@"
