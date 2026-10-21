.ONESHELL:
SHELL = /bin/bash
.SHELLFLAGS += -e

P4RT_BUILD_WITH_BAZEL_TARS ?= n

# Let Bazel collect split debug symbols into the .dwp for optimized builds.
# Explicit -g overrides the toolchain's optimized-build default of -g0.
# GNU dwp 2.44 cannot package GCC 14's default DWARF 5 debug information.
BAZEL_BUILD_OPTS += --fission=opt --copt=-g --copt=-gdwarf-4

# Override sonic_swss_common to use the version specific to this buildimage.
BAZEL_BUILD_OPTS += --override_repository=sonic_swss_common=/sonic/src/sonic-swss-common

# Newer version of gcc has spurious warnings in boringssl package.
BAZEL_BUILD_OPTS += --copt=-Wno-array-bounds
BAZEL_BUILD_OPTS += --copt=-Wno-stringop-overflow

# Keep optimized binaries with the skeleton debug information for the .dwp.
BAZEL_BUILD_OPTS += -c opt

# Use a host directory to store Bazel cache, if mounted. This will speed up
# incremental rebuilds on the same system for developers that makes changes
# to sonic-pins code.
#
# To build with Bazel cache, add DOCKER_BUILDER_USER_MOUNT:
#   DOCKER_BUILDER_USER_MOUNT=<directory on host system>:$BAZEL_CACHE:rw
#
# For example:
#   DOCKER_BUILDER_USER_MOUNT=/tmp/bazel_cache:/bazel:rw make target/...
#
BAZEL_CACHE ?= /bazel
BAZEL_OPTS += $(shell test -d $(BAZEL_CACHE) && echo --output_user_root=$(BAZEL_CACHE)/cache)

MAIN_TARGET = $(SONIC_P4RT)
DERIVED_TARGETS = $(SONIC_P4RT_DBG)

ifeq ($(P4RT_BUILD_WITH_BAZEL_TARS),y)
BAZEL_ACTION_AUDIT ?= $(CURDIR)/../../tools/bazel/ci/bazel_commands.py

# The source owner already publishes these payloads as TARs. Keep the runtime
# and symbols as separate real Make outputs; never request its pkg_deb wrappers.
$(DEST)/$(SONIC_P4RT): P4RT_BAZEL_TARGET = p4rt_binaries
$(DEST)/$(SONIC_P4RT_DBG): P4RT_BAZEL_TARGET = p4rt_debug
$(DEST)/$(SONIC_P4RT_DBG): $(DEST)/$(SONIC_P4RT)

$(addprefix $(DEST)/, $(SONIC_P4RT) $(SONIC_P4RT_DBG)):
	function cleanup {
		# Keep Bazel outputs/downloads/actions available to the next archive.
		cd $(CURDIR)/sonic-pins && bazel $(BAZEL_OPTS) shutdown
	}
	trap cleanup EXIT

	# TODO: Remove this Makefile workaround
	# once Bazel natively supports generating cfg_schema.h.
	if [[ ! -f /sonic/src/sonic-swss-common/common/cfg_schema.h && -f /usr/include/swss/cfg_schema.h ]]; then \
		if [ ! -d /sonic/src/sonic-swss-common/common ]; then \
			mkdir -p /sonic/src/sonic-swss-common/common; \
		fi; \
		cp /usr/include/swss/cfg_schema.h /sonic/src/sonic-swss-common/common/cfg_schema.h; \
	fi

	pushd ./sonic-pins
	tests=()
	if [ "$(P4RT_BAZEL_TARGET)" = p4rt_binaries ]; then
		# `bazel test //p4rt_app/...` also builds non-test package rules.
		# Resolve only test labels and keep an empty selection a hard failure.
		test_labels=$$(bazel $(BAZEL_OPTS) query $(filter --override_repository=%,$(BAZEL_BUILD_OPTS)) 'tests(//p4rt_app/...)' --output=label)
		test -n "$$test_labels"
		mapfile -t tests <<< "$$test_labels"
	fi
	# Audit the exact configured graph, including test dependencies, before any
	# actions run. Use the same startup flags/output root as build and test.
	mkdir -p $(DEST)
	python3 $(BAZEL_ACTION_AUDIT) \
		$(foreach option,$(BAZEL_OPTS),--bazel-startup-arg=$(option)) \
		$(foreach option,$(BAZEL_BUILD_OPTS),--bazel-arg=$(option)) \
		--output "$@.bazel-audit.json" \
		//p4rt_app:$(P4RT_BAZEL_TARGET) "$${tests[@]}"
	bazel $(BAZEL_OPTS) build $(BAZEL_BUILD_OPTS) //p4rt_app:$(P4RT_BAZEL_TARGET)
	if [ "$${#tests[@]}" -gt 0 ]; then
		bazel $(BAZEL_OPTS) test $(BAZEL_BUILD_OPTS) --build_tests_only "$${tests[@]}"
	fi
	test -s bazel-bin/p4rt_app/$(P4RT_BAZEL_TARGET).tar.bz2
	install -m 0644 bazel-bin/p4rt_app/$(P4RT_BAZEL_TARGET).tar.bz2 "$@.tmp"
	mv -f "$@.tmp" "$@"
	popd

else
BAZEL_TARGET_PATH = p4rt_app
BAZEL_BUILD_TARGETS = //p4rt_app:p4rt_deb //p4rt_app:p4rt_dbg_deb

$(addprefix $(DEST)/, $(MAIN_TARGET)): $(DEST)/% :
	function cleanup {
		# Note: make seems to hang if Bazel is still running
		cd $(CURDIR)/sonic-pins && (bazel $(BAZEL_OPTS) shutdown; rm -rf bazel-*)
	}
	trap cleanup EXIT

	# TODO: Remove this Makefile workaround
	# once Bazel natively supports generating cfg_schema.h.
	if [[ ! -f /sonic/src/sonic-swss-common/common/cfg_schema.h && -f /usr/include/swss/cfg_schema.h ]]; then \
		if [ ! -d /sonic/src/sonic-swss-common/common ]; then \
			mkdir -p /sonic/src/sonic-swss-common/common; \
		fi; \
		cp /usr/include/swss/cfg_schema.h /sonic/src/sonic-swss-common/common/cfg_schema.h; \
	fi

	pushd ./sonic-pins
	bazel $(BAZEL_OPTS) build $(BAZEL_BUILD_OPTS) $(BAZEL_BUILD_TARGETS)
	bazel $(BAZEL_OPTS) test $(BAZEL_BUILD_OPTS) //$(BAZEL_TARGET_PATH)/...
	cd ./bazel-bin/$(BAZEL_TARGET_PATH)
	mv $* $(DERIVED_TARGETS) $(DEST)/
	popd

$(addprefix $(DEST)/, $(DERIVED_TARGETS)): $(DEST)/% : $(DEST)/$(MAIN_TARGET)
endif
