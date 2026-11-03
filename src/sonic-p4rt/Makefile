.ONESHELL:
SHELL = /bin/bash
.SHELLFLAGS += -e

BAZEL_TARGET_PATH = p4rt_app
BAZEL_BUILD_TARGETS = //p4rt_app:p4rt_deb //p4rt_app:p4rt_dbg_deb

# Keep the pinned archives and integrity checks while providing reachable
# public mirrors for this nested Bazel workspace's source downloads.
BAZEL_BUILD_OPTS += --experimental_downloader_config=$(CURDIR)/source_mirrors.cfg

# Let Bazel collect split debug symbols into the .dwp for optimized builds.
# Explicit -g overrides the toolchain's optimized-build default of -g0.
# GNU dwp 2.44 cannot package GCC 14's default DWARF 5 debug information.
BAZEL_BUILD_OPTS += --fission=opt --copt=-g --copt=-gdwarf-4

# Override sonic_swss_common to use the version specific to this buildimage.
BAZEL_BUILD_OPTS += --override_repository=sonic_swss_common=/sonic/src/sonic-swss-common

# Use the YANG schema installed by libswsscommon-dev until Common can generate
# it under Bazel. Export it from CFG_SCHEMA_REPO as a declared input so the
# P4RT build keeps YANG modules enabled.
CFG_SCHEMA_H = /usr/include/swss/cfg_schema.h
CFG_SCHEMA_REPO = /tmp/sonic-p4rt-cfg-schema
BAZEL_BUILD_OPTS += --override_repository=swss_cfg_schema=$(CFG_SCHEMA_REPO)
BAZEL_BUILD_OPTS += --@sonic_swss_common//tools/bazel:cfg_schema=@swss_cfg_schema//:cfg_schema.h

# Newer version of gcc has spurious warnings in boringssl package.
BAZEL_BUILD_OPTS += --copt=-Wno-array-bounds
BAZEL_BUILD_OPTS += --copt=-Wno-stringop-overflow

# Keep optimized binaries with the skeleton debug information for the .dwp.
BAZEL_BUILD_OPTS += -c opt

# Use a host directory to store Bazel download and action caches, to speed up
# incremental rebuilds on the same system for developers changing sonic-pins.
# Keep Bazel server and output state private to each recipe with the shared
# helper for private Bazel servers so concurrent builds do not share that state.
#
# When present, /bazel_cache is the standard shared slave cache mount
# (see SONIC_BAZEL_CACHE_SOURCE in rules/config).
# To point it elsewhere instead, override BAZEL_CACHE and mount it yourself via
# DOCKER_BUILDER_USER_MOUNT:
#   DOCKER_BUILDER_USER_MOUNT=<directory on host system>:$BAZEL_CACHE:rw
#
BAZEL_CACHE ?= /bazel_cache

MAIN_TARGET = $(SONIC_P4RT)
DERIVED_TARGETS = $(SONIC_P4RT_DBG)

ifeq ($(SONIC_VS_IMAGE_TIMINGS),1)
vs_timer = python3 -B "$(CURDIR)/../../tools/bazel/ci/time_vs_image.py"
define vs_start_timing
	timing_directory="$$($(vs_timer) start-recipe "$(abspath $(CURDIR)/../..)" sonic-p4rt --bazel build test info shutdown --profiles build test --exports stage-debs)"
	exec > >(tee "$$timing_directory/make.log") 2>&1
endef
vs_time_phase = $(vs_timer) "$$timing_directory" $(1)
endif

# TODO: Remove this Makefile workaround once Bazel supports generating cfg_schema.h.
#
# Wrap the installed header in a throwaway repository so it can be named by a
# label and handed to --@sonic_swss_common//tools/bazel:cfg_schema.
$(CFG_SCHEMA_REPO)/BUILD: $(CFG_SCHEMA_H)
	mkdir -p $(@D)
	cp $< $(@D)/cfg_schema.h
	echo 'workspace(name = "swss_cfg_schema")' > $(@D)/WORKSPACE
	echo 'exports_files(["cfg_schema.h"])' > $@

$(CFG_SCHEMA_H):
	echo "$@ not found: is libswsscommon-dev installed?" >&2
	exit 1

$(addprefix $(DEST)/, $(MAIN_TARGET)): $(DEST)/% : $(CFG_SCHEMA_REPO)/BUILD
	$(vs_start_timing)
	cd ./sonic-pins
	source "$(CURDIR)/../../tools/bazel/private_server.sh"
	startup_options=($(BAZEL_OPTS))
	build_options=($(BAZEL_BUILD_OPTS) --symlink_prefix=/)
	if [[ -d "$(BAZEL_CACHE)" ]]; then
		build_options+=("--repository_cache=$(abspath $(BAZEL_CACHE))/repository_cache" "--disk_cache=$(abspath $(BAZEL_CACHE))/disk_cache")
	fi
	if [[ -n "$${SONIC_BAZEL_REMOTE_CACHE:-}" ]]; then
		build_options+=("--remote_cache=$$SONIC_BAZEL_REMOTE_CACHE" "--remote_upload_local_results=$(if $(filter y,$(SONIC_BAZEL_REMOTE_UPLOAD)),true,false)")
	fi
	# The nested sonic-pins workspace selects its pinned Bazel version via PATH.
	BAZEL=bazel start_private_bazel sonic-p4rt "$${startup_options[@]}"
	private_bazel_shutdown_prefix=($(call vs_time_phase,shutdown))
	$(call vs_time_phase,build) "$${private_bazel[@]}" build "$${build_options[@]}" $(BAZEL_BUILD_TARGETS)
	$(call vs_time_phase,test) "$${private_bazel[@]}" test "$${build_options[@]}" //$(BAZEL_TARGET_PATH)/...
	package_bin="$$($(call vs_time_phase,info) "$${private_bazel[@]}" info "$${build_options[@]}" bazel-bin)"
	$(call vs_time_phase,stage-debs) mv -- $(foreach package,$* $(DERIVED_TARGETS),"$$package_bin/$(BAZEL_TARGET_PATH)/$(package)") "$(abspath $(DEST))/"

$(addprefix $(DEST)/, $(DERIVED_TARGETS)): $(DEST)/% : $(DEST)/$(MAIN_TARGET)
