ifneq ($(strip $(SONIC_BAZEL_P4_DEBS)),)
# Check the reviewed pins on every invocation, once even with parallel goals.
# stage.py only fetches and copies existing DEBs; it never builds a package.
.PHONY: bazel-p4-import
bazel-p4-import: .platform
	python3 tools/bazel/p4/stage.py --output-directory "$(DEBS_PATH)" \
		--bazel "$${BAZEL:-bazel}" \
		--dash-sai-commit "$(DASH_SAI_COMMIT)" \
		$(foreach deb,$(SONIC_BAZEL_P4_DEBS),--expected-package "$(deb)") \
		--cache-directory="$${BAZEL_CONTAINER_CACHE_DIR-$${BAZEL_SWSS_CACHE_DIR:-}}"

$(addprefix $(DEBS_PATH)/,$(SONIC_BAZEL_P4_DEBS)): $(DEBS_PATH)/%: bazel-p4-import
	test -s "$@"
	$(call sbom_emit_fragment,$@,ONLINE_DEB,$($*_SRC_PATH),,$($*_DEPENDS),$($*_RDEPENDS),$($*_MAIN_DEB))

SONIC_TARGET_LIST += $(addprefix $(DEBS_PATH)/,$(SONIC_BAZEL_P4_DEBS))
else
# Returning to native Make must not accept the previously imported release DEB,
# especially for debug/profiling builds when the native package cache is off.
# Preserve the old output and ask for the existing per-package clean operation.
.PHONY: $(addsuffix .bazel-native-check,$(addprefix $(DEBS_PATH)/,$(SONIC_BAZEL_P4_CANDIDATES)))
$(addsuffix .bazel-native-check,$(addprefix $(DEBS_PATH)/,$(SONIC_BAZEL_P4_CANDIDATES))): $(DEBS_PATH)/%.bazel-native-check:
	@if [ -e "$(DEBS_PATH)/$*.bazel-imported" ]; then \
		echo "$(DEBS_PATH)/$* was imported by Bazel; clean it before using native Make:" >&2; \
		echo "  make $(DEBS_PATH)/$*-clean" >&2; \
		exit 1; \
	fi

$(addprefix $(DEBS_PATH)/,$(SONIC_BAZEL_P4_CANDIDATES)): $(DEBS_PATH)/%: | $(DEBS_PATH)/%.bazel-native-check
endif

# Keep normal package clean commands usable in either mode. A main package's
# clean also removes its derived outputs, so clear those import markers as well.
$(addsuffix -clean,$(addprefix $(DEBS_PATH)/,$(SONIC_BAZEL_P4_CANDIDATES))):: $(DEBS_PATH)/%-clean:
	rm -f $(addsuffix .bazel-imported,$(addprefix $(DEBS_PATH)/,$* $($*_DERIVED_DEBS) $($*_EXTRA_DEBS)))
