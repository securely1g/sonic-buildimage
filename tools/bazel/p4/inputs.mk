# Import the existing native P4 packages only for the validated VS profile.
# Other platforms, distributions and instrumented builds keep their Make rules.
SONIC_BAZEL_P4_CANDIDATES := $(P4LANG_PI) $(P4LANG_BMV2) $(P4LANG_P4C) $(DASH_SAI) $(DASH_SAI_DEV)
ifeq ($(BUILD_WITH_BAZEL_WHEN_AVAILABLE):$(CONFIGURED_PLATFORM):$(CONFIGURED_ARCH):$(BLDENV):$(INCLUDE_VS_DASH_SAI),y:vs:amd64:trixie:y)
ifeq ($(filter y,$(SONIC_DEBUGGING_ON) $(SONIC_PROFILING_ON) $(ENABLE_ASAN) $(CROSS_BUILD_ENVIRON) $(MULTIARCH_QEMU_ENVIRON) $(ENABLE_SOURCE_ARCHIVE)),)
ifeq ($(filter nostrip noopt,$(DEB_BUILD_OPTIONS) $(DEB_BUILD_OPTIONS_GENERIC)),)
SONIC_BAZEL_P4_DEBS := $(SONIC_BAZEL_P4_CANDIDATES)
SONIC_BAZEL_IMPORTED_DEBS += $(SONIC_BAZEL_P4_DEBS)

# Remove both the native producers and the derived-package placeholder. Every
# DEB, including libsai-dev, is restored independently from the reviewed pins.
SONIC_MAKE_DEBS := $(filter-out $(SONIC_BAZEL_P4_DEBS),$(SONIC_MAKE_DEBS))
SONIC_DERIVED_DEBS := $(filter-out $(SONIC_BAZEL_P4_DEBS),$(SONIC_DERIVED_DEBS))

# Legacy consumers may still use Make's cache. Include the imported package
# identity in their dependency key without using that cache to restore P4.
$(foreach deb,$(SONIC_BAZEL_P4_DEBS),$(eval $(deb)_MOD_HASH_FILE := tools/bazel/p4/packages.lock.json))
endif
endif
endif
