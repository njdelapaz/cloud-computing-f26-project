# Makefile for the priority/adaptive plugin (ver_0.2 fork of priority/multifactor)

AUTOMAKE_OPTIONS = foreign

PLUGIN_FLAGS = -module -avoid-version --export-dynamic

# adaptive_policy/ is the shared policy state, compiled into slurmctld (docker/register_plugins.sh)
AM_CPPFLAGS = -DSLURM_PLUGIN_DEBUG -I$(top_srcdir) -I$(top_srcdir)/src/common \
	-I$(top_srcdir)/src/slurmctld/adaptive_policy

pkglib_LTLIBRARIES = priority_adaptive.la

priority_adaptive_la_SOURCES = priority_adaptive.c fair_tree.c fair_tree.h priority_adaptive.h \
	adaptive_conf.c adaptive_conf.h adaptive_ctl.c adaptive_ctl.h

priority_adaptive_la_LDFLAGS = $(PLUGIN_FLAGS)
