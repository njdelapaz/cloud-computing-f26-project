#!/bin/bash
# Add the ver_0.2 plugin forks to the Slurm simulator source tree so they build with the same
# flags as the stock plugins (including the simulator's -Wl,-wrap,time link flags).
# Usage (inside the image build): register_plugins.sh <slurm source dir> <ver_0.2 plugin dir>
set -euo pipefail
SRC="$1"; PLUG="$2"

cp -r "$PLUG/priority_adaptive"          "$SRC/src/plugins/priority/adaptive"
cp -r "$PLUG/select_cons_tres_adaptive"  "$SRC/src/plugins/select/cons_tres_adaptive"

sed -i 's/^SUBDIRS = basic multifactor$/SUBDIRS = basic multifactor adaptive/' \
    "$SRC/src/plugins/priority/Makefile.am"
sed -i 's/^SUBDIRS = other cons_tres cray_aries linear$/SUBDIRS = other cons_tres cons_tres_adaptive cray_aries linear/' \
    "$SRC/src/plugins/select/Makefile.am"
sed -i 's#^\t\t src/plugins/priority/multifactor/Makefile$#&\n\t\t src/plugins/priority/adaptive/Makefile#' \
    "$SRC/configure.ac"
sed -i 's#^\t\t src/plugins/select/cons_tres/Makefile$#&\n\t\t src/plugins/select/cons_tres_adaptive/Makefile#' \
    "$SRC/configure.ac"

# Fail loudly if any edit didn't apply
grep -q '^SUBDIRS = basic multifactor adaptive$' "$SRC/src/plugins/priority/Makefile.am"
grep -q 'cons_tres cons_tres_adaptive' "$SRC/src/plugins/select/Makefile.am"
grep -q 'src/plugins/priority/adaptive/Makefile' "$SRC/configure.ac"
grep -q 'src/plugins/select/cons_tres_adaptive/Makefile' "$SRC/configure.ac"

# Regenerate configure and Makefile.in (autoconf 2.71 / automake 1.16.5, the versions Slurm used).
# The GLib/GTK stub macros sit next to this script in m4/.
cd "$SRC" && ACLOCAL_PATH="$(dirname "$0")/m4" autoreconf -fi
