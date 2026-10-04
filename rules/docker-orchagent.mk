# docker image for orchagent

DOCKER_ORCHAGENT_STEM = docker-orchagent
DOCKER_ORCHAGENT = $(DOCKER_ORCHAGENT_STEM).gz
DOCKER_ORCHAGENT_DBG = $(DOCKER_ORCHAGENT_STEM)-$(DBG_IMAGE_MARK).gz

ifneq ($(BUILD_SWSS_WITH_BAZEL),y)
ifneq ($(BUILD_SWSS_WITH_BAZEL),n)
$(error BUILD_SWSS_WITH_BAZEL must be y or n)
endif
endif

# The top-level Make build still visits bookworm for its legacy dependencies.
# Select Bazel only in the trixie slave that owns the installed SWSS container.
ifeq ($(BUILD_SWSS_WITH_BAZEL),y)
ifeq ($(BLDENV),trixie)
ifneq ($(CONFIGURED_PLATFORM),vs)
$(error BUILD_SWSS_WITH_BAZEL=y requires PLATFORM=vs)
endif
ifneq ($(CONFIGURED_ARCH),amd64)
$(error BUILD_SWSS_WITH_BAZEL=y requires PLATFORM_ARCH=amd64)
endif
ifneq ($(filter y,$(CROSS_BUILD_ENVIRON) $(MULTIARCH_QEMU_ENVIRON)),)
$(error BUILD_SWSS_WITH_BAZEL=y requires a native amd64 build)
endif
ifeq ($(ENABLE_ASAN),y)
$(error BUILD_SWSS_WITH_BAZEL=y does not support ENABLE_ASAN=y)
endif
SONIC_BAZEL_SWSS_IMAGES += $(DOCKER_ORCHAGENT) $(DOCKER_ORCHAGENT_DBG)
endif
endif

# Keep the legacy metadata for consumers such as docker-sonic-vs, which merges
# these package and wheel lists into its own Make build. The Bazel SWSS recipe
# below uses its own explicit prerequisites instead of these package targets.
$(DOCKER_ORCHAGENT)_DEPENDS += $(SWSS) $(LIB_SONIC_DASH_API)

ifeq ($(ENABLE_ASAN), y)
$(DOCKER_ORCHAGENT)_DEPENDS += $(SWSS_DBG)
endif

$(DOCKER_ORCHAGENT)_DBG_DEPENDS = $($(DOCKER_SWSS_LAYER_TRIXIE)_DBG_DEPENDS)
$(DOCKER_ORCHAGENT)_DBG_DEPENDS +=   $(SWSS_DBG) \
                                $(LIBSWSSCOMMON_DBG) \
                                $(LIBSAIREDIS_DBG)
$(DOCKER_ORCHAGENT)_PYTHON_WHEELS += $(SCAPY)

$(DOCKER_ORCHAGENT)_DBG_IMAGE_PACKAGES = $($(DOCKER_SWSS_LAYER_TRIXIE)_DBG_IMAGE_PACKAGES)

$(DOCKER_ORCHAGENT)_PATH = $(DOCKERS_PATH)/$(DOCKER_ORCHAGENT_STEM)

$(DOCKER_ORCHAGENT)_LOAD_DOCKERS += $(DOCKER_SWSS_LAYER_TRIXIE)

$(DOCKER_ORCHAGENT)_VERSION = 1.0.0
$(DOCKER_ORCHAGENT)_PACKAGE_NAME = swss
$(DOCKER_ORCHAGENT)_WARM_SHUTDOWN_BEFORE = syncd
$(DOCKER_ORCHAGENT)_FAST_SHUTDOWN_BEFORE = syncd

SONIC_DOCKER_IMAGES += $(DOCKER_ORCHAGENT)
SONIC_TRIXIE_DOCKERS += $(DOCKER_ORCHAGENT)
SONIC_INSTALL_DOCKER_IMAGES += $(DOCKER_ORCHAGENT)

SONIC_DOCKER_DBG_IMAGES += $(DOCKER_ORCHAGENT_DBG)
SONIC_TRIXIE_DBG_DOCKERS += $(DOCKER_ORCHAGENT_DBG)
SONIC_INSTALL_DOCKER_DBG_IMAGES += $(DOCKER_ORCHAGENT_DBG)

$(DOCKER_ORCHAGENT)_CONTAINER_NAME = swss
$(DOCKER_ORCHAGENT)_RUN_OPT += -t --cap-add=NET_ADMIN --security-opt apparmor=unconfined --security-opt="systempaths=unconfined"
ifeq ($(ENABLE_ASAN), y)
$(DOCKER_ORCHAGENT)_RUN_OPT += --cap-add=SYS_PTRACE
endif
$(DOCKER_ORCHAGENT)_RUN_OPT += -v /etc/network/interfaces:/etc/network/interfaces:ro
$(DOCKER_ORCHAGENT)_RUN_OPT += -v /etc/localtime:/etc/localtime:ro 
$(DOCKER_ORCHAGENT)_RUN_OPT += -v /etc/network/interfaces.d/:/etc/network/interfaces.d/:ro
$(DOCKER_ORCHAGENT)_RUN_OPT += -v /host/machine.conf:/host/machine.conf:ro
$(DOCKER_ORCHAGENT)_RUN_OPT += -v /etc/sonic:/etc/sonic:ro
$(DOCKER_ORCHAGENT)_RUN_OPT += -v /var/log/swss:/var/log/swss:rw
$(DOCKER_ORCHAGENT)_RUN_OPT += -v /zmq_swss:/zmq_swss:rw

$(DOCKER_ORCHAGENT)_BASE_IMAGE_FILES += swssloglevel:/usr/bin/swssloglevel
$(DOCKER_ORCHAGENT)_FILES += $(ARP_UPDATE_SCRIPT) $(ARP_UPDATE_VARS_TEMPLATE)
