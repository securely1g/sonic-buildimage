# Supplemental controller-only goal; pass after the unchanged source slave.mk.
# It resolves metadata and records the planned selection without an installer build.
ifneq ($(SONIC_DEB_BUILD_POLICY),reuse-only)
$(error VS archive reuse requires SONIC_DEB_BUILD_POLICY=reuse-only)
endif
ifneq ($(SONIC_BAZEL_KERNEL_PREPARE),verify)
$(error VS archive reuse requires SONIC_BAZEL_KERNEL_PREPARE=verify)
endif
ifneq ($(SONIC_BAZEL_VS_ARCHIVE_PROFILE),final23)
$(error VS archive reuse requires SONIC_BAZEL_VS_ARCHIVE_PROFILE=final23)
endif
ifneq ($(words $(sonic-vs.bin_MACHINE)),1)
$(error Expected one original Make machine for sonic-vs.bin)
endif

# Record only public build inputs. GNU Make writes the data directly: package
# names and values never become shell arguments. This is a fresh capture, not
# permission to assemble an image or to generate any package.
ifneq ($(TARGET_PATH),target)
$(error VS reuse metadata requires TARGET_PATH=target)
endif
override _VS_REUSE_METADATA_PATH := target/sonic-vs.bin.make-inputs.tsv
override _VS_REUSE_METADATA_VARIABLES := \
    TARGET_PATH DEBS_PATH IMAGE_DISTRO_DEBS_PATH PYTHON_WHEELS_PATH FILES_PATH \
    RULES_PATH LINUX_KERNEL_VERSION KVERSION SONIC_PACKAGES \
    PHONY_PATH CONFIGURED_ARCH CONFIGURED_PLATFORM BLDENV SONIC_IMAGE_VERSION \
    INSTALL_DEBUG_TOOLS BUILD_WITH_BAZEL_WHEN_AVAILABLE SONIC_BAZEL_VS_ARCHIVE_PROFILE SONIC_DEB_BUILD_POLICY SONIC_BAZEL_KERNEL_PREPARE \
    SONIC_BAZEL_KERNEL_INPUTS SONIC_ROOTFS_DOCKER_NETWORK SONIC_DPKG_CACHE_METHOD SONIC_DPKG_CACHE_METHOD_OVERRIDE INCLUDE_FIPS \
    INCLUDE_VS_DASH_SAI ENABLE_ASAN ENABLE_SYNCD_RPC CROSS_BUILD_ENVIRON MULTIARCH_QEMU_ENVIRON \
    INCLUDE_SYSTEM_EVENTD BUILD_REDUCE_IMAGE_SIZE ENABLE_ZTP PDDF_SUPPORT ENABLE_ORGANIZATION_EXTENSIONS SECURE_UPGRADE_MODE INITRAMFS_TOOLS \
    INITRAMFS_TOOLS_CORE LINUX_KERNEL LINUX_KERNEL_DBG LINUX_KBUILD LINUX_HEADERS \
    SONIC_DEVICE_DATA IFUPDOWN2 MAKEDUMPFILE KDUMP_TOOLS LIBPAM_RADIUS \
    LIBNSS_RADIUS LIBPAM_TACPLUS LIBNSS_TACPLUS MONIT OPENSSH_SERVER \
    OPENSSH_VERSION_FULL PYTHON3_SWSSCOMMON SONIC_DB_CLI SONIC_NETTOOLS SONIC_RSYSLOG_PLUGIN \
    SONIC_UTILITIES_DATA SONIC_CTRMGRD_RS SONIC_HOST_SERVICES_RS SONIC_HOST_SERVICES_DATA SOCAT \
    BASH BASH_TACPLUS AUDISP_TACPLUS SYSLOG_COUNTER SEDUTIL \
    SONIC_ZTP SONIC_INSTALLER_EXTRA_DEBS SYMCRYPT_OPENSSL FIPS_BASEIMAGE_INSTALLERS FIPS_BASEIMAGE_PYTHON_INSTALLERS \
    RASDAEMON LIBSWSSCOMMON PYTHON_SWSSCOMMON SONIC_UTILITIES_PY3 SONIC_PY_COMMON_PY2 \
    SONIC_PY_COMMON_PY3 SONIC_CONFIG_ENGINE_PY2 SONIC_CONFIG_ENGINE_PY3 SONIC_PLATFORM_COMMON_PY3 REDIS_DUMP_LOAD_PY2 \
    REDIS_DUMP_LOAD_PY3 SONIC_PLATFORM_API_PY2 SONIC_PLATFORM_API_PY3 PDDF_PLATFORM_API_BASE_PY2 PDDF_PLATFORM_API_BASE_PY3 \
    SONIC_YANG_MODELS_PY3 SONIC_CTRMGRD SONIC_YANG_MGMT_PY3 SYSTEM_HEALTH SONIC_HOST_SERVICES_PY3 \
    SONIC_DEBIAN_EXTENSION_DEPENDS SONIC_BAZEL_VS_CONTRACT_DEPENDS SONIC_PACKAGES_LOCAL SONIC_INSTALLERS SONIC_RFS_TARGETS \
    SONIC_ONLINE_DEBS SONIC_COPY_DEBS SONIC_MAKE_DEBS SONIC_DPKG_DEBS SONIC_PYTHON_STDEB_DEBS \
    SONIC_DERIVED_DEBS SONIC_EXTRA_DEBS SONIC_BAZEL_KERNEL_DEBS SONIC_PYTHON_WHEELS SONIC_COPY_FILES \
    SONIC_ONLINE_FILES SONIC_MAKE_FILES
