# Prepare explicit package payload inputs before the shared bridge invokes Bazel.
# These recipes only inspect and extract existing Make DEBs. They never invoke Bazel.

SYNCD_VS_BAZEL_INPUT_ROOT = $(TARGET_PATH)/bazel-inputs/docker-syncd-vs
SYNCD_VS_BAZEL_DEBS_PATH = $(if $($(DOCKER_SYNCD_BASE)_DEBS_PATH),$($(DOCKER_SYNCD_BASE)_DEBS_PATH),$(DEBS_PATH))

# Dockerfile.j2 installs the two libnl development packages first, then the
# dependency-first runtime expansion used by slave.mk. The preparation helper
# keeps the first occurrence of a repeated package, matching the legacy recipe.
# P4C's transitive MPI dependency installs an SSH client in runtime. Select the
# Make FIPS package there so debug inherits the same OpenSSH files and identity.
SYNCD_VS_BAZEL_FIPS_DEBS = $(if $(filter y,$(INCLUDE_FIPS)),$(FIPS_OPENSSH_CLIENT))
SYNCD_VS_BAZEL_RUNTIME_DEBS = $(LIBNL3_DEV) $(LIBNL_ROUTE3_DEV) $(call expand,$($(DOCKER_SYNCD_BASE)_DEPENDS),RDEPENDS) $(SYNCD_VS_BAZEL_FIPS_DEBS)
SYNCD_VS_BAZEL_DEBUG_DEBS = $(filter-out $(SYNCD_VS_BAZEL_FIPS_DEBS),$(call expand,$($(DOCKER_SYNCD_BASE)_DBG_DEPENDS),RDEPENDS))
SYNCD_VS_BAZEL_RUNTIME_REQUIRED = syncd-vs libsairedis libsaimetadata libsaivs libswsscommon libsai p4lang-pi p4lang-bmv2 p4lang-p4c libnl-3-dev libnl-route-3-dev libnl-3-200 libnl-genl-3-200 libnl-route-3-200 libnl-nf-3-200 libnl-cli-3-200 libyang3 openssh-client
SYNCD_VS_BAZEL_DEBUG_REQUIRED = syncd-vs-dbgsym libsairedis-dbgsym libsaimetadata-dbgsym libsaivs-dbgsym libswsscommon-dbgsym libyang3-dbgsym python3-swsscommon-dbgsym sonic-db-cli-dbgsym sonic-eventd-dbgsym

.SECONDEXPANSION:
.PHONY: syncd-vs-bazel-inputs-force
syncd-vs-bazel-inputs-force:

$(SYNCD_VS_BAZEL_INPUT_ROOT)/runtime/manifest.json: syncd-vs-bazel-inputs-force \
        dockers/docker-syncd-vs/bazel/prepare_packages.py \
        $$(addprefix $$(SYNCD_VS_BAZEL_DEBS_PATH)/,$$(SYNCD_VS_BAZEL_RUNTIME_DEBS))
	python3 dockers/docker-syncd-vs/bazel/prepare_packages.py \
	    --output $(SYNCD_VS_BAZEL_INPUT_ROOT)/runtime --variant runtime \
	    --architecture $(CONFIGURED_ARCH) --distribution $(BLDENV) \
	    --include-vs-dash-sai $(INCLUDE_VS_DASH_SAI) --include-fips $(INCLUDE_FIPS) \
	    --enable-asan '$(ENABLE_ASAN)' --enable-syncd-rpc '$(ENABLE_SYNCD_RPC)' \
	    $(foreach package,$(SYNCD_VS_BAZEL_RUNTIME_REQUIRED),--required-package $(package)) \
	    $(foreach package,$(SYNCD_VS_BAZEL_RUNTIME_DEBS),--package $(SYNCD_VS_BAZEL_DEBS_PATH)/$(package))

$(SYNCD_VS_BAZEL_INPUT_ROOT)/debug/manifest.json: syncd-vs-bazel-inputs-force \
        dockers/docker-syncd-vs/bazel/prepare_packages.py \
        $(SYNCD_VS_BAZEL_INPUT_ROOT)/runtime/manifest.json \
        $$(addprefix $$(SYNCD_VS_BAZEL_DEBS_PATH)/,$$(SYNCD_VS_BAZEL_DEBUG_DEBS))
	python3 dockers/docker-syncd-vs/bazel/prepare_packages.py \
	    --output $(SYNCD_VS_BAZEL_INPUT_ROOT)/debug --variant debug \
	    --architecture $(CONFIGURED_ARCH) --distribution $(BLDENV) \
	    --include-vs-dash-sai $(INCLUDE_VS_DASH_SAI) --include-fips $(INCLUDE_FIPS) \
	    --enable-asan '$(ENABLE_ASAN)' --enable-syncd-rpc '$(ENABLE_SYNCD_RPC)' \
	    --runtime-manifest $(SYNCD_VS_BAZEL_INPUT_ROOT)/runtime/manifest.json \
	    $(foreach package,$(SYNCD_VS_BAZEL_DEBUG_REQUIRED),--required-package $(package)) \
	    $(foreach package,$($(DOCKER_SYNCD_BASE)_DBG_IMAGE_PACKAGES),--debug-apt-package $(package)) \
	    $(foreach package,$(SYNCD_VS_BAZEL_DEBUG_DEBS),--package $(SYNCD_VS_BAZEL_DEBS_PATH)/$(package))

# Final assembly stages regular prerequisites by hash. Each manifest recipe
# publishes its ordered aggregate tar in the same immutable generation.
$(SYNCD_VS_BAZEL_INPUT_ROOT)/runtime/payload.tar: $(SYNCD_VS_BAZEL_INPUT_ROOT)/runtime/manifest.json
	test -s "$@"

$(SYNCD_VS_BAZEL_INPUT_ROOT)/debug/payload.tar: $(SYNCD_VS_BAZEL_INPUT_ROOT)/debug/manifest.json
	test -s "$@"
