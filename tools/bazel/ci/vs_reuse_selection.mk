# Supplemental controller-only goal; pass after the unchanged source slave.mk.
# It resolves metadata and records the planned selection without an installer build.
ifneq ($(SONIC_DEB_BUILD_POLICY),reuse-only)
$(error VS archive reuse requires SONIC_DEB_BUILD_POLICY=reuse-only)
endif
ifneq ($(SONIC_BAZEL_KERNEL_PREPARE),verify)
$(error VS archive reuse requires SONIC_BAZEL_KERNEL_PREPARE=verify)
endif
ifneq ($(SONIC_BAZEL_VS_ARCHIVE_PROFILE),final23)
$(error VS archive reuse requires SONIC_BAZEL_VS_ARCHIVE_PROFILE=final23)
endif
ifneq ($(SONIC_IMAGE_VERSION),bazel-ci)
$(error VS archive reuse requires the current bazel-ci image identity)
endif
ifneq ($(CROSS_BUILD_ENVIRON),n)
$(error VS archive reuse requires explicit native CROSS_BUILD_ENVIRON=n)
endif
ifneq ($(MULTIARCH_QEMU_ENVIRON),n)
$(error VS archive reuse requires explicit native MULTIARCH_QEMU_ENVIRON=n)
endif
ifneq ($(SONIC_DPKG_CACHE_METHOD),rcache)
$(error VS archive reuse must read from existing package cache)
endif
ifneq ($(words $(sonic-vs.bin_MACHINE)),1)
$(error Expected one original Make machine for sonic-vs.bin)
endif

.PHONY: vs-reuse-selection
vs-reuse-selection: private export SONIC_IMAGE_VERSION := $(SONIC_IMAGE_VERSION)
vs-reuse-selection: private export VS_ARCHITECTURE := $(CONFIGURED_ARCH)
vs-reuse-selection: private export VS_PLATFORM := $(CONFIGURED_PLATFORM)
vs-reuse-selection: private export VS_DISTRIBUTION := $(BLDENV)
vs-reuse-selection: private export VS_BUILDER := $(BUILD_WITH_BAZEL_WHEN_AVAILABLE)
vs-reuse-selection: private export VS_FIPS := $(INCLUDE_FIPS)
vs-reuse-selection: private export VS_DASH := $(INCLUDE_VS_DASH_SAI)
vs-reuse-selection: private export VS_ASAN := $(ENABLE_ASAN)
vs-reuse-selection: private export VS_SYNCD_RPC := $(ENABLE_SYNCD_RPC)
vs-reuse-selection: private export VS_CROSS_BUILD := $(CROSS_BUILD_ENVIRON)
vs-reuse-selection: private export VS_MULTIARCH_QEMU := $(MULTIARCH_QEMU_ENVIRON)
vs-reuse-selection: $(addprefix $(TARGET_PATH)/bazel-manifests/,$(addsuffix /manifest.json,$(sort $(SONIC_BAZEL_MANIFESTS)))) $(SONIC_BAZEL_IMAGE_ENVIRONMENT_FILES)
	set -euo pipefail
	@test ! -e "$(TARGET_PATH)/sonic-vs.bin.selection.json"
	@test ! -e "$(TARGET_PATH)/sonic-vs.bin.dockers"
	export installer_images="$(foreach docker, $(sonic-vs.bin_DOCKERS),\
		$(addprefix $($(docker:-dbg.gz=.gz)_PACKAGE_NAME)|,\
		$(addprefix $($(docker:-dbg.gz=.gz)_PATH)|,\
		$(addprefix $($(docker:-dbg.gz=.gz)_MACHINE)|,\
		$(addprefix $(TARGET_PATH)/,$(addsuffix :$($(docker:-dbg.gz=.gz)_VERSION),$(docker)))))))"
	export sonic_local_packages="$(foreach package, $(SONIC_PACKAGES_LOCAL),\
		$(addsuffix |$($(package)_DEFAULT_FEATURE_STATE_ENABLED),\
		$(addsuffix |$($(package)_DEFAULT_FEATURE_OWNER),\
		$(addsuffix |$(addprefix $(TARGET_PATH)/, $(package)), $(package)))))"
	export include_system_eventd="$(INCLUDE_SYSTEM_EVENTD)"
	export install_debug_image="$(INSTALL_DEBUG_TOOLS)"
	printf '%s\n' $${installer_images} | \
		awk -F'|' -v machine="$(sonic-vs.bin_MACHINE)" \
		'$$3 == "" || $$3 == machine { image = $$4; sub(/:[^:]*$$/, "", image); print image }' \
		> "$(TARGET_PATH)/sonic-vs.bin.dockers"
	python3 -B tools/bazel/ci/vs_final23.py --workspace "$(CURDIR)" --installer sonic-vs.bin --machine "$(sonic-vs.bin_MACHINE)"