override _VS_REUSE_METADATA_DEBS = $(sort $(SONIC_ONLINE_DEBS) $(SONIC_COPY_DEBS) $(SONIC_MAKE_DEBS) $(SONIC_DPKG_DEBS) $(SONIC_PYTHON_STDEB_DEBS) $(SONIC_DERIVED_DEBS) $(SONIC_EXTRA_DEBS) $(SONIC_BAZEL_KERNEL_DEBS))
override _VS_REUSE_METADATA_WHEELS = $(sort $(SONIC_PYTHON_WHEELS))
override _VS_REUSE_METADATA_FILES = $(sort $(SONIC_COPY_FILES) $(SONIC_ONLINE_FILES) $(SONIC_MAKE_FILES))

# Keep every kernel glob match; build_debian debug exclusions remain a consumer check.
define _vs_reuse_capture_inputs
$(if $(wildcard $(_VS_REUSE_METADATA_PATH) target/sonic-vs.bin.selection.json target/sonic-vs.bin.dockers),$(error VS reuse metadata requires fresh selection outputs))
$(file >$(_VS_REUSE_METADATA_PATH),sonic-vs-make-inputs-v1)
$(foreach name,$(_VS_REUSE_METADATA_VARIABLES),$(file >>$(_VS_REUSE_METADATA_PATH),variable	$(name)	$($(name))))
$(file >>$(_VS_REUSE_METADATA_PATH),extra-exports-present	$(if $(strip $(SONIC_INSTALLER_EXTRA_EXPORTS)),y,n))
$(foreach name,DEPENDS INSTALLS LAZY_INSTALLS LAZY_BUILD_INSTALLS PYTHON_WHEELS FILES RFS_DEPENDS MACHINE IMAGE_TYPE DOCKERS,$(file >>$(_VS_REUSE_METADATA_PATH),installer	$(name)	$(sonic-vs.bin_$(name))))
$(foreach deb,$(_VS_REUSE_METADATA_DEBS),$(file >>$(_VS_REUSE_METADATA_PATH),deb	$(deb)	$($(deb)_DEPENDS)	$($(deb)_WHEEL_DEPENDS)	$($(deb)_RDEPENDS)	$($(deb)_PHONIES)	$($(deb)_CACHE_OVERRIDE)	$($(deb)_CACHE_MODE)	$($(deb)_PLATFORM)	$($(deb)_MACHINE)))
$(foreach wheel,$(_VS_REUSE_METADATA_WHEELS),$(file >>$(_VS_REUSE_METADATA_PATH),wheel	$(wheel)	$($(wheel)_DEPENDS)	$($(wheel)_DEBS_DEPENDS)	$($(wheel)_FILES)	$($(wheel)_CACHE_OVERRIDE)	$($(wheel)_CACHE_MODE)))
$(foreach rfs,$(sort $(SONIC_RFS_TARGETS)),$(file >>$(_VS_REUSE_METADATA_PATH),rfs	$(rfs)	$($(rfs)_DEPENDENT_RFS)	$($(rfs)_INSTALLER)	$($(rfs)_MACHINE)))
$(foreach input,$(_VS_REUSE_METADATA_FILES),$(file >>$(_VS_REUSE_METADATA_PATH),file	$(input)	$($(input)_PATH)	$($(input)_DEPENDS)	$($(input)_PHONIES)))
$(foreach pattern,audisp-tacplus_*.deb bash-tacplus_*.deb bash_*.deb ifupdown2_*.deb kdump-tools_*.deb libnss-radius_*.deb libnss-tacplus_*.deb libpam-radius-auth_*.deb libpam-tacplus_*.deb libtac2_*.deb libyang3_*.deb makedumpfile_*.deb monit_*.deb python3-libyang_*.deb rasdaemon_*.deb sedutil_*.deb sonic-ctrmgrd-rs_*.deb sonic-db-cli_*.deb sonic-device-data_*.deb sonic-host-services-data_*.deb sonic-host-services-rs_*.deb sonic-nettools_*.deb sonic-rsyslog-plugin_*.deb sonic-utilities-data_*.deb sonic-ztp_*.deb syslog-counter_*.deb openssh-server_$(OPENSSH_VERSION_FULL)_*.deb openssh-client_$(OPENSSH_VERSION_FULL)_*.deb openssh-sftp-server_$(OPENSSH_VERSION_FULL)_*.deb initramfs-tools-core_*.deb initramfs-tools_*.deb linux-image-$(LINUX_KERNEL_VERSION)-*_$(CONFIGURED_ARCH).deb,$(file >>$(_VS_REUSE_METADATA_PATH),body-glob	$(pattern)	$(sort $(wildcard $(IMAGE_DISTRO_DEBS_PATH)/$(pattern)))))
$(foreach input,$(RULES_PATH)/config.organization files/build_templates/build_debian.organization.sh files/build_templates/organization_extensions.sh files/build_templates/sonic_debian_extension_organization_packages.j2 files/build_templates/sonic_debian_extension_organization_docker_images.j2,$(file >>$(_VS_REUSE_METADATA_PATH),optional-input	$(input)	$(if $(wildcard $(input)),present,absent)))
$(file >>$(_VS_REUSE_METADATA_PATH),end	sonic-vs-make-inputs-v1)
endef

