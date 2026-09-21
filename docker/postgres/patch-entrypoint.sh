#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
set -eu
entrypoint="$1"
old='exec gosu postgres "$BASH_SOURCE" "$@"'
# A changed upstream seam needs review, not a best-effort text replacement.
[ "$(grep -Fc "$old" "$entrypoint")" = 1 ]
sed 's/exec gosu postgres /exec setpriv --reuid=postgres --regid=postgres --init-groups /' \
    "$entrypoint" > "$entrypoint.patched"
! grep -Fq gosu "$entrypoint.patched"
cat "$entrypoint.patched" > "$entrypoint"
rm "$entrypoint.patched"
