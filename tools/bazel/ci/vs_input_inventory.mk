# Read after the real slave.mk; this goal observes definitions only.
ifneq ($(SONIC_VS_INPUT_INVENTORY),y)
$(error vs-input-inventory requires SONIC_VS_INPUT_INVENTORY=y)
endif
ifneq ($(strip $(MAKECMDGOALS)),vs-input-inventory)
$(error Inventory permits one metadata goal only)
endif

# Explicit empty double-colon rules prevent Make from remaking included files.
# Any pre-existing ordinary rule for an include is a hard Make error, not a build.
# Exact configured VS include declarations, including absent optional files.
# Existing files also enter MAKEFILE_LIST; these names close the missing-file case.
VS_INPUT_KNOWN_MAKEFILES := slave.mk Makefile.cache rules/config rules/functions rules/config.organization rules/config.user $(RULES_PATH)/*.mk $(RULES_PATH)/*.dep $(PLATFORM_PATH)/rules.mk $(PLATFORM_PATH)/rules.dep $(if $(PLATFORM_PDDF_PATH),$(PLATFORM_PDDF_PATH)/rules.mk $(PLATFORM_PDDF_PATH)/rules.dep) $(PLATFORM_PATH)/*.mk $(PLATFORM_PATH)/*.dep platform/template/docker-syncd-trixie.mk platform/template/docker-gbsyncd-trixie.mk tools/bazel/docker.mk tools/bazel/manifests.mk
$(sort $(MAKEFILE_LIST) $(VS_INPUT_KNOWN_MAKEFILES)):: ;

VS_INPUT_ROWS := $(TARGET_PATH)/vs-input-inventory.rows
VS_INPUT_JSON := $(TARGET_PATH)/vs-input-inventory.json
VS_INPUT_CATEGORIES := SONIC_COPY_DEBS SONIC_ONLINE_DEBS SONIC_MAKE_DEBS SONIC_DPKG_DEBS SONIC_DERIVED_DEBS SONIC_EXTRA_DEBS SONIC_BAZEL_KERNEL_DEBS SONIC_PYTHON_STDEB_DEBS SONIC_PYTHON_WHEELS SONIC_COPY_FILES SONIC_ONLINE_FILES SONIC_MAKE_FILES SONIC_RFS_TARGETS
VS_INPUT_NODES = $(sort $(foreach category,$(VS_INPUT_CATEGORIES),$($(category))))
VS_INPUT_NODE_FIELDS := DEPENDS RDEPENDS DEBS_DEPENDS WHEEL_DEPENDS PYTHON_DEBS PYTHON_WHEELS FILES PHONIES AFTER MAIN_DEB DERIVED_DEBS EXTRA_DEBS DEPENDENT_RFS INSTALLER MACHINE PATH URL CACHE_MODE
# This is the unchanged sonic-vs.bin prerequisite expression, with only its
# secondary-expansion stem bound to sonic-vs.bin. No prerequisite is scheduled.
VS_INPUT_DIRECT = \
        .platform \
        onie-image.conf \
        build_debian.sh \
        scripts/validate_sonic_version.py \
        files/build_templates/sonic_version.yml.j2 \
        files/build_templates/sonic_debian_extension.j2 \
        files/build_templates/docker_image_ctl.j2 \
        $(SONIC_DEBIAN_EXTENSION_DEPENDS) \
        scripts/dbg_files.sh \
        scripts/build_sbom.sh \
        scripts/build_sbom.py \
        scripts/sbom_kernel_deb.py \
        scripts/install_sbom_tool.sh \
        scripts/sbom_fragment.py \
        scripts/sbom_cve_refs.py \
        scripts/sbom_purl.py \
        scripts/sbom_parse_lockfiles.py \
        scripts/sbom_extract_vex_from_patches.py \
        build_image.sh \
        $(if $(filter sonic-vs.img.gz,sonic-vs.bin),scripts/image_timing.py scripts/build_kvm_image.sh) \
        $(addsuffix -install,$(addprefix $(IMAGE_DISTRO_DEBS_PATH)/,$(sonic-vs.bin_DEPENDS))) \
        $(addprefix $(IMAGE_DISTRO_DEBS_PATH)/,$(sonic-vs.bin_INSTALLS)) \
        $(addprefix $(IMAGE_DISTRO_DEBS_PATH)/,$(sonic-vs.bin_LAZY_INSTALLS)) \
        $(addprefix $(IMAGE_DISTRO_DEBS_PATH)/,$(sonic-vs.bin_LAZY_BUILD_INSTALLS)) \
		$(addprefix $(PYTHON_WHEELS_PATH)/,$(sonic-vs.bin_PYTHON_WHEELS)) \
        $(addprefix $(IMAGE_DISTRO_DEBS_PATH)/,$(INITRAMFS_TOOLS) \
                $(LINUX_KERNEL) \
                $(LINUX_KBUILD) \
                $(SONIC_DEVICE_DATA) \
                $(IFUPDOWN2) \
                $(MAKEDUMPFILE) \
                $(KDUMP_TOOLS) \
                $(LIBPAM_RADIUS) \
                $(LIBNSS_RADIUS) \
                $(LIBPAM_TACPLUS) \
                $(LIBNSS_TACPLUS) \
                $(MONIT) \
                $(OPENSSH_SERVER) \
                $(PYTHON3_SWSSCOMMON) \
                $(SONIC_DB_CLI) \
                $(SONIC_NETTOOLS) \
                $(SONIC_RSYSLOG_PLUGIN) \
                $(SONIC_UTILITIES_DATA) \
                $(SONIC_CTRMGRD_RS) \
                $(SONIC_HOST_SERVICES_RS) \
                $(SONIC_HOST_SERVICES_DATA) \
                $(SOCAT) \
                $(BASH) \
                $(BASH_TACPLUS) \
                $(AUDISP_TACPLUS) \
                $(SYSLOG_COUNTER) \
                $(SEDUTIL)) \
        $(addprefix $(TARGET_PATH)/,$(sonic-vs.bin_DOCKERS)) \
        $(if $(filter sonic-vs.bin sonic-vs.img.gz,sonic-vs.bin),$(SONIC_BAZEL_VS_CONTRACT_DEPENDS)) \
        $(addprefix $(TARGET_PATH)/,$(SONIC_PACKAGES_LOCAL)) \
        $(addprefix $(FILES_PATH)/,$(sonic-vs.bin_FILES)) \
        $(if $(findstring y,$(ENABLE_ZTP)),$(addprefix $(IMAGE_DISTRO_DEBS_PATH)/,$(SONIC_ZTP))) \
        $(addprefix $(IMAGE_DISTRO_DEBS_PATH)/,$(SONIC_INSTALLER_EXTRA_DEBS)) \
        $(if $(findstring y,$(INCLUDE_FIPS)),$(addprefix $(IMAGE_DISTRO_DEBS_PATH)/,$(SYMCRYPT_OPENSSL))) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(SONIC_UTILITIES_PY3)) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(SONIC_PY_COMMON_PY2)) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(SONIC_PY_COMMON_PY3)) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(SONIC_CONFIG_ENGINE_PY2)) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(SONIC_CONFIG_ENGINE_PY3)) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(SONIC_PLATFORM_COMMON_PY3)) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(REDIS_DUMP_LOAD_PY2)) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(SONIC_PLATFORM_API_PY2)) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(SONIC_PLATFORM_API_PY3)) \
        $(if $(findstring y,$(PDDF_SUPPORT)),$(addprefix $(PYTHON_WHEELS_PATH)/,$(PDDF_PLATFORM_API_BASE_PY2))) \
        $(if $(findstring y,$(PDDF_SUPPORT)),$(addprefix $(PYTHON_WHEELS_PATH)/,$(PDDF_PLATFORM_API_BASE_PY3))) \
        $(if $(findstring amd64,$(CONFIGURED_ARCH)),$(addprefix $(IMAGE_DISTRO_DEBS_PATH)/,$(RASDAEMON))) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(SONIC_YANG_MODELS_PY3)) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(SONIC_CTRMGRD)) \
        $(addprefix $(FILES_PATH)/,$($(SONIC_CTRMGRD)_FILES)) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(SONIC_YANG_MGMT_PY3)) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(SYSTEM_HEALTH)) \
        $(addprefix $(PYTHON_WHEELS_PATH)/,$(SONIC_HOST_SERVICES_PY3)) \
        $(addprefix $(TARGET_PATH)/,$(sonic-vs.bin_RFS_DEPENDS)) \
        $(addprefix $(IMAGE_DISTRO_DEBS_PATH)/,$(LINUX_KBUILD)-install)

# Values are data, never shell arguments. The collector rejects controls/duplicates.
define vs_input_row
$(file >>$(VS_INPUT_ROWS),$(1)	$(2)	$(strip $(3)))
endef

# Kernel settings are scalar values; preserve their exact whitespace.
define vs_input_scalar_row
$(file >>$(VS_INPUT_ROWS),$(1)	$(2)	$(3))
endef

.PHONY: vs-input-inventory
vs-input-inventory:
	$(if $(wildcard $(VS_INPUT_ROWS) $(VS_INPUT_JSON)),$(error Inventory outputs already exist))
	$(file >$(VS_INPUT_ROWS),format	schema	1)
	$(call vs_input_row,profile,architecture,$(CONFIGURED_ARCH))
	$(call vs_input_row,profile,platform,$(CONFIGURED_PLATFORM))
	$(call vs_input_row,profile,distribution,$(BLDENV))
	$(call vs_input_row,profile,builder,$(BUILD_WITH_BAZEL_WHEN_AVAILABLE))
	$(call vs_input_row,profile,fips,$(INCLUDE_FIPS))
	$(call vs_input_row,profile,dash,$(INCLUDE_VS_DASH_SAI))
	$(call vs_input_row,profile,asan,$(ENABLE_ASAN))
	$(call vs_input_row,profile,syncd_rpc,$(ENABLE_SYNCD_RPC))
	$(call vs_input_row,profile,cross,$(CROSS_BUILD_ENVIRON))
	$(call vs_input_row,profile,qemu,$(MULTIARCH_QEMU_ENVIRON))
	$(call vs_input_row,profile,install_debug_tools,$(INSTALL_DEBUG_TOOLS))
	$(call vs_input_row,profile,image_version,$(SONIC_IMAGE_VERSION))
	$(call vs_input_row,profile,machine,$(sonic-vs.bin_MACHINE))
	$(call vs_input_row,root,make_prerequisites,$(VS_INPUT_DIRECT))
	$(call vs_input_row,root,installer_debs,$(addprefix $(IMAGE_DISTRO_DEBS_PATH)/,$(sonic-vs.bin_INSTALLS) $(FIPS_BASEIMAGE_INSTALLERS) $(SOCAT)))
	$(call vs_input_row,root,installer_python_debs,$(addprefix $(IMAGE_DISTRO_DEBS_PATH)/,$(FIPS_BASEIMAGE_PYTHON_INSTALLERS)))
	$(call vs_input_row,root,installer_wheels,$(addprefix $(PYTHON_WHEELS_PATH)/,$(sonic-vs.bin_PYTHON_WHEELS)))
	$(call vs_input_row,root,rfs,$(addprefix $(TARGET_PATH)/,$(sonic-vs.bin_RFS_DEPENDS)))
	$(call vs_input_row,root,kernel_inputs,$(SONIC_BAZEL_KERNEL_INPUTS))
	$(call vs_input_row,root,kernel_debs,$(addprefix $(DEBS_PATH)/,$(SONIC_BAZEL_KERNEL_DEBS)))
	$(foreach setting,$(SONIC_BAZEL_KERNEL_CONFIG),$(call vs_input_scalar_row,kernel_config,$(setting),$(if $(filter SECURE_UPGRADE_MODE,$(setting)),$(subst ",,$($(setting))),$($(setting)))))
	$(call vs_input_row,include,files,$(MAKEFILE_LIST))
	$(foreach category,$(VS_INPUT_CATEGORIES),$(call vs_input_row,category,$(category),$($(category))))
	$(foreach node,$(VS_INPUT_NODES),$(foreach field,$(VS_INPUT_NODE_FIELDS),$(call vs_input_row,node,$(node)|$(field),$($(node)_$(field)))))
	python3 -B tools/bazel/ci/vs_input_inventory.py --workspace "$(CURDIR)" --revision "$(VS_INPUT_SOURCE_REVISION)" --rows "$(VS_INPUT_ROWS)" --output "$(VS_INPUT_JSON)"
