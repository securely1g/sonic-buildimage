# Opt-in producer for the existing Bazel VS assembly graph. Loaded by slave.mk
# after the normal native image definitions; ordinary Make goals are unchanged.
ifneq ($(filter bazel-vs-native-inputs,$(MAKECMDGOALS)),)
ifneq ($(strip $(MAKECMDGOALS)),bazel-vs-native-inputs)
$(error Invoke bazel-vs-native-inputs by itself)
endif
ifneq ($(CONFIGURED_PLATFORM)/$(CONFIGURED_ARCH)/$(BLDENV),vs/amd64/trixie)
$(error bazel-vs-native-inputs requires AMD64 Trixie VS)
endif
ifneq ($(BAZEL_MIN_READINESS),bazel_disabled)
$(error bazel-vs-native-inputs keeps native services on Make; use BAZEL_MIN_READINESS=bazel_disabled)
endif
ifneq ($(filter y,$(ENABLE_SBOM) $(ENABLE_ASAN) $(INSTALL_DEBUG_TOOLS) $(BUILD_MULTIASIC_KVM) $(MULTIARCH_QEMU_ENVIRON) $(CROSS_BUILD_ENVIRON) $(BUILD_REDUCE_IMAGE_SIZE) $(INCLUDE_KUBERNETES) $(INCLUDE_KUBERNETES_MASTER) $(SONIC_ENABLE_IMAGE_SIGNATURE)),)
$(error Unsupported feature in bazel-vs-native-inputs)
endif
ifneq ($(strip $(SONIC_PACKAGES) $(sonic-vs.bin_POST_BUILD_HOOK)),)
$(error Remote SONiC packages and installer post-build hooks are unsupported)
endif
ifneq ($(subst ",,$(SECURE_UPGRADE_MODE)),no_sign)
$(error bazel-vs-native-inputs requires SECURE_UPGRADE_MODE=no_sign)
endif

BAZEL_NATIVE_PREPARE := y
# Keep orchagent in the installed-image/service template inventory, but remove
# its archive prerequisite. The final Bazel graph owns this single image.
BAZEL_NATIVE_EXCLUDED_IMAGES := docker-orchagent.gz
BAZEL_NATIVE_SWSS_PREREQUISITES := $(addprefix $(TARGET_PATH)/,$(DOCKER_CONFIG_ENGINE_TRIXIE)) $(PYTHON_WHEELS_PATH)/$(SCAPY)
bazel_native_docker_closure = $(sort $(1) $(foreach image,$(1),$(call bazel_native_docker_closure,$($(image)_LOAD_DOCKERS) $($(image)_AFTER))))
BAZEL_NATIVE_SELECTED_DOCKERS := $(call bazel_native_docker_closure,$(sonic-vs.bin_DOCKERS) $(SONIC_PACKAGES_LOCAL))

.PHONY: bazel-vs-native-inputs bazel-native-inventory $(TARGET_PATH)/sonic-vs.bin
bazel-vs-native-inputs: $(TARGET_PATH)/sonic-vs.bin
	test -s target/bazel-native/provenance.json

$(TARGET_PATH)/sonic-vs.bin: $(BAZEL_NATIVE_SWSS_PREREQUISITES) | bazel-native-inventory

bazel-native-inventory: private export BAZEL_PLATFORM := $(CONFIGURED_PLATFORM)
bazel-native-inventory: private export BAZEL_ARCH := $(CONFIGURED_ARCH)
bazel-native-inventory: private export BAZEL_DISTRO := $(BLDENV)
bazel-native-inventory: private export BAZEL_SWSS := $(SWSS)
bazel-native-inventory: private export BAZEL_SELECTED_DOCKERS := $(BAZEL_NATIVE_SELECTED_DOCKERS)
bazel-native-inventory: private export BAZEL_INSTALLED_DOCKERS := $(sonic-vs.bin_DOCKERS)
bazel-native-inventory: private export BAZEL_LOCAL_PACKAGES := $(SONIC_PACKAGES_LOCAL)
bazel-native-inventory: private export BAZEL_REMOTE_PACKAGES := $(SONIC_PACKAGES)
bazel-native-inventory: private export BAZEL_RFS_DEPENDS := $(sonic-vs.bin_RFS_DEPENDS)
bazel-native-inventory: private export BAZEL_IMAGE_FILES := $(sonic-vs.bin_FILES)
bazel-native-inventory: private export BAZEL_IMAGE_INSTALLS := $(sonic-vs.bin_INSTALLS) $(sonic-vs.bin_LAZY_INSTALLS) $(sonic-vs.bin_LAZY_BUILD_INSTALLS)
bazel-native-inventory: private export BAZEL_SWSS_PREREQUISITES := $(BAZEL_NATIVE_SWSS_PREREQUISITES)
bazel-native-inventory: private export BAZEL_IMAGE_VERSION := $(SONIC_IMAGE_VERSION)
bazel-native-inventory: private export BAZEL_BUILD_TIMESTAMP := $(BUILD_TIMESTAMP)
bazel-native-inventory: private export BAZEL_BUILD_NUMBER := $(BUILD_NUMBER)
bazel-native-inventory: private export BAZEL_CONFIG_FLAGS := ENABLE_SBOM=$(ENABLE_SBOM) ENABLE_ASAN=$(ENABLE_ASAN) INSTALL_DEBUG_TOOLS=$(INSTALL_DEBUG_TOOLS) BUILD_MULTIASIC_KVM=$(BUILD_MULTIASIC_KVM) MULTIARCH_QEMU_ENVIRON=$(MULTIARCH_QEMU_ENVIRON) CROSS_BUILD_ENVIRON=$(CROSS_BUILD_ENVIRON) BUILD_REDUCE_IMAGE_SIZE=$(BUILD_REDUCE_IMAGE_SIZE) INCLUDE_KUBERNETES=$(INCLUDE_KUBERNETES) INCLUDE_KUBERNETES_MASTER=$(INCLUDE_KUBERNETES_MASTER) IMAGE_SIGNATURE=$(SONIC_ENABLE_IMAGE_SIGNATURE) SECURE_UPGRADE_MODE=$(SECURE_UPGRADE_MODE) BAZEL_MIN_READINESS=$(BAZEL_MIN_READINESS)
bazel-native-inventory:
	python3 tools/bazel/image/native/producer.py begin
endif
