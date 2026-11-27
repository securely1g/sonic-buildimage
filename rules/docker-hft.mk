# docker image for High Frequency Telemetry (HFT): runs countersyncd

DOCKER_HFT_STEM = docker-hft
DOCKER_HFT = $(DOCKER_HFT_STEM).gz
DOCKER_HFT_DBG = $(DOCKER_HFT_STEM)-$(DBG_IMAGE_MARK).gz

$(DOCKER_HFT)_PATH = $(DOCKERS_PATH)/$(DOCKER_HFT_STEM)

# countersyncd is shipped by the swss package, which docker-swss-layer-trixie
# already installs, so docker-hft only adds its own scripts on top of it.
$(DOCKER_HFT)_DBG_DEPENDS = $($(DOCKER_SWSS_LAYER_TRIXIE)_DBG_DEPENDS)
$(DOCKER_HFT)_DBG_IMAGE_PACKAGES = $($(DOCKER_SWSS_LAYER_TRIXIE)_DBG_IMAGE_PACKAGES)

$(DOCKER_HFT)_LOAD_DOCKERS += $(DOCKER_SWSS_LAYER_TRIXIE)

$(DOCKER_HFT)_VERSION = 1.0.0
$(DOCKER_HFT)_PACKAGE_NAME = hft

SONIC_DOCKER_IMAGES += $(DOCKER_HFT)
SONIC_TRIXIE_DOCKERS += $(DOCKER_HFT)
ifeq ($(INCLUDE_HFT), y)
SONIC_INSTALL_DOCKER_IMAGES += $(DOCKER_HFT)
endif

SONIC_DOCKER_DBG_IMAGES += $(DOCKER_HFT_DBG)
SONIC_TRIXIE_DBG_DOCKERS += $(DOCKER_HFT_DBG)
ifeq ($(INCLUDE_HFT), y)
SONIC_INSTALL_DOCKER_DBG_IMAGES += $(DOCKER_HFT_DBG)
endif

$(DOCKER_HFT)_CONTAINER_NAME = hft
$(DOCKER_HFT)_RUN_OPT += -t --cap-add=NET_ADMIN
$(DOCKER_HFT)_RUN_OPT += -v /etc/sonic:/etc/sonic:ro
$(DOCKER_HFT)_RUN_OPT += -v /etc/localtime:/etc/localtime:ro

ifeq ($(BLDENV),trixie)
SONIC_BAZEL_SWITCHABLE_IMAGES += $(DOCKER_HFT) $(DOCKER_HFT_DBG)
ifeq ($(BUILD_WITH_BAZEL_WHEN_AVAILABLE):$(CONFIGURED_PLATFORM):$(CONFIGURED_ARCH):$(INCLUDE_FIPS):$(DBG_IMAGE_MARK),y:vs:amd64:y:dbg)
ifeq ($(filter y,$(CROSS_BUILD_ENVIRON) $(MULTIARCH_QEMU_ENVIRON) $(ENABLE_ASAN)),)
SONIC_BAZEL_DOCKER_IMAGES += $(DOCKER_HFT)
SONIC_BAZEL_DBG_DOCKER_IMAGES += $(DOCKER_HFT_DBG)
$(DOCKER_HFT_DBG)_PATH = $($(DOCKER_HFT)_PATH)
$(DOCKER_HFT)_BAZEL_TARGET = //dockers/docker-hft:$(DOCKER_HFT)
$(DOCKER_HFT_DBG)_BAZEL_TARGET = //dockers/docker-hft:$(DOCKER_HFT_DBG)
SONIC_BAZEL_MANIFESTS += docker-hft docker-hft-$(DBG_IMAGE_MARK)
SONIC_BAZEL_IMAGE_ENVIRONMENTS += docker-hft
docker-hft_MANIFEST_IMAGE = $(DOCKER_HFT)
docker-hft-$(DBG_IMAGE_MARK)_MANIFEST_IMAGE = $(DOCKER_HFT)
docker-hft-$(DBG_IMAGE_MARK)_MANIFEST_SUFFIX = $(DBG_IMAGE_MARK)
$(DOCKER_HFT)_BAZEL_DEPENDS = $(TARGET_PATH)/bazel-manifests/docker-base-trixie/manifest.json \
    $(TARGET_PATH)/bazel-manifests/docker-config-engine-trixie/manifest.json \
    $(TARGET_PATH)/bazel-manifests/docker-swss-layer-trixie/manifest.json \
    $(TARGET_PATH)/bazel-manifests/docker-hft/manifest.json \
    $(TARGET_PATH)/bazel-manifests/docker-hft/image.env
$(DOCKER_HFT_DBG)_BAZEL_DEPENDS = $($(DOCKER_HFT)_BAZEL_DEPENDS) $(TARGET_PATH)/bazel-manifests/docker-hft-$(DBG_IMAGE_MARK)/manifest.json
endif
endif
endif
