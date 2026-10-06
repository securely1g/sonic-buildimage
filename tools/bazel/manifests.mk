# Use Make's existing metadata evaluation, service detection and JSON merge.
# Each owner registers manifest keys and the archive whose metadata they use:
#   SONIC_BAZEL_MANIFESTS += example example-dbg
#   example_MANIFEST_IMAGE = docker-example.gz
#   example-dbg_MANIFEST_IMAGE = docker-example.gz
#   example-dbg_MANIFEST_SUFFIX = dbg
# Add the resulting files to the container's _BAZEL_DEPENDS. A debug image that
# extends a runtime image needs both manifests, even when requested on its own.
.PHONY: bazel-manifests-force
bazel-manifests-force:

# Check on each request: metadata may come from conditionals, command-line
# settings or optional templates. Compare bytes before replacing a published
# input, so unchanged manifests do not invalidate downstream Make targets.
$(addprefix $(TARGET_PATH)/bazel-manifests/,$(addsuffix /manifest.json,$(sort $(SONIC_BAZEL_MANIFESTS)))) : $(TARGET_PATH)/bazel-manifests/%/manifest.json : bazel-manifests-force
	$(if $(value generate_manifest),,$(error include rules/functions before tools/bazel/manifests.mk))
	$(if $(strip $($*_MANIFEST_IMAGE)),,$(error $*_MANIFEST_IMAGE is required))
	$(if $(strip $($($*_MANIFEST_IMAGE)_PATH)),,$(error $($*_MANIFEST_IMAGE)_PATH is required))
	@mkdir -p "$(@D)"
	@manifest_stage=$$(mktemp -d "$(@D)/.manifest.XXXXXX")
	trap 'rm -rf "$$manifest_stage"' EXIT
	$(call generate_manifest,$(patsubst %.gz,%,$($*_MANIFEST_IMAGE)),$($*_MANIFEST_SUFFIX),$$manifest_stage)
	jq -e 'type == "object"' "$$manifest_stage/manifest.json" > /dev/null
	if ! cmp -s "$$manifest_stage/manifest.json" "$@"; then
		mv -f "$$manifest_stage/manifest.json" "$@"
	fi
