#!/usr/bin/env python3
"""Review the explicit public DASH engine evidence without private command logs."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci.dash_engine_image import (TARGETS, IMAGES, FOREIGN_PAYLOADS, DIAGNOSTIC_LIMIT,
    validate_foreign_diagnostics, compare_foreign_diagnostics, validate_foreign_input_differences,
    validate_foreign_explain)
from tools.bazel.ci.public_artifacts import safe_json, dependency_json, digest


def review(directory):
    names = {name for name in TARGETS if name.endswith('.json')}
    names.update(('validation.json', 'action-audit.json', 'owner-action-audit.json', 'incremental.json',
                  'layout-identities.json'))
    names.update(name + '.validation.json' for name in IMAGES)
    for name in sorted(names):
        path = directory / name
        if path.exists():
            data = json.loads(path.read_text())
            if name == 'incremental.json' and 'foreign_diagnostics' in data:
                diagnostics = data['foreign_diagnostics']
                if not isinstance(diagnostics, dict) or not {'initial'} <= set(diagnostics) <= {'initial', 'baseline'}:
                    raise ValueError('invalid foreign diagnostic phases')
                for value in diagnostics.values(): validate_foreign_diagnostics(value)
                error = data.get('foreign_diagnostics_error')
                if error not in (None, 'foreign-diagnostics-unavailable') or (error and data.get('status') != 'failed'):
                    raise ValueError('invalid foreign diagnostic error')
                expected = compare_foreign_diagnostics(diagnostics['initial'], diagnostics['baseline']) if 'baseline' in diagnostics else None
                if 'foreign_comparison' in data and (expected is None or data['foreign_comparison'] != expected):
                    raise ValueError('invalid foreign diagnostic comparison')
                if expected is not None and not error and not {
                        'foreign_comparison', 'foreign_input_differences', 'foreign_explain',
                        'foreign_payloads_before_baseline'} <= set(data):
                    raise ValueError('missing foreign diagnostic comparison')
                if 'foreign_input_differences' in data:
                    differences = data['foreign_input_differences']
                    validate_foreign_input_differences(differences)
                    identity = lambda row: (row['target'], row['listed_outputs_sha256'], row['status'])
                    if expected is None or [identity(row) for row in differences] != [identity(row) for row in expected['instances']]:
                        raise ValueError('foreign input comparison instances differ')
                if 'foreign_explain' in data: validate_foreign_explain(data['foreign_explain'])
                if 'foreign_payloads_before_baseline' in data:
                    payloads = data['foreign_payloads_before_baseline']
                    if not isinstance(payloads, dict) or set(payloads) != set(FOREIGN_PAYLOADS):
                        raise ValueError('invalid pre-baseline payload identities')
                    for value in payloads.values():
                        if value is not None: digest(value)
                allowed = {'foreign_diagnostics', 'foreign_diagnostics_error', 'foreign_comparison',
                           'foreign_input_differences', 'foreign_explain', 'foreign_payloads_before_baseline'}
                projected = {key: value for key, value in data.items() if key.startswith('foreign_')}
                if set(projected) - allowed:
                    raise ValueError('invalid foreign diagnostic envelope')
                # Bound the whole emitted member, including indentation and its
                # newline, rather than a compact approximation of the subobject.
                if len((json.dumps(data, indent=2) + '\n').encode()) > DIAGNOSTIC_LIMIT:
                    raise ValueError('foreign diagnostic member exceeds bound')
            safe_json(data)
    for name, kind in (('MODULE.bazel.lock', 'lock'), ('module-graph.json', 'graph')):
        path = directory / name
        if path.exists():
            dependency_json(json.loads(path.read_text()), kind)
    receipt = directory / 'build-receipt.json'
    if receipt.exists():
        data = json.loads(receipt.read_text())
        if 'foreign_diagnostics' in data: validate_foreign_diagnostics(data['foreign_diagnostics'])
        public = {key: data[key] for key in ('status', 'revision', 'architecture', 'image_version', 'artifacts', 'tested_revision_parents', 'foreign_diagnostics') if key in data}
        safe_json(public)
        content = json.dumps(public, indent=2) + '\n'
        if 'foreign_diagnostics' in public and len(content.encode()) > DIAGNOSTIC_LIMIT:
            raise ValueError('foreign diagnostic receipt exceeds bound')
        (directory / 'public-receipt.json').write_text(content)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, required=True)
    review(parser.parse_args().artifacts)
