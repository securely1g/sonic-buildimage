.ONESHELL:
SHELL = /bin/bash
.SHELLFLAGS += -e

BAZEL_TARGET_PATH = p4rt_app
BAZEL_BUILD_TARGETS = //p4rt_app:p4rt_deb //p4rt_app:p4rt_dbg_deb

# Let Bazel collect split debug symbols into the .dwp for optimized builds.
# Explicit -g overrides the toolchain's optimized-build default of -g0.
# GNU dwp 2.44 cannot package GCC 14's default DWARF 5 debug information.
BAZEL_BUILD_OPTS += --fission=opt --copt=-g --copt=-gdwarf-4

# Common builds with Bazel 8.5.1; P4RT imports its library and headers with 6.4.0.
COMMON_SOURCE ?= $(abspath $(CURDIR)/../sonic-swss-common)
COMMON_SDK := $(CURDIR)/.common-sdk
COMMON_SDK_BUILDER := $(abspath $(CURDIR)/../../tools/bazel/p4rt_common_sdk.py)
BAZEL_BUILD_OPTS += --override_repository=sonic_swss_common=$(COMMON_SDK)
P4RT_BAZEL = USE_BAZEL_VERSION=6.4.0 bazel

# Newer version of gcc has spurious warnings in boringssl package.
BAZEL_BUILD_OPTS += --copt=-Wno-array-bounds
BAZEL_BUILD_OPTS += --copt=-Wno-stringop-overflow

# Keep optimized binaries with the skeleton debug information for the .dwp.
BAZEL_BUILD_OPTS += -c opt

# The slave's /etc/bazel.bazelrc shares repository and disk caches at
# /bazel_cache. Keep output bases and server state private to each builder.
# BAZEL_CACHE remains an optional override for direct component builds.
BAZEL_CACHE ?= /bazel_cache
ifneq ($(wildcard $(BAZEL_CACHE)/.),)
BAZEL_BUILD_OPTS += --repository_cache=$(BAZEL_CACHE)/repository_cache --disk_cache=$(BAZEL_CACHE)/disk_cache
COMMON_CACHE_OPTS = --bazel-arg=--repository_cache=$(BAZEL_CACHE)/repository_cache --bazel-arg=--disk_cache=$(BAZEL_CACHE)/disk_cache
endif

MAIN_TARGET = $(SONIC_P4RT)
DERIVED_TARGETS = $(SONIC_P4RT_DBG)

$(addprefix $(DEST)/, $(MAIN_TARGET)): $(DEST)/% :
	function cleanup {
		# Note: make seems to hang if Bazel is still running
		cd $(CURDIR)/sonic-pins && ($(P4RT_BAZEL) $(BAZEL_OPTS) shutdown; rm -rf bazel-*)
	}
	trap cleanup EXIT

	# Recheck Common's source graph on every P4RT build, including cfg_schema.h.
	python3 "$(COMMON_SDK_BUILDER)" --common-source "$(COMMON_SOURCE)" \
		--output-dir "$(COMMON_SDK)" $(COMMON_CACHE_OPTS)

	pushd ./sonic-pins
	$(P4RT_BAZEL) $(BAZEL_OPTS) version --gnu_format | grep -x 'bazel 6.4.0'
	$(P4RT_BAZEL) $(BAZEL_OPTS) build $(BAZEL_BUILD_OPTS) $(BAZEL_BUILD_TARGETS)
	$(P4RT_BAZEL) $(BAZEL_OPTS) test $(BAZEL_BUILD_OPTS) //$(BAZEL_TARGET_PATH)/...
	cd ./bazel-bin/$(BAZEL_TARGET_PATH)
	mv $* $(DERIVED_TARGETS) $(DEST)/
	popd

$(addprefix $(DEST)/, $(DERIVED_TARGETS)): $(DEST)/% : $(DEST)/$(MAIN_TARGET)