# Reuse the existing Make marker writer only after artifact admission has
# authenticated the actual Bazel build step and exact imported archive hash.
# These metadata targets cannot invoke an archive or package build recipe.
ifneq ($(filter vs-reuse-selection,$(MAKECMDGOALS)),)
ifeq ($(strip $(VS_REUSE_BAZEL_ARCHIVES)),)
$(error VS reuse requires admitted modern archive build evidence)
endif
ifneq ($(filter-out $(addprefix $(TARGET_PATH)/,$(SONIC_BAZEL_DOCKER_IMAGES) $(SONIC_BAZEL_DBG_DOCKER_IMAGES)),$(VS_REUSE_BAZEL_ARCHIVES)),)
$(error VS reuse marker names an archive not selected for Bazel)
endif
ifneq ($(filter-out $(addprefix $(TARGET_PATH)/,$(SONIC_BAZEL_SWITCHABLE_IMAGES)),$(VS_REUSE_BAZEL_ARCHIVES)),)
$(error VS reuse marker has no existing Make owner)
endif
vs-reuse-selection: $(addprefix $(TARGET_PATH)/.container-build-method/,$(notdir $(VS_REUSE_BAZEL_ARCHIVES)))
endif

.PHONY: vs-reuse-selection
vs-reuse-selection: private export SONIC_IMAGE_VERSION := $(SONIC_IMAGE_VERSION)
vs-reuse-selection: private export VS_ARCHITECTURE := $(CONFIGURED_ARCH)
vs-reuse-selection: private export VS_PLATFORM := $(CONFIGURED_PLATFORM)
vs-reuse-selection: private export VS_DISTRIBUTION := $(BLDENV)
vs-reuse-selection: private export VS_BUILDER := $(BUILD_WITH_BAZEL_WHEN_AVAILABLE)
vs-reuse-selection: private export VS_FIPS := $(INCLUDE_FIPS)
vs-reuse-selection: private export VS_DASH := $(INCLUDE_VS_DASH_SAI)
vs-reuse-selection: private export VS_ASAN := $(ENABLE_ASAN)
vs-reuse-selection: private export VS_SYNCD_RPC := $(ENABLE_SYNCD_RPC)
vs-reuse-selection: private export VS_CROSS_BUILD := $(CROSS_BUILD_ENVIRON)
vs-reuse-selection: private export VS_MULTIARCH_QEMU := $(MULTIARCH_QEMU_ENVIRON)
vs-reuse-selection: $(addprefix $(TARGET_PATH)/bazel-manifests/,$(addsuffix /manifest.json,$(sort $(SONIC_BAZEL_MANIFESTS)))) $(SONIC_BAZEL_IMAGE_ENVIRONMENT_FILES)
	$(call _vs_reuse_capture_inputs)
	set -euo pipefail
	@test ! -e "$(TARGET_PATH)/sonic-vs.bin.selection.json"
	@test ! -e "$(TARGET_PATH)/sonic-vs.bin.dockers"
	export installer_images="$(foreach docker, $(sonic-vs.bin_DOCKERS),\
		$(addprefix $($(docker:-dbg.gz=.gz)_PACKAGE_NAME)|,\
		$(addprefix $($(docker:-dbg.gz=.gz)_PATH)|,\
		$(addprefix $($(docker:-dbg.gz=.gz)_MACHINE)|,\
		$(addprefix $(TARGET_PATH)/,$(addsuffix :$($(docker:-dbg.gz=.gz)_VERSION),$(docker)))))))"
	export sonic_local_packages="$(foreach package, $(SONIC_PACKAGES_LOCAL),\
		$(addsuffix |$($(package)_DEFAULT_FEATURE_STATE_ENABLED),\
		$(addsuffix |$($(package)_DEFAULT_FEATURE_OWNER),\
		$(addsuffix |$(addprefix $(TARGET_PATH)/, $(package)), $(package)))))"
	export include_system_eventd="$(INCLUDE_SYSTEM_EVENTD)"
	export install_debug_image="$(INSTALL_DEBUG_TOOLS)"
	printf '%s\n' $${installer_images} | \
		awk -F'|' -v machine="$(sonic-vs.bin_MACHINE)" \
		'$$3 == "" || $$3 == machine { image = $$4; sub(/:[^:]*$$/, "", image); print image }' \
		> "$(TARGET_PATH)/sonic-vs.bin.dockers"
	python3 -B tools/bazel/ci/vs_final23.py --workspace "$(CURDIR)" --installer sonic-vs.bin --machine "$(sonic-vs.bin_MACHINE)"
