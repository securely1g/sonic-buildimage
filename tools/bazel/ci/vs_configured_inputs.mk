# Configured declaration observation only; no selection manifests or package prerequisites.
ifneq ($(SONIC_VS_INPUT_INVENTORY),y)
$(error Configured input observation requires SONIC_VS_INPUT_INVENTORY=y)
endif
ifneq ($(strip $(MAKECMDGOALS)),vs-input-inventory)
$(error Inventory permits one metadata goal only)
endif

# Reuse the passed CAP5 capture. Its image-selection target is deliberately not a dependency.
include $(dir $(lastword $(MAKEFILE_LIST)))vs_reuse_selection.mk

# Preserve the reviewed inventory rule: never remake parsed or absent optional includes.
VS_INPUT_KNOWN_MAKEFILES := slave.mk Makefile.cache rules/config rules/functions rules/config.organization rules/config.user $(RULES_PATH)/*.mk $(RULES_PATH)/*.dep $(PLATFORM_PATH)/rules.mk $(PLATFORM_PATH)/rules.dep $(if $(PLATFORM_PDDF_PATH),$(PLATFORM_PDDF_PATH)/rules.mk $(PLATFORM_PDDF_PATH)/rules.dep) $(PLATFORM_PATH)/*.mk $(PLATFORM_PATH)/*.dep platform/template/docker-syncd-trixie.mk platform/template/docker-gbsyncd-trixie.mk tools/bazel/docker.mk tools/bazel/manifests.mk
$(sort $(MAKEFILE_LIST) $(VS_INPUT_KNOWN_MAKEFILES)):: ;

.PHONY: vs-input-inventory
vs-input-inventory:
	$(call _vs_reuse_capture_inputs)
