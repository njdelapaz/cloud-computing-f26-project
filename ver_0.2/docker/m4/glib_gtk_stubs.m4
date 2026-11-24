# Stubs so autoreconf works without the GLib/GTK development packages.
# Slurm only uses GLib/GTK for the sview GUI, which this build never makes. In this
# image the real macros would take their "not found" branch (the third argument),
# so the stubs do exactly that.
AC_DEFUN([AM_PATH_GLIB_2_0], [$3])
AC_DEFUN([AM_PATH_GTK_2_0], [$3])
