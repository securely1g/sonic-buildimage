# SONiC make file

NOJESSIE ?= 1
NOSTRETCH ?= 1
NOBUSTER ?= 1
NOBULLSEYE ?= 1
NOBOOKWORM ?= 0
NOTRIXIE ?= 0

override Q := @
ifeq ($(QUIET),n)
  override Q := 
endif
override SONIC_OVERRIDE_BUILD_VARS += $(SONIC_BUILD_VARS)
override SONIC_OVERRIDE_BUILD_VARS += Q=$(Q)
export Q SONIC_OVERRIDE_BUILD_VARS

ifeq ($(NOJESSIE),0)
BUILD_JESSIE=1
endif

ifeq ($(NOSTRETCH),0)
BUILD_STRETCH=1
endif

ifeq ($(NOBUSTER),0)
BUILD_BUSTER=1
endif

ifeq ($(NOBULLSEYE),0)
BUILD_BULLSEYE=1
endif

ifeq ($(NOBOOKWORM),0)
BUILD_BOOKWORM=1
endif

ifeq ($(NOTRIXIE),0)
BUILD_TRIXIE=1
endif

PLATFORM_PATH := platform/$(if $(PLATFORM),$(PLATFORM),$(CONFIGURED_PLATFORM))
PLATFORM_CHECKOUT := platform/checkout
PLATFORM_CHECKOUT_FILE := $(PLATFORM_CHECKOUT)/$(PLATFORM).ini
PLATFORM_CHECKOUT_CMD := $(shell if [ -f $(PLATFORM_CHECKOUT_FILE) ]; then PLATFORM_REPO=$(PLATFORM_REPO) PLATFORM_REF=$(PLATFORM_REF) PLATFORM_PATH=$(PLATFORM_PATH) j2 $(PLATFORM_CHECKOUT)/template.j2 $(PLATFORM_CHECKOUT_FILE); fi)
MAKE_WITH_RETRY := ./scripts/run_with_retry $(MAKE)
# Native preparation builds predecessors only; store selection belongs to the
# final assembler. Clear a CLI or saved backend setting for every sub-make.
SONIC_NATIVE_INPUT_OPTIONS = $(if $(filter bazel-vs-native-inputs,$@),USE_CONTAINER_IMAGE_STORE_AS_BACKEND=n)

%::
	@echo "+++ --- Making $@ --- +++"
ifeq ($(NOJESSIE), 0)
	$(MAKE_WITH_RETRY) $(SONIC_NATIVE_INPUT_OPTIONS) EXTRA_DOCKER_TARGETS=$(notdir $@) -f Makefile.work jessie
endif
ifeq ($(NOSTRETCH), 0)
	$(MAKE_WITH_RETRY) $(SONIC_NATIVE_INPUT_OPTIONS) EXTRA_DOCKER_TARGETS=$(notdir $@) BLDENV=stretch -f Makefile.work stretch
endif
ifeq ($(NOBUSTER), 0)
	$(MAKE_WITH_RETRY) $(SONIC_NATIVE_INPUT_OPTIONS) EXTRA_DOCKER_TARGETS=$(notdir $@) BLDENV=buster -f Makefile.work buster
endif
ifeq ($(NOBULLSEYE), 0)
	$(MAKE_WITH_RETRY) $(SONIC_NATIVE_INPUT_OPTIONS) EXTRA_DOCKER_TARGETS=$(notdir $@) BLDENV=bullseye -f Makefile.work bullseye
endif
ifeq ($(NOBOOKWORM), 0)
	$(MAKE_WITH_RETRY) $(SONIC_NATIVE_INPUT_OPTIONS) EXTRA_DOCKER_TARGETS=$(notdir $@) BLDENV=bookworm -f Makefile.work bookworm
endif
ifeq ($(NOTRIXIE), 0)
	$(MAKE_WITH_RETRY) $(SONIC_NATIVE_INPUT_OPTIONS) BLDENV=trixie -f Makefile.work $@
endif

	BLDENV=bookworm $(MAKE) $(SONIC_NATIVE_INPUT_OPTIONS) -f Makefile.work docker-cleanup

jessie:
	@echo "+++ Making $@ +++"
ifeq ($(NOJESSIE), 0)
	$(MAKE) -f Makefile.work jessie
endif

stretch:
	@echo "+++ Making $@ +++"
ifeq ($(NOSTRETCH), 0)
	$(MAKE) -f Makefile.work stretch
endif

buster:
	@echo "+++ Making $@ +++"
