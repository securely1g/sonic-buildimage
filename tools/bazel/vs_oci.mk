# Post-installer CI work shares this slave's private Bazel server/output tree.
# Keep owner recipes and dependency checks; do not combine different image
# configurations into one Bazel command or share server state across builders.
ifneq ($(filter bazel-vs-oci bazel-vs-oci-verify,$(MAKECMDGOALS)),)
ifneq ($(BUILD_WITH_BAZEL_WHEN_AVAILABLE):$(BLDENV):$(CONFIGURED_PLATFORM):$(CONFIGURED_ARCH):$(INCLUDE_FIPS):$(INCLUDE_VS_DASH_SAI),y:trixie:vs:amd64:y:y)
$(error bazel-vs-oci requires native AMD64 Trixie VS with Bazel, FIPS and DASH enabled)
endif
ifneq ($(filter y,$(CROSS_BUILD_ENVIRON) $(MULTIARCH_QEMU_ENVIRON) $(ENABLE_ASAN) $(ENABLE_SYNCD_RPC)),)
$(error bazel-vs-oci does not support cross/emulated, ASAN or Syncd RPC builds)
endif
ifneq ($(sort $(filter docker-orchagent.gz docker-syncd-vs.gz,$(SONIC_BAZEL_DOCKER_IMAGES))),docker-orchagent.gz docker-syncd-vs.gz)
$(error bazel-vs-oci requires both runtime images to select their Bazel producers)
endif
ifneq ($(sort $(filter docker-orchagent-dbg.gz docker-syncd-vs-dbg.gz,$(SONIC_BAZEL_DBG_DOCKER_IMAGES))),docker-orchagent-dbg.gz docker-syncd-vs-dbg.gz)
$(error bazel-vs-oci requires both debug images to select their Bazel producers)
endif

.PHONY: bazel-vs-oci bazel-vs-oci-verify
# Serialize these prerequisites even if an inner Make inherits a jobserver.
# The image owners retain their ordinary prerequisite and atomic export rules.
.NOTPARALLEL: bazel-vs-oci bazel-vs-oci-verify
# Finish the legacy P4 workspace first; it shuts down its own Bazel server.
# Keep Syncd debug immediately before verification in the image workspace.
bazel-vs-oci: $(addprefix $(DEBS_PATH)/,$(SONIC_P4RT) $(SONIC_P4RT_DBG)) \
        $(addprefix $(TARGET_PATH)/,$(DOCKER_ORCHAGENT_DBG) $(DOCKER_SYNCD_BASE) $(DOCKER_SYNCD_BASE_DBG))
	bash tools/bazel/ci/verify_vs_oci.sh "$(DEBS_PATH)/$(SONIC_P4RT)" "$(DEBS_PATH)/$(SONIC_P4RT_DBG)"

# CI has already checked all four archives immediately after the VS build.
# Keep archive producers out of this target so missing outputs cannot be hidden
# by a second build. The existing image test consumes the retained archives.
bazel-vs-oci-verify: $(addprefix $(DEBS_PATH)/,$(SONIC_P4RT) $(SONIC_P4RT_DBG))
	bash tools/bazel/ci/verify_vs_oci.sh "$(DEBS_PATH)/$(SONIC_P4RT)" "$(DEBS_PATH)/$(SONIC_P4RT_DBG)"
endif
