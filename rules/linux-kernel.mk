# linux kernel package

KERNEL_VERSION = 6.12.41
KERNEL_ABISUFFIX = +deb13
KERNEL_SUBVERSION = 1
KERNEL_FEATURESET = sonic
# Note: KVERSION_SHORT is used by Arista
KVERSION_SHORT := $(KERNEL_VERSION)$(KERNEL_ABISUFFIX)-$(KERNEL_FEATURESET)
ifeq ($(CONFIGURED_ARCH), armhf)
# Override kernel version for ARMHF as it uses arm MP (multi-platform) for short version
KVERSION ?= $(KVERSION_SHORT)-armmp
else
KVERSION ?= $(KVERSION_SHORT)-$(CONFIGURED_ARCH)
endif

# Place an URL here to .tar.gz file if you want to include those patches
EXTERNAL_KERNEL_PATCH_URL =
# Set y to include non upstream patches tarball provided by the corresponding platform
INCLUDE_EXTERNAL_PATCHES ?= n
# platforms should override this and provide an absolute location to the patches
EXTERNAL_KERNEL_PATCH_LOC =

export KVERSION_SHORT KVERSION KERNEL_VERSION KERNEL_ABISUFFIX KERNEL_FEATURESET KERNEL_SUBVERSION
export EXTERNAL_KERNEL_PATCH_URL
export INCLUDE_EXTERNAL_PATCHES
export EXTERNAL_KERNEL_PATCH_LOC

LINUX_HEADERS_COMMON = linux-headers-$(KERNEL_VERSION)$(KERNEL_ABISUFFIX)-common-$(KERNEL_FEATURESET)_$(KERNEL_VERSION)-$(KERNEL_SUBVERSION)_all.deb
$(LINUX_HEADERS_COMMON)_SRC_PATH = $(SRC_PATH)/sonic-linux-kernel
ifeq ($(strip $(SONIC_BAZEL_KERNEL_PACKAGES)),)
SONIC_MAKE_DEBS += $(LINUX_HEADERS_COMMON)
else
# This explicit mode accepts only a verified bundle from the kernel producer.
ifneq ($(CONFIGURED_PLATFORM)/$(CONFIGURED_ARCH)/$(BLDENV),vs/amd64/trixie)
$(error Bazel kernel packages require AMD64 Trixie VS)
endif
ifneq ($(filter-out n,$(strip $(CROSS_BUILD_ENVIRON) $(MULTIARCH_QEMU_ENVIRON))),)
$(error Bazel kernel packages require native AMD64 execution)
endif
ifneq ($(KERNEL_VERSION)/$(KERNEL_ABISUFFIX)/$(KERNEL_SUBVERSION)/$(KERNEL_FEATURESET),6.12.41/+deb13/1/sonic)
$(error Bazel kernel packages require the 6.12.41-1 +deb13 sonic contract)
endif
ifneq ($(KVERSION),6.12.41+deb13-sonic-amd64)
$(error Bazel kernel packages require KVERSION=6.12.41+deb13-sonic-amd64)
endif
ifneq ($(strip $(ADDITIONAL_BUILD_PROFILES)),)
$(error Bazel kernel packages require the default kernel build profiles)
endif
ifneq ($(subst ",,$(SECURE_UPGRADE_MODE)),no_sign)
$(error Bazel kernel packages require SECURE_UPGRADE_MODE=no_sign)
endif
ifneq ($(INCLUDE_EXTERNAL_PATCHES),n)
$(error Bazel kernel packages do not support external platform patches)
endif
ifneq ($(SONIC_BAZEL_KERNEL_PACKAGES),target/bazel-kernel-inputs)
$(error Bazel kernel packages must be staged in target/bazel-kernel-inputs)
endif
SONIC_COPY_DEBS += $(LINUX_HEADERS_COMMON)
.PHONY: bazel-kernel-verify
bazel-kernel-verify:
	python3 -B tools/bazel/ci/kernel.py verify --bundle target/bazel-kernel-inputs
$(DEBS_PATH)/$(LINUX_HEADERS_COMMON): bazel-kernel-verify
endif

LINUX_KBUILD = linux-kbuild-$(KERNEL_VERSION)$(KERNEL_ABISUFFIX)_$(KERNEL_VERSION)-$(KERNEL_SUBVERSION)_$(CONFIGURED_ARCH).deb
$(eval $(call add_derived_package,$(LINUX_HEADERS_COMMON),$(LINUX_KBUILD)))

ifeq ($(CONFIGURED_ARCH), armhf)
	LINUX_KERNEL = linux-image-$(KVERSION)_$(KERNEL_VERSION)-$(KERNEL_SUBVERSION)_$(CONFIGURED_ARCH).deb
else
	LINUX_KERNEL = linux-image-$(KVERSION)-unsigned_$(KERNEL_VERSION)-$(KERNEL_SUBVERSION)_$(CONFIGURED_ARCH).deb
endif
$(eval $(call add_derived_package,$(LINUX_HEADERS_COMMON),$(LINUX_KERNEL)))

LINUX_HEADERS = linux-headers-$(KVERSION)_$(KERNEL_VERSION)-$(KERNEL_SUBVERSION)_$(CONFIGURED_ARCH).deb
$(LINUX_HEADERS)_DEPENDS += $(LINUX_KBUILD) $(LINUX_KERNEL)
$(LINUX_HEADERS)_RDEPENDS += $(LINUX_KBUILD) $(LINUX_KERNEL)
$(eval $(call add_derived_package,$(LINUX_HEADERS_COMMON),$(LINUX_HEADERS)))

ifneq ($(strip $(SONIC_BAZEL_KERNEL_PACKAGES)),)
$(foreach deb,$(LINUX_HEADERS_COMMON) $(LINUX_KBUILD) $(LINUX_KERNEL) $(LINUX_HEADERS),$(eval $(deb)_PATH := $(SONIC_BAZEL_KERNEL_PACKAGES)))
endif