ifeq ($(NOBUSTER), 0)
	$(MAKE) -f Makefile.work buster
endif

bullseye:
	@echo "+++ Making $@ +++"
ifeq ($(NOBULLSEYE), 0)
	$(MAKE) -f Makefile.work bullseye
endif

trixie:
	@echo "+++ Making $@ +++"
ifeq ($(NOTRIXIE), 0)
	$(MAKE) -f Makefile.work trixie
endif

init reset:
	@echo "+++ Making $@ +++"
	$(MAKE) -f Makefile.work $@

#
# Function to invoke target $@ in Makefile.work with proper BLDENV
#
define make_work
	@echo "+++ Making $@ +++"
	$(if $(BUILD_JESSIE),$(MAKE) -f Makefile.work $@,)
	$(if $(BUILD_STRETCH),BLDENV=stretch $(MAKE) -f Makefile.work $@,)
	$(if $(BUILD_BUSTER),BLDENV=buster $(MAKE) -f Makefile.work $@,)
	$(if $(BUILD_BULLSEYE),BLDENV=bullseye $(MAKE) -f Makefile.work $@,)
	$(if $(BUILD_BOOKWORM),BLDENV=bookworm $(MAKE) -f Makefile.work $@,)
	$(if $(BUILD_TRIXIE),BLDENV=trixie $(MAKE) -f Makefile.work $@,)
endef

.PHONY: $(PLATFORM_PATH)

$(PLATFORM_PATH):
	@echo "+++ Checking $@ +++"
	$(PLATFORM_CHECKOUT_CMD)

configure : $(PLATFORM_PATH)
	$(call make_work, $@)

clean showtag docker-cleanup clean-docker sonic-slave-build sonic-slave-bash :
	$(call make_work, $@)

# Freeze the versions, see more detail options: scripts/versions_manager.py freeze -h
freeze:
	@scripts/versions_manager.py freeze $(FREEZE_VERSION_OPTIONS)

# Let the inner build check changed sources and Make/Bazel builder switches even
# when these outputs already exist. Keep the outputs non-phony so they retain
# the implicit forwarding recipe above, in both builder modes.
.PHONY: sonic-swss-forward
sonic-swss-forward:

target/docker-orchagent.gz target/docker-orchagent-dbg.gz \
target/sonic-vs.bin target/sonic-vs.img.gz target/sonic-vs.raw: sonic-swss-forward

# Existing kernel/module outputs still need the inner Make dependency checks.
# Filter only requested goals, so this does not enumerate or build unrelated
# outputs. Keep forwarding in both modes to detect a Bazel/Make builder change.
$(filter target/debs/trixie/linux-%.deb target/debs/trixie/linux-%.deb-install \
    target/debs/trixie/genl-packet-module_%.deb target/debs/trixie/genl-packet-module_%.deb-install \
    target/files/trixie/ixgbe.ko target/%rfs.squashfs,$(MAKECMDGOALS)): sonic-swss-forward

