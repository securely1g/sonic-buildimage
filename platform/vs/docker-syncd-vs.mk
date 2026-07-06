# docker image for vs syncd

DOCKER_SYNCD_PLATFORM_CODE = vs
include $(PLATFORM_PATH)/../template/docker-syncd-trixie.mk

# Stable handle for including this docker via _INCLUDE_DOCKER in other images.
# DOCKER_SYNCD_BASE is reassigned by every syncd platform, so capture it now.
DOCKER_SYNCD_VS := $(DOCKER_SYNCD_BASE)

$(DOCKER_SYNCD_BASE)_DEPENDS += $(SYNCD_VS) \
                              $(LIBNL3_DEV) \
                              $(LIBNL3)

$(DOCKER_SYNCD_BASE)_DBG_DEPENDS += $(SYNCD_VS_DBG) \
                                $(LIBSWSSCOMMON_DBG) \
                                $(LIBSAIMETADATA_DBG) \
                                $(LIBSAIREDIS_DBG) \
                                $(LIBSAIVS_DBG)

$(DOCKER_SYNCD_BASE)_VERSION = 1.0.0
$(DOCKER_SYNCD_BASE)_PACKAGE_NAME = syncd

