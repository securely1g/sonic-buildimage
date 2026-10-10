# docker image for vs syncd

DOCKER_SYNCD_PLATFORM_CODE = vs
include $(PLATFORM_PATH)/../template/docker-syncd-trixie.mk

ifneq ($(BUILD_WITH_BAZEL_WHEN_AVAILABLE),y)
ifneq ($(BUILD_WITH_BAZEL_WHEN_AVAILABLE),n)
$(error BUILD_WITH_BAZEL_WHEN_AVAILABLE must be y or n)
endif
endif

# Keep the same narrow native Trixie boundary as the first SWSS OCI consumer.
# This path packages the existing Make DEBs; it does not rebuild the P4 toolchain
# with Bazel. The other syncd configurations continue to use their Dockerfile.
ifeq ($(BLDENV),trixie)
SONIC_BAZEL_SWITCHABLE_IMAGES += $(DOCKER_SYNCD_BASE) $(DOCKER_SYNCD_BASE_DBG)
ifeq ($(BUILD_WITH_BAZEL_WHEN_AVAILABLE):$(CONFIGURED_PLATFORM):$(CONFIGURED_ARCH):$(DBG_IMAGE_MARK):$(INCLUDE_VS_DASH_SAI):$(INCLUDE_FIPS),y:vs:amd64:dbg:y:y)
ifeq ($(filter y,$(CROSS_BUILD_ENVIRON) $(MULTIARCH_QEMU_ENVIRON) $(ENABLE_ASAN) $(ENABLE_SYNCD_RPC)),)
ifeq ($(findstring docker-sonic-vs,$(SONIC_BUILD_TARGET) $(EXTRA_DOCKER_TARGETS)),)
# Bazel declares this reviewed Make input directory explicitly in the root package.
ifeq ($(if $($(DOCKER_SYNCD_BASE)_DEBS_PATH),$($(DOCKER_SYNCD_BASE)_DEBS_PATH),$(DEBS_PATH)),target/debs/trixie)
SONIC_BAZEL_DOCKER_IMAGES += $(DOCKER_SYNCD_BASE)
SONIC_BAZEL_DBG_DOCKER_IMAGES += $(DOCKER_SYNCD_BASE_DBG)
SONIC_BAZEL_OCI_BASES += docker-config-engine-trixie.oci
docker-config-engine-trixie.oci_OCI_ARCHIVE = $(TARGET_PATH)/$(DOCKER_CONFIG_ENGINE_TRIXIE)
docker-config-engine-trixie.oci_OCI_PLATFORM = linux/amd64

SONIC_BAZEL_MANIFESTS += $(DOCKER_SYNCD_BASE_STEM) $(DOCKER_SYNCD_BASE_STEM)-$(DBG_IMAGE_MARK)
$(DOCKER_SYNCD_BASE_STEM)_MANIFEST_IMAGE = $(DOCKER_SYNCD_BASE)
$(DOCKER_SYNCD_BASE_STEM)-$(DBG_IMAGE_MARK)_MANIFEST_IMAGE = $(DOCKER_SYNCD_BASE)
$(DOCKER_SYNCD_BASE_STEM)-$(DBG_IMAGE_MARK)_MANIFEST_SUFFIX = $(DBG_IMAGE_MARK)

# Pass existing Make DEBs directly to the shared Bazel importer. Preserve the
# Dockerfile's two initial libnl development packages and dependency-first order.
# Expand before filtering so dependencies of source-built libraries are retained.
SYNCD_VS_BAZEL_DEBS_PATH = $(if $($(DOCKER_SYNCD_BASE)_DEBS_PATH),$($(DOCKER_SYNCD_BASE)_DEBS_PATH),$(DEBS_PATH))
SYNCD_VS_BAZEL_SOURCE_DEBS = $(LIBSWSSCOMMON) $(LIBSAIREDIS) $(LIBSAIMETADATA) $(LIBSWSSCOMMON_DBG) $(LIBSAIREDIS_DBG) $(LIBSAIMETADATA_DBG)
# P4C brings an SSH client into runtime. Keep the Make FIPS package there so the
# debug image inherits the same identity and never replaces it with Debian SSH.
SYNCD_VS_BAZEL_FIPS_DEBS = $(if $(filter y,$(INCLUDE_FIPS)),$(FIPS_OPENSSH_CLIENT))
SYNCD_VS_BAZEL_RUNTIME_DEBS = $(filter-out $(SYNCD_VS_BAZEL_SOURCE_DEBS),$(LIBNL3_DEV) $(LIBNL_ROUTE3_DEV) $(call expand,$($(DOCKER_SYNCD_BASE)_DEPENDS),RDEPENDS) $(SYNCD_VS_BAZEL_FIPS_DEBS))
SYNCD_VS_BAZEL_DEBUG_DEBS = $(filter-out $(SYNCD_VS_BAZEL_SOURCE_DEBS) $(SYNCD_VS_BAZEL_FIPS_DEBS),$(call expand,$($(DOCKER_SYNCD_BASE)_DBG_DEPENDS),RDEPENDS))
# This separate candidate supplies only symbols matched against the final image.
# Its old libswsscommon symbols must not enter the ordinary debug package layer.
SYNCD_VS_BAZEL_BASE_DEBUG_DEBS = $(LIBSWSSCOMMON_DBG)

$(DOCKER_SYNCD_BASE)_BAZEL_TARGET = //dockers/docker-syncd-vs:docker-syncd-vs.gz
$(DOCKER_SYNCD_BASE_DBG)_BAZEL_TARGET = //dockers/docker-syncd-vs:docker-syncd-vs-dbg.gz
$(DOCKER_SYNCD_BASE)_BAZEL_OCI_TARGET = //dockers/docker-syncd-vs:docker-syncd-vs
$(DOCKER_SYNCD_BASE_DBG)_BAZEL_OCI_TARGET = //dockers/docker-syncd-vs:docker-syncd-vs-dbg
$(DOCKER_SYNCD_BASE)_BAZEL_DEPENDS = $(TARGET_PATH)/docker-config-engine-trixie.oci \
    $(addprefix $(SYNCD_VS_BAZEL_DEBS_PATH)/,$(SYNCD_VS_BAZEL_RUNTIME_DEBS)) \
    $(TARGET_PATH)/bazel-manifests/docker-syncd-vs/manifest.json
$(DOCKER_SYNCD_BASE_DBG)_BAZEL_DEPENDS = $($(DOCKER_SYNCD_BASE)_BAZEL_DEPENDS) \
    $(addprefix $(SYNCD_VS_BAZEL_DEBS_PATH)/,$(SYNCD_VS_BAZEL_DEBUG_DEBS) $(SYNCD_VS_BAZEL_BASE_DEBUG_DEBS)) \
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