# Select only the explicit VS ONIE goal here. Other component and KVM goals keep
# the existing BUILD_WITH_BAZEL_WHEN_AVAILABLE behavior in Makefile.work.
ifneq ($(filter target/sonic-vs.bin,$(MAKECMDGOALS)),)
SONIC_IMAGE_CONFIG_FILES := rules/config $(wildcard rules/config.organization rules/config.user)
$(SONIC_IMAGE_CONFIG_FILES): ;
include $(SONIC_IMAGE_CONFIG_FILES)
ifneq ($(words $(BUILD_WITH_BAZEL_WHEN_AVAILABLE)),1)
$(error BUILD_WITH_BAZEL_WHEN_AVAILABLE must be y or n)
endif
ifneq ($(filter $(BUILD_WITH_BAZEL_WHEN_AVAILABLE),y n),$(BUILD_WITH_BAZEL_WHEN_AVAILABLE))
$(error BUILD_WITH_BAZEL_WHEN_AVAILABLE must be y or n)
endif
ifneq ($(words $(USE_CONTAINER_IMAGE_STORE_AS_BACKEND)),1)
$(error USE_CONTAINER_IMAGE_STORE_AS_BACKEND must be y or n)
endif
ifneq ($(filter $(USE_CONTAINER_IMAGE_STORE_AS_BACKEND),y n),$(USE_CONTAINER_IMAGE_STORE_AS_BACKEND))
$(error USE_CONTAINER_IMAGE_STORE_AS_BACKEND must be y or n)
endif
ifeq ($(USE_CONTAINER_IMAGE_STORE_AS_BACKEND),y)
ifneq ($(BUILD_WITH_BAZEL_WHEN_AVAILABLE),y)
$(error USE_CONTAINER_IMAGE_STORE_AS_BACKEND=y requires BUILD_WITH_BAZEL_WHEN_AVAILABLE=y)
endif
endif
ifeq ($(BUILD_WITH_BAZEL_WHEN_AVAILABLE),y)
ifneq ($(strip $(MAKECMDGOALS)),target/sonic-vs.bin)
$(error Bazel VS assembly supports only the explicit target/sonic-vs.bin goal)
endif
ifneq ($(filter-out amd64,$(PLATFORM_ARCH)),)
$(error Bazel VS assembly requires PLATFORM_ARCH=amd64)
endif
ifneq ($(filter-out vs,$(PLATFORM)),)
$(error Bazel VS assembly requires PLATFORM=vs)
endif
ifneq ($(filter-out trixie,$(BLDENV)),)
$(error Bazel VS assembly requires BLDENV=trixie)
endif
# A separate assembler checkout may be unconfigured. If it has a saved native
# configuration, do not publish an older VS bundle for a conflicting target.
ifneq ($(wildcard .arch),)
ifneq ($(strip $(shell cat .arch)),amd64)
$(error Bazel VS assembly requires saved .arch=amd64)
endif
endif
ifneq ($(wildcard .platform),)
ifneq ($(strip $(shell cat .platform)),vs)
$(error Bazel VS assembly requires saved .platform=vs)
endif
endif
# Assembly uses frozen native inputs, so current Make options cannot silently
# request signing, debug payloads or other features absent from that bundle.
BAZEL_VS_UNSUPPORTED_OPTIONS := $(foreach option,ENABLE_SBOM ENABLE_ASAN \
    SONIC_INSTALL_DEBUG_TOOLS INSTALL_DEBUG_TOOLS BUILD_MULTIASIC_KVM \
    MULTIARCH_QEMU_ENVIRON CROSS_BUILD_ENVIRON BUILD_REDUCE_IMAGE_SIZE \
    INCLUDE_KUBERNETES INCLUDE_KUBERNETES_MASTER SONIC_ENABLE_IMAGE_SIGNATURE \
    ENABLE_IMAGE_SIGNATURE,$(if $(filter-out n,$($(option))),$(option)))
ifneq ($(strip $(BAZEL_VS_UNSUPPORTED_OPTIONS)),)
$(error Bazel VS assembly requires these options disabled: $(strip $(BAZEL_VS_UNSUPPORTED_OPTIONS)))
endif
ifneq ($(subst ",,$(strip $(SECURE_UPGRADE_MODE))),no_sign)
$(error Bazel VS assembly requires SECURE_UPGRADE_MODE=no_sign)
endif
ifneq ($(strip $(SONIC_PACKAGES) $(sonic-vs.bin_POST_BUILD_HOOK)),)
$(error Bazel VS assembly does not support remote packages or installer post-build hooks)
endif
endif

# Serialize publication and safely replace a previous Bazel image when switching
# back to Make. The native child uses the existing forwarding recipe above.
ifneq ($(SONIC_BAZEL_NATIVE_PUBLISH),y)
BAZEL_VS_LAUNCHER_OPTIONS = \
    $(if $(BAZEL_IMAGE_MOUNT_ROOT),--mount-root "$(BAZEL_IMAGE_MOUNT_ROOT)") \
    $(if $(BAZEL_IMAGE_OUTPUT_USER_ROOT),--output-user-root "$(BAZEL_IMAGE_OUTPUT_USER_ROOT)") \
    $(if $(BAZEL_IMAGE_REPOSITORY_CACHE),--repository-cache "$(BAZEL_IMAGE_REPOSITORY_CACHE)") \
    $(if $(BAZEL_IMAGE_WORKER),--persistent-worker "$(BAZEL_IMAGE_WORKER)") \
    $(if $(BAZEL_IMAGE_GIT_CONFIG),--git-config "$(BAZEL_IMAGE_GIT_CONFIG)")
.PHONY: target/sonic-vs.bin
target/sonic-vs.bin:
	python3 tools/bazel/image/build.py --build-with-bazel-when-available "$(BUILD_WITH_BAZEL_WHEN_AVAILABLE)" --backend "$(USE_CONTAINER_IMAGE_STORE_AS_BACKEND)" $(BAZEL_VS_LAUNCHER_OPTIONS)
endif
endif
