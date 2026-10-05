#!/usr/bin/env python3
"""Check shared container rendering independently of a specific image recipe."""

import json
import unittest

import jinja2

import container_config


def recipe(variable, name, version="1.0.0"):
    return (
        f"$({variable})_VERSION = {version}\n"
        f"$({variable})_CONTAINER_NAME = {name}\n"
        f"$({variable})_PACKAGE_NAME = {name}\n"
    )


class ContainerConfigTest(unittest.TestCase):
    def test_container_selection_isolates_metadata_and_unrelated_conditions(self):
        source = recipe("DOCKER_WORKER", "worker") + recipe("DOCKER_DATABASE", "database", "2.0.0")
        source += (
            "ifdef OPTIONAL_FEATURE\n"
            "$(DOCKER_WORKER)_RUN_OPT += $(EXTRA_OPTIONS)\n"
            "$(DOCKER_WORKER_EXTRA)_VERSION = $(UNRELATED_VERSION)\n"
            "endif\n"
        )
        worker = container_config.manifest_metadata(source, "DOCKER_WORKER")
        database = container_config.manifest_metadata(source, "DOCKER_DATABASE")
        self.assertEqual((worker["name"], worker["version"]), ("worker", "1.0.0"))
        self.assertEqual((database["name"], database["version"]), ("database", "2.0.0"))
        self.assertEqual(worker["privileged"], "")
        self.assertEqual(worker["volumes"], "")

    def test_assignments_replace_append_and_ignore_comments(self):
        source = recipe("DOCKER_WORKER", "worker") + (
            "$(DOCKER_WORKER)_SERVICE_AFTER += database\n"
            "$(DOCKER_WORKER)_SERVICE_AFTER += telemetry # build comment\n"
            "$(DOCKER_WORKER)_SERVICE_BEFORE = old\n"
            "$(DOCKER_WORKER)_SERVICE_BEFORE := new\n"
            "$(DOCKER_WORKER)_VERSION := 2.0.0\n"
        )
        context = container_config.manifest_metadata(source, "DOCKER_WORKER")
        self.assertEqual(context["after"], "database telemetry")
        self.assertEqual(context["before"], "new")
        self.assertEqual(context["version"], "2.0.0")

    def test_make_variable_requires_an_identifier(self):
        for variable in ("", "DOCKER.WORKER", "DOCKER.*", "$(DOCKER_WORKER)", "1WORKER", None):
            with self.subTest(variable=variable), self.assertRaisesRegex(ValueError, "identifier"):
                container_config.manifest_metadata("", variable)

    def test_missing_required_metadata_fails(self):
        for field in ("VERSION", "CONTAINER_NAME", "PACKAGE_NAME"):
            source = recipe("DOCKER_WORKER", "worker") + f"$(DOCKER_WORKER)_{field} =\n"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Missing required"):
                container_config.manifest_metadata(source, "DOCKER_WORKER")

    def test_selected_computed_conditional_multiline_and_default_fields_fail(self):
        extras = [
            "$(DOCKER_WORKER)_VERSION = $(PROJECT_VERSION)",
            "$(DOCKER_WORKER)_SERVICE_AFTER += database \\\ntelemetry",
            "$(DOCKER_WORKER)_VERSION ?= 2.0.0",
        ]
        for condition in ("ifeq ($(PLATFORM),vs)", "ifneq ($(PLATFORM),vs)",
                          "ifdef FEATURE", "ifndef FEATURE"):
            extras.append(condition + "\n$(DOCKER_WORKER)_VERSION = 2.0.0\nendif")
        for extra in extras:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                container_config.manifest_metadata(recipe("DOCKER_WORKER", "worker") + extra,
                                                   "DOCKER_WORKER")

    def test_manifest_json_filter_preserves_values_and_context(self):
        context = {"name": 'worker "special"', "depends": ["database", "telemetry"]}
        result = container_config.render_manifest(
            '{"name": {{ name|json }}, "depends": {{ depends|json }}}', context
        )
        self.assertEqual(result, context)
        self.assertNotIn("version_suffix", context)

    def test_manifest_requires_complete_context_and_valid_json_object(self):
        with self.assertRaises(jinja2.UndefinedError):
            container_config.render_manifest('{"name": "{{ missing }}"}', {})
        with self.assertRaises(json.JSONDecodeError):
            container_config.render_manifest('{"name": }', {})
        for template in ("[]", "null", '"text"'):
            with self.subTest(template=template), self.assertRaisesRegex(ValueError, "JSON object"):
                container_config.render_manifest(template, {})

    def test_version_suffix_preserves_existing_build_metadata(self):
        template = (
            '{% if version_suffix %}'
            '{% set version = version + ("." if "+" in version else "+") + version_suffix %}'
            '{% endif %}'
            '{"version": "{{ version }}"}'
        )
        self.assertEqual(container_config.render_manifest(template, {"version": "1.0.0"}),
                         {"version": "1.0.0"})
        for version, expected in (("1.0.0", "1.0.0+dbg"), ("1.0.0+custom", "1.0.0+custom.dbg")):
            with self.subTest(version=version):
                self.assertEqual(container_config.render_manifest(template, {"version": version}, "dbg"),
                                 {"version": expected})

    def test_template_context_controls_feature_and_preserves_shell_variables(self):
        template = (
            "#!/bin/sh\n"
            "{% if FEATURE_ENABLED == 'y' %}\n"
            "echo enabled\n"
            "{% endif %}\n"
            'echo "${RUNTIME_VALUE:-unknown}"\n'
        )
        self.assertEqual(container_config.render_template(template, {"FEATURE_ENABLED": "n"}),
                         '#!/bin/sh\necho "${RUNTIME_VALUE:-unknown}"\n')
        self.assertEqual(container_config.render_template(template, {"FEATURE_ENABLED": "y"}),
                         '#!/bin/sh\necho enabled\necho "${RUNTIME_VALUE:-unknown}"\n')
        with self.assertRaises(jinja2.UndefinedError):
            container_config.render_template(template, {})

    def test_manifest_label_is_one_line_of_compact_json(self):
        manifest = {"package": {"name": 'quoted "name"', "description": "line1\nline2"}}
        label = container_config.manifest_label(manifest)
        self.assertEqual(len(label.splitlines()), 1)
        key, value = label.rstrip("\n").split("=", 1)
        self.assertEqual(key, "com.azure.sonic.manifest")
        self.assertEqual(json.loads(value), manifest)
        self.assertEqual(value, json.dumps(manifest, separators=(",", ":")))
        self.assertTrue(label.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
