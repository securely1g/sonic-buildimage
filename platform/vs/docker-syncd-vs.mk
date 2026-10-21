# docker image for vs syncd

DOCKER_SYNCD_PLATFORM_CODE = vs
include $(PLATFORM_PATH)/../template/docker-syncd-trixie.mk

ifneq ($(BUILD_WITH_BAZEL_WHEN_AVAILABLE),y)
ifneq ($(BUILD_WITH_BAZEL_WHEN_AVAILABLE),n)
$(error BUILD_WITH_BAZEL_WHEN_AVAILABLE must be y or n)
endif
endif

# Keep the same narrow native Trixie boundary as the first SWSS OCI consumer.
# All project packages use source targets except the cached P4/DASH tuple.
# The other syncd configurations continue to use their Dockerfile.
ifeq ($(BLDENV),trixie)
SONIC_BAZEL_SWITCHABLE_IMAGES += $(DOCKER_SYNCD_BASE) $(DOCKER_SYNCD_BASE_DBG)
ifeq ($(BUILD_WITH_BAZEL_WHEN_AVAILABLE):$(CONFIGURED_PLATFORM):$(CONFIGURED_ARCH):$(DBG_IMAGE_MARK):$(INCLUDE_VS_DASH_SAI):$(INCLUDE_FIPS),y:vs:amd64:dbg:y:y)
ifeq ($(filter y,$(CROSS_BUILD_ENVIRON) $(MULTIARCH_QEMU_ENVIRON) $(ENABLE_ASAN) $(ENABLE_SYNCD_RPC)),)
ifeq ($(findstring docker-sonic-vs,$(SONIC_BUILD_TARGET) $(EXTRA_DOCKER_TARGETS)),)
# Bazel declares this reviewed Make input directory explicitly in the root package.
SYNCD_VS_BAZEL_DEBS_PATH = $(if $($(DOCKER_SYNCD_BASE)_DEBS_PATH),$($(DOCKER_SYNCD_BASE)_DEBS_PATH),$(DEBS_PATH))
ifeq ($(SYNCD_VS_BAZEL_DEBS_PATH),target/debs/trixie)
SONIC_BAZEL_DOCKER_IMAGES += $(DOCKER_SYNCD_BASE)
SONIC_BAZEL_DBG_DOCKER_IMAGES += $(DOCKER_SYNCD_BASE_DBG)

SONIC_BAZEL_MANIFESTS += $(DOCKER_SYNCD_BASE_STEM) $(DOCKER_SYNCD_BASE_STEM)-$(DBG_IMAGE_MARK)
$(DOCKER_SYNCD_BASE_STEM)_MANIFEST_IMAGE = $(DOCKER_SYNCD_BASE)
$(DOCKER_SYNCD_BASE_STEM)-$(DBG_IMAGE_MARK)_MANIFEST_IMAGE = $(DOCKER_SYNCD_BASE)
$(DOCKER_SYNCD_BASE_STEM)-$(DBG_IMAGE_MARK)_MANIFEST_SUFFIX = $(DBG_IMAGE_MARK)

# The four P4/DASH runtime packages retain PR #14's exact cached bytes. Source
# owner targets build all other project packages, including the config-engine
# base; no Make-built runtime or symbol package is imported for those owners.
SYNCD_VS_BAZEL_RUNTIME_DEBS = $(P4LANG_PI) $(P4LANG_BMV2) $(P4LANG_P4C) $(DASH_SAI)

$(DOCKER_SYNCD_BASE)_BAZEL_TARGET = //platform/vs/docker-syncd-vs:docker-syncd-vs.gz
$(DOCKER_SYNCD_BASE_DBG)_BAZEL_TARGET = //platform/vs/docker-syncd-vs:docker-syncd-vs-dbg.gz
$(DOCKER_SYNCD_BASE)_BAZEL_ARGS = --config=syncd-dash
$(DOCKER_SYNCD_BASE_DBG)_BAZEL_ARGS = --config=syncd-dash
$(DOCKER_SYNCD_BASE)_BAZEL_DEPENDS = \
    $(addprefix $(SYNCD_VS_BAZEL_DEBS_PATH)/,$(SYNCD_VS_BAZEL_RUNTIME_DEBS)) \
    $(TARGET_PATH)/bazel-manifests/docker-base-trixie/manifest.json \
    $(TARGET_PATH)/bazel-manifests/docker-config-engine-trixie/manifest.json \
    $(TARGET_PATH)/bazel-manifests/docker-syncd-vs/manifest.json
$(DOCKER_SYNCD_BASE_DBG)_BAZEL_DEPENDS = $($(DOCKER_SYNCD_BASE)_BAZEL_DEPENDS) \
    $(TARGET_PATH)/bazel-manifests/docker-syncd-vs-dbg/manifest.json

endif
endif
endif
endif
endif

$(DOCKER_SYNCD_BASE_DBG)_PATH = $($(DOCKER_SYNCD_BASE)_PATH)

$(DOCKER_SYNCD_BASE)_DEPENDS += $(SYNCD_VS) \
                              $(LIBNL3_DEV) \
                              $(LIBNL3)

$(DOCKER_SYNCD_BASE)_DBG_DEPENDS += $(SYNCD_VS_DBG) \
                                $(LIBSWSSCOMMON_DBG) \
                                $(LIBSAIMETADATA_DBG) \
                                $(LIBSAIREDIS_DBG) \
                                $(LIBSAIVS_DBG)

$(DOCKER_SYNCD_BASE)_RUN_OPT += --privileged

$(DOCKER_SYNCD_BASE)_VERSION = 1.0.0
$(DOCKER_SYNCD_BASE)_PACKAGE_NAME = syncd
