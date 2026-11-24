# Makefile for priority/multifactor plugin

AUTOMAKE_OPTIONS = foreign

PLUGIN_FLAGS = -module -avoid-version --export-dynamic

AM_CPPFLAGS = -DSLURM_PLUGIN_DEBUG -I$(top_srcdir) -I$(top_srcdir)/src/common

pkglib_LTLIBRARIES = priority_adaptive.la

priority_adaptive_la_SOURCES = priority_adaptive.c fair_tree.c fair_tree.h priority_adaptive.h

priority_adaptive_la_LDFLAGS = $(PLUGIN_FLAGS)
