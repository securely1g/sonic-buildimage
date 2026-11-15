#!/usr/bin/env python3
"""Collect two existing installed-policy targets in one reviewed source graph.

The existing native cache setup, Make owners, action audit, exporter, image
validators and policy merger remain authoritative. This caller neither changes
crypto sealing nor confers native/full-VS acceptance on a new archive.
"""
import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import shlex
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    'cohort_setup', Path(__file__).with_name('build_vs_swss_installed_policy.py'))
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)
require = setup.require

BASE = '//dockers/docker-config-engine-trixie:docker-config-engine-trixie'
FAMILIES = {
    'swss': ('//dockers/docker-orchagent:', ('docker-orchagent', 'docker-orchagent-dbg')),
    'syncd': ('//platform/vs/docker-syncd-vs:', ('docker-syncd-vs', 'docker-syncd-vs-dbg')),
}
POLICIES = {name + '-installed-images-policy.json':
            '//tools/bazel/tests:' + name + '_installed_images_policy' for name in FAMILIES}
OPTIONS = ['--jobs=2', '--local_resources=cpu=2', '--local_resources=memory=8000', '--config=syncd-dash']
PRIMARY = {'docker-base-trixie.gz': '//dockers/docker-base-trixie:docker-base-trixie.gz',
           'docker-config-engine-trixie.gz': BASE + '.gz', **POLICIES,
           **{name + '.gz': owner + name + '.gz'
              for owner, images in FAMILIES.values() for name in images}}


COMPONENTS = {'dash-engine': {'full_label_layouts': False,
                 'images': ['docker-dash-engine', 'docker-dash-engine-dbg'],
                 'manifest_command': ['make',
                                      '-f',
                                      'tools/bazel/prepare_manifests.mk',
                                      'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                                      'platform/vs/docker-dash-engine.mk',
                                      'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                                      'BLDENV=trixie',
                                      'CONFIGURED_PLATFORM=vs',
                                      'CONFIGURED_ARCH=amd64',
                                      'INCLUDE_FIPS=y',
                                      'INCLUDE_VS_DASH_SAI=y',
                                      'ENABLE_ASAN=n',
                                      'DOCKERS_PATH=dockers',
                                      'PLATFORM_PATH=platform/vs',
                                      'SONIC_IMAGE_VERSION=bazel-dash-engine-ci',
                                      'manifests'],
                 'module': 'dash_engine_image',
                 'owner_sha256': '14dc108777c81c6718e69a06e018aa81f7e614472016379e744eb36122598dad',
                 'parent': '//dockers/docker-base-trixie:docker-base-trixie',
                 'prefix': '//platform/vs/docker-dash-engine:',
                 'receipt_fields': {'image_version': 'bazel-dash-engine-ci'},
                 'selected_archives': ['docker-dash-engine.gz']},
 'dash-ha': {'full_label_layouts': False,
             'images': ['docker-dash-ha', 'docker-dash-ha-dbg'],
             'manifest_command': ['make',
                                  '-f',
                                  'tools/bazel/prepare_manifests.mk',
                                  'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                                  'rules/docker-config-engine-trixie.mk rules/docker-swss-layer-trixie.mk '
                                  'rules/docker-dash-ha.mk',
                                  'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                                  'BLDENV=trixie',
                                  'CONFIGURED_PLATFORM=vs',
                                  'CONFIGURED_ARCH=amd64',
                                  'INCLUDE_FIPS=y',
                                  'ENABLE_ASAN=n',
                                  'DBG_IMAGE_MARK=dbg',
                                  'DOCKERS_PATH=dockers'],
             'module': 'dash_ha_image',
             'owner_sha256': '670f54fdc8f8a1702304256474a4771b901809797bb66e408d7bfa86cae54ef4',
             'parent': '//dockers/docker-swss-layer-trixie:docker-swss-layer-trixie',
             'prefix': '//dockers/docker-dash-ha:',
             'receipt_fields': {},
             'selected_archives': ['docker-dash-ha.gz', 'docker-dash-ha-dbg.gz']},
 'database': {'full_label_layouts': True,
              'images': ['docker-database', 'docker-database-dbg'],
              'manifest_command': ['make',
                                   '-f',
                                   'tools/bazel/prepare_manifests.mk',
                                   'manifests',
                                   'target/bazel-manifests/docker-database/image.env',
                                   'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                                   'rules/docker-config-engine-trixie.mk rules/docker-database.mk',
                                   'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                                   'BLDENV=trixie',
                                   'CONFIGURED_PLATFORM=vs',
                                   'CONFIGURED_ARCH=amd64',
                                   'INCLUDE_FIPS=y',
                                   'ENABLE_ASAN=n',
                                   'DBG_IMAGE_MARK=dbg',
                                   'DOCKERS_PATH=dockers',
                                   'INCLUDE_SYSTEM_EVENTD=y'],
              'module': 'database_image',
              'owner_sha256': '66ca2d601123974da84bba20874903f94d68c1f2832599b89f1dfa809b7e8015',
              'parent': '//dockers/docker-config-engine-trixie:docker-config-engine-trixie',
              'prefix': '//dockers/docker-database:',
              'receipt_fields': {'include_system_eventd': 'y'},
              'selected_archives': ['docker-database.gz', 'docker-database-dbg.gz']},
 'eventd': {'full_label_layouts': True,
            'images': ['docker-eventd', 'docker-eventd-dbg'],
            'manifest_command': ['make',
                                 '-f',
                                 'tools/bazel/prepare_manifests.mk',
                                 'manifests',
                                 'target/bazel-manifests/docker-eventd/image.env',
                                 'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                                 'rules/docker-config-engine-trixie.mk rules/docker-eventd.mk',
                                 'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                                 'BLDENV=trixie',
                                 'CONFIGURED_PLATFORM=vs',
                                 'CONFIGURED_ARCH=amd64',
                                 'INCLUDE_FIPS=y',
                                 'ENABLE_ASAN=n',
                                 'DBG_IMAGE_MARK=dbg',
                                 'DOCKERS_PATH=dockers',
                                 'SONIC_IMAGE_VERSION=bazel-ci'],
            'module': 'eventd_image',
            'owner_sha256': '1e01ba7c3bce25cb72befe462ec1a9e7ee3891991868f567cf56392572d0a1c1',
            'parent': '//dockers/docker-config-engine-trixie:docker-config-engine-trixie',
            'prefix': '//dockers/docker-eventd:',
            'receipt_fields': {'image_version': 'bazel-ci'},
            'selected_archives': ['docker-eventd.gz', 'docker-eventd-dbg.gz']},
 'frr': {'full_label_layouts': False,
         'images': ['docker-fpm-frr', 'docker-fpm-frr-dbg'],
         'manifest_command': ['make',
                              '-f',
                              'tools/bazel/prepare_manifests.mk',
                              'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                              'rules/docker-config-engine-trixie.mk rules/docker-swss-layer-trixie.mk '
                              'rules/docker-fpm-frr.mk',
                              'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                              'BLDENV=trixie',
                              'CONFIGURED_PLATFORM=vs',
                              'CONFIGURED_ARCH=amd64',
                              'INCLUDE_FIPS=y',
                              'ENABLE_ASAN=n',
                              'DBG_IMAGE_MARK=dbg',
                              'ENABLE_FRR_TCMALLOC=y',
                              'ENABLE_FRR_SNMP_AGENT=y',
                              'FRR_USER_UID=300',
                              'FRR_USER_GID=300',
                              'DOCKERS_PATH=dockers'],
         'module': 'frr_image',
         'owner_sha256': '75dafab466e000fbc55f9b7cd307947c3926e6f042976d2ade8a6502755a02a2',
         'parent': '//dockers/docker-swss-layer-trixie:docker-swss-layer-trixie',
         'prefix': '//dockers/docker-fpm-frr:',
         'receipt_fields': {},
         'selected_archives': ['docker-fpm-frr.gz', 'docker-fpm-frr-dbg.gz']},
 'gnmi': {'full_label_layouts': True,
          'images': ['docker-sonic-gnmi', 'docker-sonic-gnmi-dbg'],
          'manifest_command': ['make',
                               '-f',
                               'tools/bazel/prepare_manifests.mk',
                               'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                               'rules/docker-config-engine-trixie.mk rules/docker-gnmi.mk',
                               'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                               'BLDENV=trixie',
                               'CONFIGURED_PLATFORM=vs',
                               'CONFIGURED_ARCH=amd64',
                               'INCLUDE_FIPS=y',
                               'ENABLE_ASAN=n',
                               'DBG_IMAGE_MARK=dbg',
                               'DOCKERS_PATH=dockers',
                               'ENABLE_TRANSLIB_WRITE=y',
                               'ENABLE_NATIVE_WRITE=y',
                               'ENABLE_DIALOUT=y',
                               'SONIC_IMAGE_VERSION=bazel-ci',
                               'manifests',
                               'target/bazel-manifests/docker-sonic-gnmi/image.env'],
          'module': 'gnmi_image',
          'owner_sha256': '355ad4b4bd1663a81ccb0d529b27810c6283daa13a5b7527a5a2309c57d36542',
          'parent': '//dockers/docker-config-engine-trixie:docker-config-engine-trixie',
          'prefix': '//dockers/docker-sonic-gnmi:',
          'receipt_fields': {'dialout_enabled': True, 'image_version': 'bazel-ci'},
          'selected_archives': ['docker-sonic-gnmi.gz', 'docker-sonic-gnmi-dbg.gz']},
 'lldp': {'full_label_layouts': True,
          'images': ['docker-lldp', 'docker-lldp-dbg'],
          'manifest_command': ['make',
                               '-f',
                               'tools/bazel/prepare_manifests.mk',
                               'manifests',
                               'target/bazel-manifests/docker-lldp/image.env',
                               'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                               'rules/docker-config-engine-trixie.mk rules/docker-lldp.mk',
                               'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                               'BLDENV=trixie',
                               'CONFIGURED_PLATFORM=vs',
                               'CONFIGURED_ARCH=amd64',
                               'INCLUDE_FIPS=y',
                               'ENABLE_ASAN=n',
                               'DBG_IMAGE_MARK=dbg',
                               'DOCKERS_PATH=dockers',
                               'SONIC_IMAGE_VERSION=bazel-ci'],
          'module': 'lldp_image',
          'owner_sha256': '25eb02f5e6bb24b9850fa6aedbd1fa67b6a97321da431160b4ee9830b06a7068',
          'parent': '//dockers/docker-config-engine-trixie:docker-config-engine-trixie',
          'prefix': '//dockers/docker-lldp:',
          'receipt_fields': {'image_version': 'bazel-ci'},
          'selected_archives': ['docker-lldp.gz', 'docker-lldp-dbg.gz']},
 'macsec': {'full_label_layouts': False,
            'images': ['docker-macsec', 'docker-macsec-dbg'],
            'manifest_command': ['make',
                                 '-f',
                                 'tools/bazel/prepare_manifests.mk',
                                 'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                                 'rules/docker-config-engine-trixie.mk rules/docker-swss-layer-trixie.mk '
                                 'rules/docker-macsec.mk',
                                 'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                                 'BLDENV=trixie',
                                 'CONFIGURED_PLATFORM=vs',
                                 'CONFIGURED_ARCH=amd64',
                                 'INCLUDE_FIPS=y',
                                 'ENABLE_ASAN=n',
                                 'DBG_IMAGE_MARK=dbg',
                                 'DOCKERS_PATH=dockers'],
            'module': 'macsec_image',
            'owner_sha256': 'bb674fddf3171ff601b16190a51d78742552f1fbfdb6aa8f5c97800a71d5e6c0',
            'parent': '//dockers/docker-swss-layer-trixie:docker-swss-layer-trixie',
            'prefix': '//dockers/docker-macsec:',
            'receipt_fields': {},
            'selected_archives': ['docker-macsec.gz', 'docker-macsec-dbg.gz']},
 'mgmt-framework': {'full_label_layouts': True,
                    'images': ['docker-sonic-mgmt-framework', 'docker-sonic-mgmt-framework-dbg'],
                    'manifest_command': ['make',
                                         '-f',
                                         'tools/bazel/prepare_manifests.mk',
                                         'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                                         'rules/docker-config-engine-trixie.mk '
                                         'rules/docker-sonic-mgmt-framework.mk',
                                         'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                                         'BLDENV=trixie',
                                         'CONFIGURED_PLATFORM=vs',
                                         'CONFIGURED_ARCH=amd64',
                                         'INCLUDE_FIPS=y',
                                         'ENABLE_ASAN=n',
                                         'DBG_IMAGE_MARK=dbg',
                                         'DOCKERS_PATH=dockers'],
                    'module': 'mgmt_framework_image',
                    'owner_sha256': '9bf9a691c795819d1abcfdf282f8555555872578d6fe3506df1d25687a56a593',
                    'parent': '//dockers/docker-config-engine-trixie:docker-config-engine-trixie',
                    'prefix': '//dockers/docker-sonic-mgmt-framework:',
                    'receipt_fields': {},
                    'selected_archives': ['docker-sonic-mgmt-framework.gz',
                                          'docker-sonic-mgmt-framework-dbg.gz']},
 'mux': {'full_label_layouts': True,
         'images': ['docker-mux', 'docker-mux-dbg'],
         'manifest_command': ['make',
                              '-f',
                              'tools/bazel/prepare_manifests.mk',
                              'manifests',
                              'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                              'rules/docker-config-engine-trixie.mk rules/docker-mux.mk',
                              'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                              'BLDENV=trixie',
                              'CONFIGURED_PLATFORM=vs',
                              'CONFIGURED_ARCH=amd64',
                              'INCLUDE_FIPS=y',
                              'INCLUDE_MUX=y',
                              'ENABLE_ASAN=n',
                              'DBG_IMAGE_MARK=dbg',
                              'DOCKERS_PATH=dockers',
                              'SONIC_IMAGE_VERSION=bazel-mux-ci'],
         'module': 'mux_image',
         'owner_sha256': '8f3168941e4a717def82cb3088966e82bc33febfc6aa0c42938d809f48e9a96a',
         'parent': '//dockers/docker-config-engine-trixie:docker-config-engine-trixie',
         'prefix': '//dockers/docker-mux:',
         'receipt_fields': {'image_version': 'bazel-mux-ci'},
         'selected_archives': ['docker-mux.gz', 'docker-mux-dbg.gz']},
 'otel': {'full_label_layouts': False,
          'images': ['docker-sonic-otel', 'docker-sonic-otel-dbg'],
          'manifest_command': ['make',
                               '-f',
                               'tools/bazel/prepare_manifests.mk',
                               'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                               'rules/docker-config-engine-trixie.mk rules/docker-otel.mk',
                               'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                               'BLDENV=trixie',
                               'CONFIGURED_PLATFORM=vs',
                               'CONFIGURED_ARCH=amd64',
                               'INCLUDE_FIPS=y',
                               'ENABLE_ASAN=n',
                               'DBG_IMAGE_MARK=dbg',
                               'DOCKERS_PATH=dockers',
                               'SONIC_IMAGE_VERSION=bazel-ci',
                               'manifests',
                               'target/bazel-manifests/docker-sonic-otel/image.env'],
          'module': 'otel_image',
          'owner_sha256': 'b18e5227b3c2c59f7da7c25b19ddbca0add7872e126bd2784052be502dff7c0d',
          'parent': '//dockers/docker-config-engine-trixie:docker-config-engine-trixie',
          'prefix': '//dockers/docker-sonic-otel:',
          'receipt_fields': {'image_version': 'bazel-ci'},
          'selected_archives': ['docker-sonic-otel.gz', 'docker-sonic-otel-dbg.gz']},
 'radv': {'full_label_layouts': True,
          'images': ['docker-router-advertiser', 'docker-router-advertiser-dbg'],
          'manifest_command': ['make',
                               '-f',
                               'tools/bazel/prepare_manifests.mk',
                               'manifests',
                               'target/bazel-manifests/docker-router-advertiser/image.env',
                               'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                               'rules/docker-config-engine-trixie.mk rules/docker-router-advertiser.mk',
                               'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                               'BLDENV=trixie',
                               'CONFIGURED_PLATFORM=vs',
                               'CONFIGURED_ARCH=amd64',
                               'INCLUDE_FIPS=y',
                               'ENABLE_ASAN=n',
                               'DBG_IMAGE_MARK=dbg',
                               'DOCKERS_PATH=dockers',
                               'SONIC_IMAGE_VERSION=bazel-ci'],
          'module': 'radv_image',
          'owner_sha256': '48a0a33ba841c06287e178233873743a2ec6370954444aa8b956f0bb4d23a936',
          'parent': '//dockers/docker-config-engine-trixie:docker-config-engine-trixie',
          'prefix': '//dockers/docker-router-advertiser:',
          'receipt_fields': {'image_version': 'bazel-ci'},
          'selected_archives': ['docker-router-advertiser.gz', 'docker-router-advertiser-dbg.gz']},
 'sflow': {'full_label_layouts': False,
           'images': ['docker-sflow', 'docker-sflow-dbg'],
           'manifest_command': ['make',
                                '-f',
                                'tools/bazel/prepare_manifests.mk',
                                'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                                'rules/docker-config-engine-trixie.mk rules/docker-swss-layer-trixie.mk '
                                'rules/docker-sflow.mk',
                                'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                                'BLDENV=trixie',
                                'CONFIGURED_PLATFORM=vs',
                                'CONFIGURED_ARCH=amd64',
                                'INCLUDE_FIPS=y',
                                'ENABLE_ASAN=n',
                                'DBG_IMAGE_MARK=dbg',
                                'DOCKERS_PATH=dockers'],
           'module': 'sflow_image',
           'owner_sha256': '2ff26f9d2e36b600626c9c0ac004c6ef74a7d12c0d50ef6ebf6f397ad0f1b9a1',
           'parent': '//dockers/docker-swss-layer-trixie:docker-swss-layer-trixie',
           'prefix': '//dockers/docker-sflow:',
           'receipt_fields': {},
           'selected_archives': ['docker-sflow.gz', 'docker-sflow-dbg.gz']},
 'snmp': {'full_label_layouts': False,
          'images': ['docker-snmp', 'docker-snmp-dbg'],
          'manifest_command': ['make',
                               '-f',
                               'tools/bazel/prepare_manifests.mk',
                               'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                               'rules/docker-config-engine-trixie.mk rules/docker-snmp.mk',
                               'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                               'BLDENV=trixie',
                               'CONFIGURED_PLATFORM=vs',
                               'CONFIGURED_ARCH=amd64',
                               'INCLUDE_FIPS=y',
                               'ENABLE_ASAN=n',
                               'DBG_IMAGE_MARK=dbg',
                               'DOCKERS_PATH=dockers'],
          'module': 'snmp_image',
          'owner_sha256': '69a7525dedd9b6485923865f07670864755e172c1bc433cd7ed0c91601451b9d',
          'parent': '//dockers/docker-config-engine-trixie:docker-config-engine-trixie',
          'prefix': '//dockers/docker-snmp:',
          'receipt_fields': {'image_version': 'bazel-ci'},
          'selected_archives': ['docker-snmp.gz', 'docker-snmp-dbg.gz']},
 'swss-layer': {'full_label_layouts': False,
                'images': ['docker-swss-layer-trixie'],
                'manifest_command': ['make',
                                     '-f',
                                     'tools/bazel/prepare_manifests.mk',
                                     'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                                     'rules/docker-config-engine-trixie.mk rules/docker-swss-layer-trixie.mk',
                                     'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                                     'BLDENV=trixie',
                                     'CONFIGURED_PLATFORM=vs',
                                     'CONFIGURED_ARCH=amd64',
                                     'INCLUDE_FIPS=y',
                                     'ENABLE_ASAN=n',
                                     'DBG_IMAGE_MARK=dbg',
                                     'DOCKERS_PATH=dockers'],
                'module': 'swss_layer',
                'owner_sha256': '37d79beaca1d12f2567a8ebe702f9400bd76ae77a826a19ba9ef3e6fd8b91849',
                'parent': '//dockers/docker-config-engine-trixie:docker-config-engine-trixie',
                'prefix': '//dockers/docker-swss-layer-trixie:',
                'receipt_fields': {},
                'selected_archives': ['docker-swss-layer-trixie.gz']},
 'sysmgr': {'full_label_layouts': False,
            'images': ['docker-sysmgr', 'docker-sysmgr-dbg'],
            'manifest_command': ['make',
                                 '-f',
                                 'tools/bazel/prepare_manifests.mk',
                                 'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                                 'rules/docker-config-engine-trixie.mk rules/docker-sysmgr.mk',
                                 'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                                 'BLDENV=trixie',
                                 'CONFIGURED_PLATFORM=vs',
                                 'CONFIGURED_ARCH=amd64',
                                 'INCLUDE_FIPS=y',
                                 'ENABLE_ASAN=n',
                                 'DBG_IMAGE_MARK=dbg',
                                 'DOCKERS_PATH=dockers'],
            'module': 'sysmgr_image',
            'owner_sha256': 'd11a7ecdbd3c76d3edaccb4c2a0c8b7964460c665cdeed73b67c9f8c5167a408',
            'parent': '//dockers/docker-config-engine-trixie:docker-config-engine-trixie',
            'prefix': '//dockers/docker-sysmgr:',
            'receipt_fields': {},
            'selected_archives': ['docker-sysmgr.gz', 'docker-sysmgr-dbg.gz']},
 'teamd': {'full_label_layouts': False,
           'images': ['docker-teamd', 'docker-teamd-dbg'],
           'manifest_command': ['make',
                                '-f',
                                'tools/bazel/prepare_manifests.mk',
                                'MANIFEST_METADATA=rules/docker-base-trixie.mk '
                                'rules/docker-config-engine-trixie.mk rules/docker-swss-layer-trixie.mk '
                                'rules/docker-teamd.mk',
                                'BUILD_WITH_BAZEL_WHEN_AVAILABLE=y',
                                'BLDENV=trixie',
                                'CONFIGURED_PLATFORM=vs',
                                'CONFIGURED_ARCH=amd64',
                                'INCLUDE_FIPS=y',
                                'ENABLE_ASAN=n',
                                'DBG_IMAGE_MARK=dbg',
                                'DOCKERS_PATH=dockers'],
           'module': 'teamd_image',
           'owner_sha256': 'b29e16aac7b151f0b1c2ffaf6d274f9e35ffd720e3d0779e2d61ec747758c38b',
           'parent': '//dockers/docker-swss-layer-trixie:docker-swss-layer-trixie',
           'prefix': '//dockers/docker-teamd:',
           'receipt_fields': {},
           'selected_archives': ['docker-teamd.gz', 'docker-teamd-dbg.gz']}}
COMPONENT_PROFILE = ['--config=syncd-dash', '--//dockers/docker-snmp:image_version=bazel-ci']


def load_plan(path):
    value = json.loads(path.read_bytes())
    require(value['schema'] == 1 and value['architecture'] == 'amd64' and
            value['scope'] == 'coherent-swss-syncd' and value['policy_targets'] == POLICIES,
            'Different coherent policy scope')
    require(set(value['expected_native_archives']) == set(FAMILIES['syncd'][1]),
            'Different accepted native archive set')
    require(all(value['exports'].get(name) == target for name, target in PRIMARY.items()),
            'Different primary target')
    if 'component_refresh' in value:
        require(value['component_refresh'] is True and
                value['component_families'] == sorted(COMPONENTS) and
                value['component_profile'] == COMPONENT_PROFILE and
                value['snmp_image_version_profile'] == 'bazel-ci' and
                len(value['expected_native_base_diff_ids']) == 9, 'Different component refresh scope')
    return value


def environment_check(plan, scratch, source):
    expected = {
        'BAZEL_REAL': str(scratch / 'native-bazel'),
        'SONIC_BAZEL_REGISTRY_REVISION': plan['catalog_revision'],
        'USE_BAZEL_VERSION': '8.5.1', 'BAZELISK_HOME': str(scratch / 'bazelisk'),
        'SOURCE_INPUTS_RUN': str(plan['source_inputs']['run_id']),
        'TMPDIR': str(scratch / 'temporary'),
    }
    require(all(os.environ.get(key) == value for key, value in expected.items()),
            'Build environment differs from the prepared remote-only launcher')
    if plan.get('component_refresh'):
        require(not any(os.environ.get(key) for key in (
            'SONIC_CONTAINER_BAZEL_OPTIONS', 'SONIC_CONTAINER_BAZEL_CACHE_DIR',
            'SONIC_SWSS_BAZEL_OPTIONS', 'SONIC_SWSS_BAZEL_CACHE_DIR')),
            'Unexpected component driver options or local cache inputs')
    # Reuse the current source owner: it filters user rc files before Bazel runs.
    # The sealed native launcher explicitly disables only these two local caches.
    home_rc = Path.home() / '.bazelrc'
    if not home_rc.exists() and not home_rc.is_symlink():
        return
    setup.source_check(source, plan['source_revision'])
    owner_path = source / 'tools/bazel/ci/review_bazel_rc.py'
    spec = importlib.util.spec_from_file_location('_cohort_review_bazel_rc', owner_path)
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    isolated_home = scratch / 'home'
    require(not (isolated_home / '.bazelrc').exists() and
            not (isolated_home / '.bazelrc').is_symlink(), 'Private comparison rc must be absent')

    def effective(configuration):
        result = []
        for line in configuration.splitlines():
            tokens = shlex.split(line, comments=True)
            if (len(tokens) == 2 and tokens[0] in ('common', 'build', 'test') and
                    tokens[1].split('=', 1)[0] in ('--disk_cache', '--repository_cache')):
                continue
            result.append(line)
        return result

    require(effective(owner.configuration(source, Path.home())) ==
            effective(owner.configuration(source, isolated_home)),
            'Home Bazel rc changes effective source inputs or cache options')


def declarations(plan, owners, components=None):
    exports, layouts = dict(PRIMARY), {BASE}
    for family, owner in owners.items():
        prefix, images = FAMILIES[family]
        require(owner.OWNER == prefix and tuple(owner.IMAGES) == images and owner.BASE == BASE,
                'Different image owner scope')
        for name, target in owner.TARGETS.items():
            require(Path(name).name == name and name not in ('', '.', '..'), 'Different export name')
            if name in PRIMARY:
                require(PRIMARY[name] == target, 'Different primary image target')
            else:
                exports[family + '--' + name] = target
        layouts.update(prefix + name for name in images)
    require(owners['syncd'].OPTIONS == OPTIONS, 'Different Syncd options')
    if components is not None:
        require(plan.get('component_refresh') is True and set(components) == set(COMPONENTS),
                'Incomplete fixed component owner set')
        primary = component_primary()
        for family, owner in components.items():
            spec = COMPONENTS[family]
            require(owner.OWNER == spec['prefix'], 'Different component owner prefix')
            images = [owner.IMAGE.split(':')[-1]] if family == 'swss-layer' else list(owner.IMAGES)
            require(images == spec['images'], 'Different component image set')
            for name, target in owner.TARGETS.items():
                require(Path(name).name == name and name not in ('', '.', '..'), 'Different export name')
                exported = name if name in primary else family + '--' + name
                require(exported not in exports, 'Repeated component export')
                if name in primary:
                    require(primary[name] == target, 'Different component archive target')
                exports[exported] = target
            layouts.update(spec['prefix'] + name for name in spec['images'])
            layouts.add(spec['parent'])
    else:
        require(not plan.get('component_refresh'), 'Missing component owners')
    require(exports == plan['exports'], 'Different reviewed target closure')
    return exports, sorted(layouts)


def manifest_snapshot(source):
    result = {}
    for image in ('docker-base-trixie', 'docker-config-engine-trixie'):
        directory = source / 'target/bazel-manifests' / image
        require(directory.is_dir() and not directory.is_symlink(), 'Missing shared Make manifest')
        entries = list(directory.iterdir())
        require(1 <= len(entries) <= 2 and any(p.name == 'manifest.json' for p in entries),
                'Different shared manifest files')
        for path in entries:
            require(path.name in ('manifest.json', 'image.env') and path.is_file() and
                    not path.is_symlink() and 0 < path.stat().st_size <= 1024**2,
                    'Different shared manifest member')
            raw = path.read_bytes()
            result[str(path.relative_to(source))] = {
                'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    return result


def bind_policies(paths, receipt, plan, check, merge):
    actual = {}
    for name in PRIMARY:
        if name.endswith('.gz'):
            path = paths[name]
            row = receipt['artifacts'][name]
            require(path.is_file() and not path.is_symlink() and
                    path.stat().st_size == row['bytes'] and check.sha(path) == row['sha256'],
                    'Exported archive identity differs')
            actual[name[:-3]] = {'bytes': row['bytes'], 'sha256': row['sha256']}
    mismatches = [name for name, expected in plan['expected_native_archives'].items()
                  if actual[name] != expected]
    require(not mismatches, 'Accepted native archive differs: ' + json.dumps({
        name: {
            'target': PRIMARY[name + '.gz'],
            'expected': {key: plan['expected_native_archives'][name][key]
                         for key in ('bytes', 'sha256')},
            'observed': actual[name],
        } for name in mismatches}, sort_keys=True))
    policies, identities = [], {}
    for family, (_, images) in FAMILIES.items():
        name = family + '-installed-images-policy.json'
        expected = {image: actual[image] for image in ('docker-config-engine-trixie', *images)}
        identities[name] = setup.validate_policy(paths[name], {'expected_archives': expected})
        policies.append(json.loads(paths[name].read_bytes()))
    # Existing schema/complete shared-image equality, without a replacement merger.
    merge(policies)
    return actual, identities


def component_primary():
    return {name: spec['prefix'] + name for spec in COMPONENTS.values()
            for name in spec['selected_archives']}


def component_proxy_text(source, scratch):
    command = shlex.join([str(source / 'tools/bazel/ci/review_bazel.sh'), '--batch',
                          '--output_user_root=' + str(scratch / 'output-user-root')])
    text = '#!/bin/sh\ncase "${1-}" in\n  build|test|aquery|cquery)\n'
    for index, option in enumerate(COMPONENT_PROFILE):
        variable = 'cohort_option_' + str(index)
        text += '    ' + variable + '=false\n    for cohort_argument do\n'
        text += '      if [ "$cohort_argument" = ' + shlex.quote(option) + ' ]; then\n'
        text += '        ' + variable + '=true\n      fi\n    done\n'
        text += '    if [ "$' + variable + '" = false ]; then set -- "$@" ' + shlex.quote(option) + '; fi\n'
    # Preserve every JSON record while avoiding Bazel's whole-log dependency sort.
    text += '    ;;\nesac\ncase "${1-}" in\n  build) set -- "$@" --execution_log_sort=false ;;\nesac\n'
    return text + 'exec ' + command + ' "$@"\n'


def prepare_component_proxy(source, scratch):
    # Extend the existing task-local proxy. The qualified native launcher still
    # supplies the endpoint, disables local caches, and enforces resource limits.
    proxy = scratch / 'review-bazel'
    command = shlex.join([str(source / 'tools/bazel/ci/review_bazel.sh'), '--batch',
                          '--output_user_root=' + str(scratch / 'output-user-root')])
    original = '#!/bin/sh\nexec ' + command + ' "$@"\n'
    require(proxy.is_file() and not proxy.is_symlink() and proxy.read_text() == original,
            'Different prepared review proxy')
    proxy.write_text(component_proxy_text(source, scratch))
    proxy.chmod(0o700)


def bind_component_archives(paths, receipt, check):
    actual = {}
    for name in component_primary():
        path, row = paths[name], receipt['artifacts'][name]
        require(path.is_file() and not path.is_symlink() and
                path.stat().st_size == row['bytes'] and check.sha(path) == row['sha256'],
                'Component archive identity differs')
        actual[name[:-3]] = {'bytes': row['bytes'], 'sha256': row['sha256']}
    return actual


def check_base_parent(plan, source, private, receipt, bazel, audit, builder, logs, layout_reader, check):
    """Reject incompatible BASE inputs before the full native archive gate."""
    directory = private / 'base-parent-preflight'
    directory.mkdir(mode=0o700)
    target = '//dockers/docker-base-trixie:docker-base-trixie'
    targets = {'docker-base-trixie.gz': PRIMARY['docker-base-trixie.gz']}
    audit.audit_targets(bazel, OPTIONS, [*targets.values(), target],
                        workspace=source, output=directory / 'action-audit.json')
    original = {'commands': []}
    paths = builder.collect_archives(source, directory, original, targets, bazel=bazel,
                                      options=OPTIONS, additional_targets=[target])
    require(set(paths) == set(targets), 'Incomplete BASE preflight export')
    values = logs.execute([bazel, 'cquery', *OPTIONS, '--output=files', target],
                          directory, original, 'base.layout', cwd=source).splitlines()
    require(len(values) == 1, 'Different BASE preflight layout count')
    layout = layout_reader(source / values[0], 'linux/amd64')
    expected = plan['expected_native_base_diff_ids']
    observed = layout.config['rootfs']['diff_ids']
    archive = paths['docker-base-trixie.gz']
    result = {'status': 'passed' if observed == expected else 'failed',
              'target': target, 'profile': COMPONENT_PROFILE,
              'expected_diff_ids': expected, 'observed_diff_ids': observed,
              'source_revision': plan['source_revision'],
              'archive': {'bytes': archive.stat().st_size, 'sha256': check.sha(archive)},
              'comparison_scope': 'preliminary_rootfs_prefix_only',
              'stored_layer_or_full_archive_equivalence': False,
              'full_native_archive_gate_required': True}
    (directory / 'prefix.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    receipt['base_parent_preflight'] = result
    require(observed == expected, 'BASE parent differs from accepted native prefix: ' +
            json.dumps({'expected': expected, 'observed': observed}, sort_keys=True))
    return result


def collect_component_exports(source, directory, receipt, targets, *, builder,
                              check, bazel, options, additional_targets,
                              execution_log):
    """Query each exact label once, retaining every declared export identity."""
    if not targets or any(not name or Path(name).name != name or name in ('.', '..')
                          for name in targets):
        raise ValueError('archive declarations need nonempty, plain output filenames')
    representatives = {}
    selected = {}
    for name, target in targets.items():
        if target not in representatives:
            representatives[target] = name
            selected[name] = target
    paths = builder.collect_archives(
        source, directory, receipt, selected, bazel=bazel, options=options,
        additional_targets=additional_targets, execution_log=execution_log)
    require(set(paths) == set(selected), 'Component collector outputs differ')
    for name, target in targets.items():
        representative = representatives[target]
        if name == representative:
            continue
        original = paths[representative]
        row = receipt.get('artifacts', {}).get(representative)
        require(isinstance(row, dict) and row.get('target') == target,
                'Component representative receipt differs')
        require(original.is_file() and not original.is_symlink() and
                original.stat().st_size > 0 and
                original.stat().st_size == row.get('bytes') and
                check.sha(original) == row.get('sha256'),
                'Component representative file differs')
        destination = directory / name
        builder.export_archive(original, destination)
        require(destination.is_file() and not destination.is_symlink() and
                destination.stat().st_size == row['bytes'] and
                check.sha(destination) == row['sha256'],
                'Component alias copy differs')
        paths[name] = destination
        receipt['artifacts'][name] = dict(row)
    return {name: paths[name] for name in targets}


BATCH_MAIN_LABELS = frozenset(('//dockers/docker-dash-ha:all_debug_symbols',
 '//dockers/docker-dash-ha:debug_apt_selection',
 '//dockers/docker-dash-ha:debug_client',
 '//dockers/docker-dash-ha:debug_installed_packages',
 '//dockers/docker-dash-ha:debug_package_metadata',
 '//dockers/docker-dash-ha:debug_tools',
 '//dockers/docker-dash-ha:files',
 '//dockers/docker-dash-ha:installed_packages',
 '//dockers/docker-dash-ha:runtime_apt_selection',
 '//dockers/docker-dash-ha:runtime_package_metadata',
 '//dockers/docker-dash-ha:source_runtime',
 '//dockers/docker-dash-ha:test_redis',
 '//dockers/docker-database:all_debug_symbols',
 '//dockers/docker-database:debug_client',
 '//dockers/docker-database:debug_installed_packages',
 '//dockers/docker-database:debug_package_metadata',
 '//dockers/docker-database:debug_tools',
 '//dockers/docker-database:installed_packages',
 '//dockers/docker-database:runtime_package_metadata',
 '//dockers/docker-database:source_debug_symbols',
 '//dockers/docker-database:source_runtime',
 '//dockers/docker-eventd:all_debug_symbols',
 '//dockers/docker-eventd:debug_client',
 '//dockers/docker-eventd:debug_installed_packages',
 '//dockers/docker-eventd:debug_package_metadata',
 '//dockers/docker-eventd:debug_tools',
 '//dockers/docker-eventd:test_redis',
 '//dockers/docker-fpm-frr:all_debug_symbols',
 '//dockers/docker-fpm-frr:debug_client',
 '//dockers/docker-fpm-frr:debug_installed_packages',
 '//dockers/docker-fpm-frr:debug_package_metadata',
 '//dockers/docker-fpm-frr:debug_tools',
 '//dockers/docker-fpm-frr:files',
 '//dockers/docker-fpm-frr:installed_packages',
 '//dockers/docker-fpm-frr:python_runtime',
 '//dockers/docker-fpm-frr:runtime_package_metadata',
 '//dockers/docker-fpm-frr:source_runtime',
 '//dockers/docker-fpm-frr:test_redis',
 '//dockers/docker-lldp:all_debug_symbols',
 '//dockers/docker-lldp:debug_client',
 '//dockers/docker-lldp:debug_installed_packages',
 '//dockers/docker-lldp:debug_package_metadata',
 '//dockers/docker-lldp:debug_tools',
 '//dockers/docker-lldp:installed_packages',
 '//dockers/docker-lldp:runtime_package_metadata',
 '//dockers/docker-lldp:source_runtime',
 '//dockers/docker-lldp:test_redis',
 '//dockers/docker-macsec:all_debug_symbols',
 '//dockers/docker-macsec:debug_apt_selection',
 '//dockers/docker-macsec:debug_client',
 '//dockers/docker-macsec:debug_installed_packages',
 '//dockers/docker-macsec:debug_package_metadata',
 '//dockers/docker-macsec:debug_tools',
 '//dockers/docker-macsec:files',
 '//dockers/docker-macsec:installed_packages',
 '//dockers/docker-macsec:runtime_apt_selection',
 '//dockers/docker-macsec:runtime_package_metadata',
 '//dockers/docker-macsec:source_runtime',
 '//dockers/docker-macsec:test_redis',
 '//dockers/docker-mux:all_debug_symbols',
 '//dockers/docker-mux:debug_client',
 '//dockers/docker-mux:debug_installed_packages',
 '//dockers/docker-mux:debug_package_metadata',
 '//dockers/docker-mux:debug_tools',
 '//dockers/docker-mux:installed_packages',
 '//dockers/docker-mux:runtime_package_metadata',
 '//dockers/docker-mux:source_runtime',
 '//dockers/docker-mux:test_redis',
 '//dockers/docker-orchagent:all_debug_symbols',
 '//dockers/docker-orchagent:debug_accounts',
 '//dockers/docker-orchagent:debug_apt_selection',
 '//dockers/docker-orchagent:debug_installed_packages',
 '//dockers/docker-orchagent:debug_package_metadata',
 '//dockers/docker-orchagent:debug_tools',
 '//dockers/docker-orchagent:package_policy',
 '//dockers/docker-orchagent:rdeps_debug_symbols',
 '//dockers/docker-orchagent:runtime_apt_selection',
 '//dockers/docker-router-advertiser:all_debug_symbols',
 '//dockers/docker-router-advertiser:debug_client',
 '//dockers/docker-router-advertiser:debug_installed_packages',
 '//dockers/docker-router-advertiser:debug_package_metadata',
 '//dockers/docker-router-advertiser:debug_tools',
 '//dockers/docker-router-advertiser:installed_packages',
 '//dockers/docker-router-advertiser:runtime_package_metadata',
 '//dockers/docker-router-advertiser:test_redis',
 '//dockers/docker-sflow:all_debug_symbols',
 '//dockers/docker-sflow:debug_apt_selection',
 '//dockers/docker-sflow:debug_client',
 '//dockers/docker-sflow:debug_installed_packages',
 '//dockers/docker-sflow:debug_package_metadata',
 '//dockers/docker-sflow:debug_tools',
 '//dockers/docker-sflow:files',
 '//dockers/docker-sflow:installed_packages',
 '//dockers/docker-sflow:runtime_apt_selection',
 '//dockers/docker-sflow:runtime_package_metadata',
 '//dockers/docker-sflow:source_runtime',
 '//dockers/docker-sflow:test_redis',
 '//dockers/docker-snmp:all_debug_symbols',
 '//dockers/docker-snmp:debug_client',
 '//dockers/docker-snmp:debug_installed_packages',
 '//dockers/docker-snmp:debug_package_metadata',
 '//dockers/docker-snmp:installed_packages',
 '//dockers/docker-snmp:runtime_package_metadata',
 '//dockers/docker-snmp:test_redis',
 '//dockers/docker-sonic-gnmi:all_debug_symbols',
 '//dockers/docker-sonic-gnmi:debug_apt_selection',
 '//dockers/docker-sonic-gnmi:debug_client',
 '//dockers/docker-sonic-gnmi:debug_installed_packages',
 '//dockers/docker-sonic-gnmi:debug_package_metadata',
 '//dockers/docker-sonic-gnmi:debug_tools',
 '//dockers/docker-sonic-gnmi:installed_packages',
 '//dockers/docker-sonic-gnmi:installed_probe',
 '//dockers/docker-sonic-gnmi:runtime_apt_selection',
 '//dockers/docker-sonic-gnmi:runtime_package_metadata',
 '//dockers/docker-sonic-gnmi:source_runtime',
 '//dockers/docker-sonic-gnmi:test_redis',
 '//dockers/docker-sonic-mgmt-framework:all_debug_symbols',
 '//dockers/docker-sonic-mgmt-framework:debug_client',
 '//dockers/docker-sonic-mgmt-framework:debug_installed_packages',
 '//dockers/docker-sonic-mgmt-framework:debug_package_metadata',
 '//dockers/docker-sonic-mgmt-framework:debug_tools',
 '//dockers/docker-sonic-mgmt-framework:installed_packages',
 '//dockers/docker-sonic-mgmt-framework:python_runtime',
 '//dockers/docker-sonic-mgmt-framework:runtime_package_metadata',
 '//dockers/docker-sonic-mgmt-framework:source_runtime',
 '//dockers/docker-sonic-mgmt-framework:test_redis',
 '//dockers/docker-sonic-otel:all_debug_symbols',
 '//dockers/docker-sonic-otel:debug_apt_selection',
 '//dockers/docker-sonic-otel:debug_client',
 '//dockers/docker-sonic-otel:debug_installed_packages',
 '//dockers/docker-sonic-otel:debug_package_metadata',
 '//dockers/docker-sonic-otel:debug_tools',
 '//dockers/docker-sonic-otel:files',
 '//dockers/docker-sonic-otel:installed_packages',
 '//dockers/docker-sonic-otel:runtime_apt_selection',
 '//dockers/docker-sonic-otel:runtime_package_metadata',
 '//dockers/docker-sonic-otel:source_runtime',
 '//dockers/docker-sonic-otel:test_redis',
 '//dockers/docker-sysmgr:all_debug_symbols',
 '//dockers/docker-sysmgr:debug_apt_selection',
 '//dockers/docker-sysmgr:debug_client',
 '//dockers/docker-sysmgr:debug_installed_packages',
 '//dockers/docker-sysmgr:debug_package_metadata',
 '//dockers/docker-sysmgr:debug_tools',
 '//dockers/docker-sysmgr:files',
 '//dockers/docker-sysmgr:installed_packages',
 '//dockers/docker-sysmgr:runtime_apt_selection',
 '//dockers/docker-sysmgr:runtime_package_metadata',
 '//dockers/docker-sysmgr:source_runtime',
 '//dockers/docker-sysmgr:test_services',
 '//dockers/docker-teamd:all_debug_symbols',
 '//dockers/docker-teamd:debug_apt_selection',
 '//dockers/docker-teamd:debug_client',
 '//dockers/docker-teamd:debug_installed_packages',
 '//dockers/docker-teamd:debug_package_metadata',
 '//dockers/docker-teamd:debug_tools',
 '//dockers/docker-teamd:files',
 '//dockers/docker-teamd:installed_packages',
 '//dockers/docker-teamd:runtime_apt_selection',
 '//dockers/docker-teamd:runtime_package_metadata',
 '//dockers/docker-teamd:source_runtime',
 '//dockers/docker-teamd:test_redis',
 '//platform/vs/docker-dash-engine:all_debug_symbols',
 '//platform/vs/docker-dash-engine:debian_runtime_payload',
 '//platform/vs/docker-dash-engine:debug_apt_selection',
 '//platform/vs/docker-dash-engine:debug_client',
 '//platform/vs/docker-dash-engine:debug_installed_packages',
 '//platform/vs/docker-dash-engine:debug_package_metadata',
 '//platform/vs/docker-dash-engine:debug_tools',
 '//platform/vs/docker-dash-engine:files',
 '//platform/vs/docker-dash-engine:installed_packages',
 '//platform/vs/docker-dash-engine:runtime_apt_selection',
 '//platform/vs/docker-dash-engine:runtime_package_metadata',
 '//platform/vs/docker-dash-engine:source_runtime'))


def batch_json(text):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'Repeated discovery JSON key')
            value[key] = item
        return value
    return json.loads(text, object_pairs_hook=unique)


def batch_completed_groups(events, labels):
    """Partition only successful top-level identities in this build's own BEP."""
    labels = set(labels)
    identities = {}
    with events.open() as stream:
        for line in stream:
            event = batch_json(line)
            target = event.get('id', {}).get('targetCompleted')
            if not isinstance(target, dict) or target.get('label') not in labels:
                continue
            if target.get('aspect', '') != '':
                continue
            label = target['label']
            require(label not in identities, 'Repeated completed batch label')
            require(event.get('completed', {}).get('success') is True,
                    'Batch target did not complete successfully')
            configuration = target.get('configuration', {}).get('id')
            require(isinstance(configuration, str) and configuration,
                    'Missing completed batch configuration')
            identities[label] = configuration
    require(set(identities) == labels, 'Incomplete completed batch labels')
    groups = {}
    for label in sorted(labels):
        groups.setdefault(identities[label], []).append(label)
    # Opaque IDs group only this stream. They are never cquery config() inputs.
    return list(groups.values())


def batch_label_description(value):
    """Bound diagnostics to label syntax; never print arbitrary response content."""
    if isinstance(value, str):
        if len(value) <= 256 and re.fullmatch(
                r'(?:@@?[A-Za-z0-9_.+-]*)?//[A-Za-z0-9_./+-]+:[A-Za-z0-9_./+-]+', value):
            return repr(value)
        return '[str length=' + str(len(value)) + ' sha256=' + hashlib.sha256(
            value.encode('utf-8', errors='surrogatepass')).hexdigest() + ']'
    return '[' + type(value).__name__ + ']'


def batch_main_label(value, labels):
    """Map only the pinned main-repository spellings, never repository aliases."""
    detail = batch_label_description(value)
    require(isinstance(value, str) and
            (value.startswith('//') or value.startswith('@@//')),
            'Unsupported main-repository cquery label: ' + detail)
    label = value[2:] if value.startswith('@@//') else value
    require(label in labels, 'Unexpected cquery label: ' + detail)
    return label


def batch_configuration(value, labels):
    """Read genuine 8.5.1 jsonproto metadata; never fingerprint build_options."""
    require(isinstance(value, dict) and isinstance(value.get('results'), list) and
            isinstance(value.get('configurations'), list), 'Different cquery metadata shape')
    configurations = {}
    for row in value['configurations']:
        require(isinstance(row, dict), 'Different cquery configuration record')
        identifier = row.get('id')
        checksum = row.get('checksum')
        require(type(identifier) is int and identifier > 0 and identifier not in configurations and
                isinstance(checksum, str) and re.fullmatch('[0-9a-f]{64}', checksum),
                'Different cquery configuration identity')
        configurations[identifier] = checksum
    found = {}
    for row in value['results']:
        require(isinstance(row, dict), 'Different cquery target metadata')
        target = row.get('target', {})
        require(isinstance(target, dict), 'Different cquery target structure')
        if target.get('type') != 'RULE':
            name = next((target[key].get('name') for key in
                         ('sourceFile', 'generatedFile', 'packageGroup', 'environmentGroup', 'rule')
                         if isinstance(target.get(key), dict)), None)
            require(False, 'Cquery target is not a rule: ' + batch_label_description(name))
        rule = target.get('rule', {})
        require(isinstance(rule, dict), 'Different cquery rule structure')
        detail = batch_label_description(rule.get('name'))
        require(isinstance(rule.get('ruleClass'), str) and rule['ruleClass'],
                'Missing cquery rule class: ' + detail)
        require(rule['ruleClass'] != 'alias', 'Cquery alias is not allowed: ' + detail)
        label = batch_main_label(rule.get('name'), labels)
        require(label not in found, 'Repeated cquery rule label after main mapping: ' + detail)
        configuration = row.get('configuration', {})
        require(isinstance(configuration, dict), 'Different cquery target configuration: ' + detail)
        checksum = configuration.get('checksum')
        identifier = row.get('configurationId')
        require(type(identifier) is int and identifier in configurations and
                checksum == configurations[identifier], 'Different cquery target configuration')
        found[label] = checksum
    require(set(found) == set(labels), 'Incomplete cquery metadata labels')
    checksums = set(found.values())
    require(len(checksums) == 1, 'Ambiguous cquery batch configuration')
    return next(iter(checksums))


def batch_output_paths(text, labels):
    paths = {}
    for line in text.splitlines():
        row = batch_json(line)
        require(isinstance(row, dict) and set(row) == {'label', 'files'},
                'Different cquery file row')
        label = batch_main_label(row['label'], labels)
        require(label not in paths, 'Repeated cquery file label after main mapping: ' +
                batch_label_description(row['label']))
        files = row['files']
        require(isinstance(files, list) and len(files) == 1 and
                isinstance(files[0], str) and files[0] and
                not Path(files[0]).is_absolute() and '..' not in Path(files[0]).parts and
                str(Path(files[0])) == files[0], 'Different single relative cquery output')
        paths[label] = files[0]
    require(set(paths) == set(labels), 'Incomplete cquery file labels')
    return paths


def collect_batched_component_exports(source, directory, receipt, targets, *, builder,
                                      check, bazel, options, additional_targets,
                                      execution_log):
    """Keep difficult labels on the collector; batch the fixed main-rule subset."""
    require(targets and all(name and Path(name).name == name and name not in ('.', '..')
                            for name in targets), 'archive declarations need plain output filenames')
    batch = {name: target for name, target in targets.items() if target in BATCH_MAIN_LABELS}
    original = {name: target for name, target in targets.items() if target not in BATCH_MAIN_LABELS}
    require(set(batch.values()) == set(BATCH_MAIN_LABELS) and original,
            'Different fixed batched archive scope')
    paths = collect_component_exports(
        source, directory, receipt, original, builder=builder, check=check, bazel=bazel,
        options=options, additional_targets=[*sorted(set(batch.values())), *additional_targets],
        execution_log=execution_log)
    events = directory / 'build-events.jsonl'
    groups = batch_completed_groups(events, batch.values())
    root = builder.execution_root(events)
    index = {'label_count': len(set(batch.values())), 'group_count': len(groups),
             'query_count': 0, 'groups': []}
    receipt['archive_discovery'] = index
    for ordinal, labels in enumerate(groups):
        # The labels are a reviewed literal allowlist, not inferred file names.
        require(all(re.fullmatch(r'//[A-Za-z0-9_./+-]+:[A-Za-z0-9_./+-]+', x)
                    for x in labels), 'Different batched main label syntax')
        expression = 'set(' + ' '.join(labels) + ')'
        metadata_name = 'cohort.archive-config.' + str(ordinal)
        files_name = 'cohort.archive-files.' + str(ordinal)
        records = []

        def query(name, arguments):
            output = directory / (name + '.json')
            with output.open('x'):
                pass
            output.chmod(0o600)
            command_index = len(receipt['commands'])
            try:
                builder.command_log.execute([bazel, 'cquery', *options, *arguments], directory,
                                            receipt, name, cwd=source, output_path=output)
            finally:
                # The maintained logger also records an executed failing command.
                index['query_count'] += len(receipt['commands']) - command_index
            records.append({'command_index': command_index, 'name': name,
                            'bytes': output.stat().st_size, 'sha256': check.sha(output)})
            return output.read_text()

        metadata = query(metadata_name, ['--consistent_labels', '--output=jsonproto',
            '--proto:include_configurations', '--noproto:locations',
            '--proto:output_rule_attrs=', '--noproto:rule_inputs_and_outputs', expression])
        checksum = batch_configuration(batch_json(metadata), labels)
        files = query(files_name, ['--consistent_labels', '--output=starlark',
            '--starlark:expr=json.encode({"label": str(target.label), '
            '"files": [f.path for f in target.files.to_list()]})',
            'config(' + expression + ', ' + checksum + ')'])
        mapped = batch_output_paths(files, labels)
        # No per-label or per-alias command is fabricated. Both real commands
        # and their original private stdout hashes identify this shared map.
        index['groups'].append({'labels': labels, 'configuration_checksum': checksum,
                                'queries': records})
        for name, target in batch.items():
            if target not in mapped:
                continue
            path = root / mapped[target]
            require(path.is_file() and path.stat().st_size > 0,
                    'Expected exactly one nonempty archive output for ' + target)
            expected = {'target': target, 'bytes': path.stat().st_size, 'sha256': check.sha(path)}
            destination = directory / name
            require(not destination.exists() and not destination.is_symlink(),
                    'Preserve existing batch export')
            builder.export_archive(path, destination)
            require(destination.is_file() and not destination.is_symlink() and
                    destination.stat().st_size == expected['bytes'] and
                    check.sha(destination) == expected['sha256'], 'Batch export differs')
            paths[name] = destination
            receipt['artifacts'][name] = expected
    require(set(paths) == set(targets), 'Incomplete batched component exports')
    return {name: paths[name] for name in targets}


def collect_with_native_anchor(plan, source, private, receipt, exports, layouts,
                               bazel, audit, builder, check, merge):
    # The final proxy profile is already active for both this gate and every
    # subsequent existing owner build/test/aquery/cquery invocation.
    first = private / 'native-parent-preflight'
    first.mkdir(mode=0o700)
    core_layouts = sorted({BASE, *[prefix + name for prefix, images in FAMILIES.values()
                                 for name in images]})
    receipt['native_parent_action_audit'] = audit.audit_targets(
        bazel, OPTIONS, [*PRIMARY.values(), *core_layouts],
        workspace=source, output=first / 'action-audit.json')
    paths = builder.collect_archives(source, first, receipt, PRIMARY, bazel=bazel,
                                     options=[*OPTIONS, '--keep_going'], additional_targets=core_layouts)
    native_actual, policy_ids = bind_policies(paths, receipt, plan, check, merge)
    receipt['native_parent_preflight'] = {'archives': native_actual, 'policies': policy_ids,
                                          'profile': COMPONENT_PROFILE, 'passed': True}
    receipt['action_audit'] = audit.audit_targets(
        bazel, OPTIONS, [*exports.values(), *layouts],
        workspace=source, output=private / 'action-audit.json')
    remainder = {name: target for name, target in exports.items() if name not in PRIMARY}
    paths.update(collect_batched_component_exports(
        source, private, receipt, remainder, builder=builder, check=check,
        bazel=bazel, options=[*OPTIONS, '--keep_going'],
        additional_targets=layouts, execution_log=private / 'build.execution.json'))
    return paths


def component_base_owner_checks(source, private, receipt, bazel, logs, exporter, check, public_json, collected_paths):
    """Reuse the original base CLI, seven selectors and template rebuild owner."""
    directory = private / 'base'
    require(not directory.exists(), 'Preserve original base-owner inputs')
    directory.mkdir(mode=0o700)
    manifests = manifest_snapshot(source)
    owners = {
        'tools/bazel/ci/base_images.py': '510da5e87c168fae4b9ccbaa491255610e237f855f976f8df3e184afa7e3762b',
        'tools/bazel/ci/config_engine_rebuild.py': 'a77668728df8bd5f417541d9819a82e727dde8d73eb38c32ab93991857aa547b',
    }
    for name, expected in owners.items():
        path = source / name
        require(path.is_file() and not path.is_symlink() and check.sha(path) == expected,
                'Different original base-owner source')
    logs.execute([sys.executable, '-B', str(source / 'tools/bazel/ci/base_images.py'),
                  '--bazel', bazel, '--architecture', 'amd64', '--artifacts', str(directory),
                  '--config-engine-acceptance'], directory, receipt, 'original-base-owner', cwd=source)
    require(manifest_snapshot(source) == manifests, 'Base owner changed shared Make manifests')
    original = directory / 'receipt.json'
    value = json.loads(original.read_bytes())
    require(value['status'] == 'passed' and value['architecture'] == 'amd64' and
            value['revision'] == receipt['source_revision'], 'Original base owner did not pass')
    images = ('docker-base-trixie', 'docker-config-engine-trixie')
    for image in images:
        name = image + '.gz'
        expected = receipt['artifacts'][name]
        path = directory / name
        require(value['artifacts'][name] == expected and path.stat().st_size == expected['bytes'] and
                check.sha(path) == expected['sha256'] and
                collected_paths[name].stat().st_size == expected['bytes'] and
                check.sha(collected_paths[name]) == expected['sha256'],
                'Original base-owner archive differs from coherent collection')
    policy = directory / 'installed-images-policy.json'
    require(check.sha(policy) == value['installed_images_policy_sha256'],
            'Original two-image policy differs from base receipt')
    public_json(json.loads(policy.read_bytes()))
    logs.execute([sys.executable, '-B', str(source / 'tools/bazel/ci/config_engine_rebuild.py'),
                  '--bazel', bazel, '--output', str(directory / 'config-engine-rebuild.json')],
                 directory, receipt, 'original-config-engine-rebuild', cwd=source)
    rebuild_path = directory / 'config-engine-rebuild.json'
    rebuild = json.loads(rebuild_path.read_bytes())
    selected = {'base': 'docker-base-trixie.gz', 'image': 'docker-config-engine-trixie.gz',
                'startup': 'config-engine.startup.tar',
                'python_and_models': 'config-engine.python-and-models.tar',
                'native': 'docker-config-engine-trixie.source.tar'}
    expected = {name: value['artifacts'][target]['sha256'] for name, target in selected.items()}
    require(rebuild['status'] == 'passed' and set(rebuild['phases']) ==
            {'baseline', 'noop', 'template-edit', 'restore'} and all(
            rebuild['phases'][phase]['sha256'] == expected for phase in ('baseline', 'noop', 'restore')),
            'Original config-engine rebuild did not restore the coherent outputs')
    require({name for name in expected if rebuild['phases']['template-edit']['sha256'][name] !=
             expected[name]} == {'startup', 'image'}, 'Different config-engine declared-input invalidation')
    require(manifest_snapshot(source) == manifests, 'Config-engine rebuild changed shared manifests')
    public_json(rebuild)
    record = {key: value[key] for key in ('status', 'revision', 'architecture', 'artifacts',
                                        'images', 'installed_images_policy_sha256')}
    record['original_receipt'] = {'bytes': original.stat().st_size, 'sha256': check.sha(original)}
    record['diagnostic_validation_files'] = {image + '.validation.json': {
        'bytes': (directory / (image + '.validation.json')).stat().st_size,
        'sha256': check.sha(directory / (image + '.validation.json'))} for image in images}
    record['diagnostic_validation_public_admission_claimed'] = False
    record['config_engine_rebuild'] = {'bytes': rebuild_path.stat().st_size,
                                      'sha256': check.sha(rebuild_path)}
    public_json(record)
    output = private / 'owner-checks/base'
    output.mkdir(mode=0o700)
    (output / 'source-owner.json').write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
    exporter(policy, output / 'installed-images-policy.json')
    exporter(rebuild_path, output / 'config-engine-rebuild.json')
    exporter(directory / 'action-audit.json', output / 'action-audit.json')
    return record


def component_owner_checks(source, private, receipt, bazel, logs, audit, exporter, check, public_json, collected_paths):
    """Invoke the existing fixed source, owner-suite and rebuild gates."""
    directory = private / 'owner-checks'
    directory.mkdir(mode=0o700)
    unit_commands = [
        ['tools/bazel/tests/database_make_test.py'],
        ['tools/bazel/tests/eventd_make_test.py'],
        ['tools/bazel/tests/lldp_make_test.py'],
        ['tools/bazel/tests/lldp_identity_test.py'],
        ['tools/bazel/tests/mux_make_test.py'],
        ['tools/bazel/tests/radv_make_test.py'],
        ['-m', 'unittest', 'discover', '-s', 'tools/bazel/tests', '-p', 'mgmt_framework*_test.py'],
        ['tools/bazel/tests/swss_layer_payload_test.py'],
        ['tools/bazel/tests/gnmi_make_test.py'],
        ['tools/bazel/ci/gnmi_image_test.py'],
    ]
    for index, arguments in enumerate(unit_commands):
        logs.execute(['/usr/bin/env', 'PYTHONPATH=' + str(source), sys.executable, '-B', *arguments],
                     directory, receipt, 'unit-' + str(index), cwd=source)
    # ci_artifacts owns five distinct copy actions. This is the original
    # reuse-build collector and full owner-declared/Scapy suite, unchanged.
    owner_directory = directory / 'orchagent'
    logs.execute([sys.executable, '-B', str(source / 'tools/bazel/ci/container.py'),
                  '--container', '//dockers/docker-orchagent', '--artifacts', str(owner_directory),
                  '--bazel', bazel, '--reuse-build',
                  *['--bazel-arg=' + option for option in [*OPTIONS,
                    '--platforms=@sonic_build_infra//platforms:x86_64_trixie',
                    '--host_platform=@sonic_build_infra//platforms:x86_64_trixie']]],
                 directory, receipt, 'orchagent-owner', cwd=source)
    owner_report = json.loads((owner_directory / 'report.json').read_bytes())
    require(owner_report.get('status') == 'passed', 'Existing Orchagent owner checks failed')
    public_json(owner_report)
    original_sflow = logs.execute([bazel, 'query', 'kind(".*_test rule", @hsflowd//src/json:*)'],
                                   directory, receipt, 'sflow-original-test-targets', cwd=source).splitlines()
    require(len(original_sflow) == len(set(original_sflow)) == 22,
            'Different original sFlow owner test set')
    selections = {
        'sflow': (['@hsflowd//:package_test', '@sflowtool//:package_test',
                   '@libpsample//:package_test', *original_sflow],
                  [('hsflowd+', 23), ('sflowtool+', 1), ('libpsample+', 1)]),
        'otel': (['@otelcol_contrib//distributions/otelcol-contrib:source_contract_test',
                  '@otelcol_contrib//bazel:components_output_test',
                  '@otelcol_contrib//distributions/otelcol-contrib:package_test'],
                 [('otelcol-contrib+', 3)]),
        'dash-engine': (['@nnpy//:package_test'], [('nnpy+', 1)]),
    }
    records = {'base': component_base_owner_checks(source, private, receipt, bazel, logs,
                                                    exporter, check, public_json, collected_paths)}
    for family, (targets, roots) in selections.items():
        output = directory / family
        output.mkdir(mode=0o700)
        audit.audit_targets(bazel, OPTIONS, targets, workspace=source, output=output / 'action-audit.json')
        logs.execute([bazel, 'test', *OPTIONS, '--test_output=errors', *targets],
                     output, receipt, 'owner-tests', cwd=source)
        files = {}
        for owner, count in roots:
            owner_root = source / 'bazel-testlogs/external' / owner
            matches = sorted(owner_root.rglob('test.xml'))
            require(len(matches) == count, 'Different existing owner XML count')
            for xml in matches:
                require(xml.is_file() and xml.stat().st_size > 0, 'Missing existing owner test XML')
                relative = Path(owner) / xml.parent.relative_to(owner_root)
                copied = output / 'tests' / relative
                copied.mkdir(parents=True, mode=0o700)
                for name in (('test.xml', 'test.log') if family == 'sflow' else ('test.xml',)):
                    exporter(xml.with_name(name), copied / name)
                    files[str(relative / name)] = {'bytes': (copied / name).stat().st_size,
                                                  'sha256': check.sha(copied / name)}
        records[family] = {'status': 'passed', 'targets': targets, 'test_files': files,
                           'action_audit_sha256': check.sha(output / 'action-audit.json')}
    rebuilds = {
        'swss-layer': [
            '--runtime-target', '//dockers/docker-swss-layer-trixie:docker-swss-layer-trixie.gz',
            '--symbols-target', '//dockers/docker-swss-layer-trixie:docker-swss-layer-trixie.debug_symbols',
            '--additional-runtime-target', '//dockers/docker-swss-layer-trixie:source_runtime',
            '--unchanged-target', '//dockers/docker-config-engine-trixie:docker-config-engine-trixie.gz'],
        'orchagent': [
            '--runtime-target', '//dockers/docker-orchagent:docker-orchagent.gz',
            '--additional-runtime-target', '//dockers/docker-orchagent:docker-orchagent-dbg.gz',
            '--additional-runtime-target', '//dockers/docker-orchagent:rdeps',
            '--symbols-target', '//dockers/docker-orchagent:rdeps_debug_symbols',
            '--unchanged-target', '//dockers/docker-config-engine-trixie:docker-config-engine-trixie.gz',
            '--unchanged-target', '@scapy//:wheel'],
    }
    for family, targets in rebuilds.items():
        output = directory / (family + '-rebuild')
        logs.execute([sys.executable, '-B', str(source / 'tools/bazel/ci/swss_rebuild.py'),
                      '--bazel', bazel, *['--bazel-arg=' + option for option in OPTIONS],
                      *targets, '--output', str(output)],
                     directory, receipt, family + '-rebuild', cwd=source)
        row = json.loads((output / 'rebuild.json').read_bytes())
        require(row.get('source_restored') is True and
                [phase['phase'] for phase in row['builds']] == ['baseline', 'no-op', 'source-edit', 'restored'] and
                row['builds'][0]['outputs_sha256'] == row['builds'][1]['outputs_sha256'] ==
                row['builds'][3]['outputs_sha256'], 'Existing SWSS source restoration failed')
        public_json(row)
        records[family + '-rebuild'] = {'bytes': (output / 'rebuild.json').stat().st_size,
                                        'sha256': check.sha(output / 'rebuild.json')}
    owner_files = {}
    require(2 * len(owner_report['targets']) <= len(owner_report['test_outputs']) <=
            3 * len(owner_report['targets']), 'Different Orchagent owner evidence count')
    for name in owner_report['test_outputs']:
        relative = Path(name)
        require(not relative.is_absolute() and '..' not in relative.parts and
                len(relative.parts) == 3 and relative.parts[0] == 'tests' and
                relative.name in ('test.log', 'test.xml', 'validation.json'),
                'Different original Orchagent test evidence path')
        path = owner_directory / relative
        require(path.is_file() and not path.is_symlink() and path.stat().st_size > 0,
                'Missing original Orchagent owner test evidence')
        owner_files[name] = {'bytes': path.stat().st_size, 'sha256': check.sha(path)}
    records['orchagent'] = {'status': 'passed', 'bytes': (owner_directory / 'report.json').stat().st_size,
                            'sha256': check.sha(owner_directory / 'report.json'), 'test_files': owner_files}
    records['source-unit-commands'] = {'status': 'passed', 'count': len(unit_commands)}
    public_json(records)
    (directory / 'owner-checks.json').write_text(json.dumps(records, indent=2, sort_keys=True) + '\n')
    return records

def publish_component_owner_reports(private, reports, records, exporter, check, public_json):
    root = private / 'owner-checks'
    names = ['owner-checks.json', 'base/source-owner.json', 'base/installed-images-policy.json',
             'base/config-engine-rebuild.json', 'base/action-audit.json',
             'orchagent/report.json', 'orchagent/execution-gate-audit.json',
             'swss-layer-rebuild/rebuild.json', 'orchagent-rebuild/rebuild.json']
    for family in ('sflow', 'otel', 'dash-engine'):
        names.append(family + '/action-audit.json')
        names.extend(family + '/tests/' + name for name in records[family]['test_files'])
    names.extend('orchagent/' + name for name in records['orchagent']['test_files'])
    copied = {}
    for name in names:
        original = root / name
        destination = reports / 'owner-checks' / name
        require(original.is_file() and not original.is_symlink(), 'Missing original owner report')
        if original.suffix == '.json':
            public_json(json.loads(original.read_bytes()))
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        exporter(original, destination)
        require(destination.stat().st_size == original.stat().st_size and
                check.sha(destination) == check.sha(original), 'Copied owner report differs')
        copied[name] = {'bytes': destination.stat().st_size, 'sha256': check.sha(destination)}
    return copied


def validate_components(plan, source, private, receipt, paths, layouts, identity,
                        resolved, components, bazel, logs, exporter, check, public_json, audit):
    validations, incremental = {}, {}
    primary = component_primary()
    for family, owner in components.items():
        spec = COMPONENTS[family]
        directory = private / family
        directory.mkdir(mode=0o700)
        records, copied = {}, {}
        for name in owner.TARGETS:
            exported = name if name in primary else family + '--' + name
            copied[name] = directory / name
            exporter(paths[exported], copied[name])
            records[name] = receipt['artifacts'][exported]
        family_receipt = {**identity, **spec['receipt_fields'], 'status': 'built',
                          'architecture': 'amd64', 'commands': receipt['commands'],
                          'artifacts': records, 'action_audit': receipt['action_audit'],
                          'resolution': resolved, 'review_registry_revision': plan['catalog_revision'],
                          'invocation': {'bazel': bazel, 'review_registry_revision': plan['catalog_revision']},
                          'base_layout': layouts[spec['parent']], 'cohort_collection': True,
                          'final_profile': COMPONENT_PROFILE}
        family_receipt['layouts'] = ({target: layouts[target] for target in
            [spec['parent'], *[spec['prefix'] + name for name in spec['images']]]}
            if spec['full_label_layouts'] else
            {name: layouts[spec['prefix'] + name] for name in spec['images']})
        # Preserve genuine common collection records for the existing owner
        # validators/incremental checks; never invent per-family event records.
        for name in ('build-events.jsonl', 'build.execution.json'):
            exporter(private / name, directory / name)
        if family == 'dash-engine':
            counts = owner.cache_reuse.collect(directory / 'build.execution.json',
                directory / 'build-events.jsonl', owner.TARGETS[owner.IMAGES[0] + '.gz'], allow_empty=True)
            family_receipt['foreign_diagnostics'] = owner.foreign_diagnostics(
                directory / 'build.execution.json', counts,
                {name: records[name]['sha256'] for name in owner.FOREIGN_PAYLOADS})
        (directory / 'build-receipt.json').write_text(json.dumps(family_receipt, indent=2) + '\n')
        if family == 'swss-layer':
            anchor = logs.execute([bazel, 'cquery', *OPTIONS, '--output=files',
                '@sonic_swss//fpmsyncd:bgp_eoiu_marker.py'], directory, receipt,
                'swss-layer.source', cwd=source).splitlines()
            require(len(anchor) == 1, 'Different SWSS source output count')
            execution_root = (source / 'bazel-out').resolve(strict=True).parent
            swss_source = (execution_root / anchor[0]).resolve(strict=True).parents[1]
            layout = owner.oci_layout.validate_layout(source / layouts[owner.IMAGE], 'linux/amd64')
            parent = owner.oci_layout.validate_layout(source / layouts[BASE], 'linux/amd64')
            # Match the original owner report; diagnostics remain in build-receipt.json.
            value = {**owner.verify(layout, parent, copied, swss_source), **identity}
            (directory / 'validation.json').write_text(json.dumps(value, indent=2) + '\n')
        else:
            owner.validate_images(directory)
        validation = directory / 'validation.json'
        value = json.loads(validation.read_bytes())
        require(value.get('status') == 'passed' and value.get('revision') == plan['source_revision'],
                'Component source image validation did not pass')
        public_json(value)
        validations[family] = {'bytes': validation.stat().st_size, 'sha256': check.sha(validation)}
        public_receipt = {key: family_receipt[key] for key in (
            'status', 'revision', 'architecture', 'artifacts', 'review_registry_revision',
            'tested_revision_parents', 'image_version', 'include_system_eventd', 'dialout_enabled')
            if key in family_receipt}
        if family == 'swss-layer':
            public_receipt['status'] = value['status']
        public_json(public_receipt)
        (directory / 'public-receipt.json').write_text(json.dumps(public_receipt, indent=2) + '\n')
    owner_checks = component_owner_checks(source, private, receipt, bazel, logs, audit, exporter, check, public_json, paths)
    for family, owner in components.items():
        if family == 'swss-layer':
            continue
        directory = private / family
        owner.incremental(argparse.Namespace(bazel=bazel), directory)
        record = directory / 'incremental.json'
        require(record.is_file() and not record.is_symlink(), 'Missing existing incremental result')
        value = json.loads(record.read_bytes())
        if family in ('database', 'eventd', 'mux', 'radv', 'gnmi', 'lldp', 'mgmt-framework'):
            require(all(key in value for key in ('baseline', 'noop', 'edit', 'restore')) and
                    value['baseline']['sha256'] == value['noop']['sha256'] ==
                    value['restore']['sha256'], 'Component incremental restoration differs')
        else:
            require(all(key in value for key in ('baseline', 'unchanged', 'changed', 'restored')) and
                    value['baseline'] == value['unchanged'] == value['restored'],
                    'Component incremental restoration differs')
        if family not in ('database', 'eventd', 'mux', 'radv', 'gnmi'):
            require(value.get('status') == 'passed', 'Component incremental check did not pass')
        public_json(value)
        incremental[family] = {'bytes': record.stat().st_size, 'sha256': check.sha(record)}
    return validations, incremental, owner_checks


def prepare_component_heap(plan, source, scratch, worker, preflight):
    """Reallocate this cohort's measured profile through the original launcher."""
    allocation = {'jvm_heap_gib': 6, 'scheduler_memory_mb': 4000}
    require(plan.get('component_refresh') is True and
            plan.get('component_heap_recovery') == allocation,
            'Different fixed component heap recovery')
    resources_path = scratch / 'resources.json'
    launcher = scratch / 'native-bazel'
    originals = (scratch / 'resources.original.json', scratch / 'native-bazel.original')
    pending = (scratch / 'resources.heap-recovery.json', scratch / 'native-bazel.heap-recovery')
    require(all(not os.path.lexists(path) for path in (*originals, *pending)),
            'Component heap recovery outputs must be fresh')
    require(all(path.is_file() and not path.is_symlink() and
                0 < path.stat().st_size <= 128 * 1024
                for path in (resources_path, launcher)),
            'Prepared component resource or launcher file differs')
    resource_bytes, launcher_bytes = resources_path.read_bytes(), launcher.read_bytes()
    resources = json.loads(resource_bytes)
    memory, used, quota = (resources[key] for key in ('memory_max', 'memory_current', 'cpu_limit'))
    require(type(memory) is int and memory >= 20 * 1024**3 and
            type(used) is int and 0 <= used < memory and
            type(quota) in (int, float) and 4 <= quota <= 6 and
            resources.get('architecture') == 'amd64' and
            resources.get('disk_cache') is False and resources.get('repository_cache') is False,
            'Component heap recovery requires the admitted worker resources')
    original = preflight.select_build_profile(memory, quota)
    require(resources.get('owner_profile') == original and
            resources.get('selected_profile') == original and
            original['jobs'] == 2 and original['cpu'] == 2 and
            original['jvm_heap_gib'] == 3 and original['scheduler_memory_mb'] == 8000,
            'Different original component resource profile')
    selected = dict(original, **allocation)
    # prepare_cache already verified ownership. Recheck its immutable binary
    # seal and the complete rendered launcher before changing either input.
    executable = shutil.which('bazel', path='/usr/local/bin:/usr/bin:/bin')
    if executable is None:
        executable = shutil.which('bazelisk', path='/usr/local/bin:/usr/bin:/bin')
    require(executable is not None, 'Prepared Bazel launcher unavailable')
    binary = Path(executable).resolve(strict=True)
    require(binary.is_file() and binary.stat().st_size <= 128 * 1024**2 and
            hashlib.sha256(binary.read_bytes()).hexdigest() == plan['bazel_launcher_sha256'],
            'Prepared Bazel launcher seal differs')
    # This existing owner reads public managed settings; it performs no probe.
    configuration = worker.remote_cache_configuration(source)
    expected = worker.launcher_text(str(binary), scratch / 'output-base', original, configuration).encode()
    require(launcher_bytes == expected, 'Prepared native launcher differs')
    replacement = worker.launcher_text(str(binary), scratch / 'output-base', selected, configuration).encode()
    resources['selected_profile'] = selected
    resources['component_heap_recovery'] = {
        'allocation': allocation,
        'original_resources_sha256': hashlib.sha256(resource_bytes).hexdigest(),
        'original_launcher_sha256': hashlib.sha256(launcher_bytes).hexdigest(),
        'selected_launcher_sha256': hashlib.sha256(replacement).hexdigest(),
        'measured_peak_guarantee': False,
    }
    updated = (json.dumps(resources, sort_keys=True) + '\n').encode()
    for path, raw, mode in ((originals[0], resource_bytes, 0o600),
                            (originals[1], launcher_bytes, 0o700),
                            (pending[0], updated, 0o600), (pending[1], replacement, 0o700)):
        with path.open('xb') as stream:
            stream.write(raw)
        path.chmod(mode)
    # No Bazel command runs until this cache-preparation phase succeeds.
    os.replace(pending[1], launcher)
    os.replace(pending[0], resources_path)


def observe_component_heap(plan, source, scratch, directory, receipt, logs):
    """Observe the final launcher's JVM limit; do not infer peak memory use."""
    allocation = {'jvm_heap_gib': 6, 'scheduler_memory_mb': 4000}
    require(plan.get('component_refresh') is True and
            plan.get('component_heap_recovery') == allocation,
            'Different fixed component heap observation')
    observation = scratch / 'jvm-heap-observation.json'
    require(not os.path.lexists(observation), 'Heap observation must be fresh')
    proxy, launcher = scratch / 'review-bazel', scratch / 'native-bazel'
    paths = (proxy, launcher, scratch / 'native-bazel.original',
             scratch / 'resources.json', scratch / 'resources.original.json')
    require(all(path.is_file() and not path.is_symlink() and
                0 < path.stat().st_size <= 128 * 1024 for path in paths),
            'Heap observation inputs must be the prepared regular files')
    require(proxy.read_text() == component_proxy_text(source, scratch) and
            os.environ.get('BAZEL_REAL') == str(launcher) and
            os.environ.get('USE_BAZEL_VERSION') == '8.5.1',
            'Heap observation must use the final component launcher')
    resources = json.loads((scratch / 'resources.json').read_bytes())
    selected, recovery = resources['selected_profile'], resources['component_heap_recovery']
    require(all(selected.get(key) == value for key, value in
                dict(allocation, jobs=2, cpu=2).items()) and
            recovery['allocation'] == allocation,
            'Different selected heap observation profile')
    launcher_sha = hashlib.sha256(launcher.read_bytes()).hexdigest()
    original_sha = hashlib.sha256((scratch / 'native-bazel.original').read_bytes()).hexdigest()
    require(launcher_sha == recovery['selected_launcher_sha256'] and
            original_sha == recovery['original_launcher_sha256'] and
            hashlib.sha256((scratch / 'resources.original.json').read_bytes()).hexdigest()
            == recovery['original_resources_sha256'],
            'Heap observation launcher or original resource seal differs')
    raw = logs.execute([str(proxy), 'info', 'max-heap-size'], directory, receipt,
                       'cohort.max-heap', cwd=source)
    require(isinstance(raw, str), 'Different max-heap-size output type')
    text = raw.strip()
    observed = text if re.fullmatch(r'(0|[1-9][0-9]{0,8})MB', text) else None
    requested = allocation['jvm_heap_gib'] * 1024**3
    # Bazel8.5.1 StringUtilities.prettyPrintBytes truncates decimal MB here.
    expected = str(requested // 1000000) + 'MB'
    units = int(observed[:-2]) if observed is not None else None
    result = {'status': 'passed' if observed == expected else 'failed',
              'source_revision': plan['source_revision'],
              'requested_heap_bytes': requested, 'expected_max_heap_size': expected,
              'observed_max_heap_size': observed,
              'observed_bytes_lower_bound': None if units is None else units * 1000000,
              'observed_bytes_upper_bound_exclusive': None if units is None else (units + 1) * 1000000,
              'original_output_bytes': len(raw.encode()),
              'original_output_sha256': hashlib.sha256(raw.encode()).hexdigest(),
              'selected_launcher_sha256': launcher_sha,
              'original_launcher_sha256': original_sha,
              'proxy_sha256': hashlib.sha256(proxy.read_bytes()).hexdigest(),
              'bazel_version': '8.5.1', 'measured_peak_guarantee': False,
              'scope': 'Same final launcher JVM limit; decimal-rounded info value, not peak usage.'}
    with observation.open('x') as stream:
        json.dump(result, stream, sort_keys=True)
        stream.write('\n')
    observation.chmod(0o600)
    require(observed == expected,
            'Effective JVM heap differs: expected ' + expected + ', observed ' + str(observed))
    return result


def prepare_component_gnoi(source, directory, receipt, logs):
    """Initialize only the source checkout's pinned Sysmgr proto submodule."""
    relative = 'src/sonic-sysmgr/gnoi'
    protos = ('types/types.proto', 'common/common.proto', 'system/system.proto')
    tree = logs.execute(['git', '-c', 'protocol.allow=never', 'ls-tree', 'HEAD', '--', relative],
                        directory, receipt, 'gnoi.gitlink', cwd=source).strip()
    matched = re.fullmatch(r'160000 commit ([0-9a-f]{40})\t' + re.escape(relative), tree)
    require(matched is not None, 'Sysmgr gNOI input is not a unique pinned gitlink')
    revision = matched.group(1)
    # This is the existing normal-CI setup command, scoped to its one gitlink.
    logs.execute(['git', '-c', 'safe.directory=' + str(source), 'submodule', 'update', '--init',
                  '--', relative], directory, receipt, 'gnoi.initialize', cwd=source)
    submodule = source / relative
    require(submodule.is_dir() and submodule.resolve(strict=True) == submodule,
            'Sysmgr gNOI checkout must be a real source directory')
    git = ['git', '-c', 'protocol.allow=never', '-C', str(submodule)]
    head = logs.execute([*git, 'rev-parse', 'HEAD'], directory, receipt, 'gnoi.revision',
                        cwd=source).strip()
    require(head == revision, 'Sysmgr gNOI checkout differs from its source gitlink')
    rows = logs.execute([*git, 'ls-tree', '-r', 'HEAD', '--', *protos], directory, receipt,
                        'gnoi.proto-tree', cwd=source).splitlines()
    blobs = {}
    for row in rows:
        item = re.fullmatch(r'100644 blob ([0-9a-f]{40})\t(.+)', row)
        require(item is not None and item.group(2) in protos and item.group(2) not in blobs,
                'Sysmgr gNOI proto declaration differs')
        blobs[item.group(2)] = item.group(1)
    require(set(blobs) == set(protos), 'Sysmgr gNOI proto declarations are incomplete')
    logs.execute([*git, 'diff', '--no-ext-diff', '--no-textconv', '--exit-code',
                  '--ignore-submodules=none', 'HEAD'], directory,
                 receipt, 'gnoi.clean', cwd=source)
    files = {}
    for name in protos:
        path = submodule / name
        require(path.is_file() and not path.is_symlink() and path.resolve(strict=True) == path,
                'Sysmgr gNOI proto is missing or has a different file type: ' + name)
        raw = path.read_bytes()
        require(raw and hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
                == blobs[name], 'Sysmgr gNOI proto differs from its pinned source: ' + name)
        files[name] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                       'git_blob': blobs[name]}
    return {'path': relative, 'revision': revision, 'files': files}


def build_cohort(plan, source, scratch, output, owners, components=None):
    orch, syncd, audit, builder, logs, exporter, resolution, merge, public_json = owners
    environment_check(plan, scratch, source)
    if components is not None:
        proxy = scratch / 'review-bazel'
        require(proxy.is_file() and not proxy.is_symlink() and
                proxy.read_text() == component_proxy_text(source, scratch),
                'Different fixed component profile proxy')
    images = {'swss': orch, 'syncd': syncd}
    exports, layouts = declarations(plan, images, components)
    private = scratch / 'cohort-build'
    private.mkdir(mode=0o700)
    receipt = {'schema': 1, 'status': 'preparing', 'commands': [],
               'source_revision': plan['source_revision'], 'catalog_revision': plan['catalog_revision'],
               'policy_targets': POLICIES, 'options': OPTIONS}
    try:
        require(orch.identity(private) == plan['source_revision'], 'Orchagent source revision differs')
        syncd.clean_source(private)
        identity = syncd.identity()
        require(identity['revision'] == plan['source_revision'], 'Syncd source revision differs')
        if components is not None:
            receipt['gnoi_source'] = prepare_component_gnoi(source, private, receipt, logs)
            receipt['effective_heap'] = observe_component_heap(
                plan, source, scratch, private, receipt, logs)
        logs.execute(setup.MAKE_ARGUMENTS, private, receipt, 'swss.manifests', cwd=source)
        manifests = manifest_snapshot(source)
        logs.execute(syncd.manifest_command(), private, receipt, 'syncd.manifests', cwd=source)
        require(manifest_snapshot(source) == manifests, 'Shared Make manifests changed between owners')
        if components is not None:
            for family in components:
                logs.execute(COMPONENTS[family]['manifest_command'], private, receipt,
                             family + '.manifests', cwd=source)
                require(manifest_snapshot(source) == manifests,
                        'Shared Make manifests changed for component ' + family)
        receipt['shared_manifests'] = manifests
        receipt['status'] = 'auditing'
        bazel = str(scratch / 'review-bazel')
        receipt['status'] = 'building'
        if components is not None:
            check_base_parent(plan, source, private, receipt, bazel, audit, builder, logs,
                              orch.oci_layout.validate_layout, orch.check)
            paths = collect_with_native_anchor(plan, source, private, receipt, exports, layouts,
                                               bazel, audit, builder, orch.check, merge)
        else:
            receipt['action_audit'] = audit.audit_targets(
                bazel, OPTIONS, [*exports.values(), *layouts],
                workspace=source, output=private / 'action-audit.json')
            paths = builder.collect_archives(source, private, receipt, exports, bazel=bazel,
                                             options=[*OPTIONS, '--keep_going'], additional_targets=layouts)
        require(set(paths) == set(exports), 'Incomplete cohort exports')
        receipt['status'] = 'binding'
        actual, policy_ids = bind_policies(paths, receipt, plan, orch.check, merge)
        layout_paths = {}
        for target in layouts:
            values = logs.execute([bazel, 'cquery', *OPTIONS, '--output=files', target],
                                  private, receipt, 'cohort.layout', cwd=source).splitlines()
            require(len(values) == 1, 'Different layout output count')
            layout_paths[target] = values[0]
        resolved = resolution.collect(source, private, bazel=[bazel])
        receipt['status'] = 'validating'
        validations = {}
        for family, owner in images.items():
            directory = private / family
            directory.mkdir(mode=0o700)
            records = {}
            for name in owner.TARGETS:
                exported = name if name in PRIMARY else family + '--' + name
                exporter(paths[exported], directory / name)
                records[name] = receipt['artifacts'][exported]
            # This is a view of this successful collection, not an old producer receipt.
            family_receipt = {**identity, 'status': 'built', 'architecture': 'amd64',
                              'commands': receipt['commands'], 'artifacts': records,
                              'action_audit': receipt['action_audit'], 'resolution': resolved,
                              'review_registry_revision': plan['catalog_revision'],
                              'invocation': {'bazel': bazel, 'review_registry_revision': plan['catalog_revision']},
                              'base_layout': layout_paths[BASE],
                              'layouts': {name: layout_paths[owner.OWNER + name] for name in owner.IMAGES},
                              'cohort_collection': True}
            (directory / 'build-receipt.json').write_text(json.dumps(family_receipt, indent=2) + '\n')
            owner.validate_images(directory)
            validation = directory / 'validation.json'
            value = json.loads(validation.read_bytes())
            require(value.get('status') == 'passed' and value.get('revision') == plan['source_revision'],
                    'Source image validation did not pass')
            public_json(value)
            # Reuse the existing workflow's public-receipt projection. Private
            # command lines and local layout paths remain in private scratch.
            public_receipt = {key: family_receipt[key] for key in (
                'status', 'revision', 'architecture', 'artifacts',
                'review_registry_revision', 'tested_revision_parents') if key in family_receipt}
            public_json(public_receipt)
            (directory / 'public-receipt.json').write_text(json.dumps(public_receipt, indent=2) + '\n')
            validations[family] = {'bytes': validation.stat().st_size, 'sha256': orch.check.sha(validation)}
        incremental = {}
        if components is not None:
            extra, incremental, owner_checks = validate_components(plan, source, private, receipt, paths,
                layout_paths, identity, resolved, components, bazel, logs, exporter, orch.check, public_json, audit)
            validations.update(extra)
            actual.update(bind_component_archives(paths, receipt, orch.check))
            # Recollect the actual native outputs after all existing incremental
            # work, instead of checking only copies made before those calls.
            final = private / 'native-parent-final'
            final.mkdir(mode=0o700)
            final_targets = {name: target for name, target in PRIMARY.items()
                             if name[:-3] in plan['expected_native_archives']}
            final_receipt = {'commands': []}
            final_paths = builder.collect_archives(source, final, final_receipt, final_targets,
                bazel=bazel, options=OPTIONS, additional_targets=[BASE])
            for name, path in final_paths.items():
                expected = plan['expected_native_archives'][name[:-3]]
                require(path.stat().st_size == expected['bytes'] and orch.check.sha(path) == expected['sha256'],
                        'Native archive changed after existing component incremental checks: ' + name)
            receipt['native_parent_final'] = final_receipt
        require(orch.identity(private) == plan['source_revision'], 'Orchagent source changed')
        syncd.clean_source(private)
        require(syncd.identity() == identity, 'Syncd source identity changed')
        require(manifest_snapshot(source) == manifests, 'Shared Make manifests changed after build')
        output.mkdir(mode=0o700)
        archives, reports = output / 'archives', output / 'reports'
        archives.mkdir(mode=0o700)
        reports.mkdir(mode=0o700)
        primary = {**PRIMARY, **(component_primary() if components is not None else {})}
        for name in primary:
            copied = (archives if name.endswith('.gz') else reports) / name
            exporter(paths[name], copied)
            expected = receipt['artifacts'][name]
            require(copied.is_file() and not copied.is_symlink() and
                    copied.stat().st_size == expected['bytes'] and orch.check.sha(copied) == expected['sha256'],
                    'Published cohort copy differs')
        for family in [*images, *(components or {})]:
            for name in ('validation.json', 'public-receipt.json'):
                exporter(private / family / name, reports / (family + '-' + name))
        for family in incremental:
            exporter(private / family / 'incremental.json', reports / (family + '-incremental.json'))
        if components is not None:
            owner_report_files = publish_component_owner_reports(private, reports, owner_checks,
                exporter, orch.check, public_json)
        public_json(json.loads((private / 'action-audit.json').read_bytes()))
        exporter(private / 'action-audit.json', reports / 'action-audit.json')
        result = {'schema': 1, 'status': 'COHERENT_POLICIES_AND_SOURCE_IMAGES_VALIDATED',
                  'source_revision': plan['source_revision'], 'source_head': plan['source_head'],
                  'source_tree': plan['source_tree'], 'catalog_revision': plan['catalog_revision'],
                  'native_cache_controller': plan['native_revision'], 'archives': actual,
                  'policies': policy_ids, 'source_validations': validations, 'shared_manifests': manifests,
                  'accepted_native_archive_equality': True, 'native_acceptance': False, 'VS_acceptance': False,
                  'operator': {key: os.environ.get(key) for key in
                               ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_SHA',
                                'GITHUB_JOB', 'GITHUB_WORKFLOW_REF', 'GITHUB_EVENT_NAME')}}
        if components is not None:
            result.update(component_profile=COMPONENT_PROFILE, component_incremental=incremental,
                          native_parent_preflight=receipt['native_parent_preflight'],
                          base_parent_preflight=receipt['base_parent_preflight'],
                          component_loaded_runtime='PENDING separate unchanged runtime owner gates',
                          component_owner_tests=owner_checks, component_owner_reports=owner_report_files)
        public_json(result)
        (reports / 'cohort.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
        receipt['status'] = 'passed'
        return result
    finally:
        (private / 'private-build-receipt.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')


# Inert fixed callback fragment; Source owns integration and parent/root lifecycle.
# Existing loaded CLIs and inline assertions/container options are preserved.
# Parent must prepare each runtime output directory empty and worker-owned.
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

_COMPONENT_RUNTIME_FIXED = {'dash-engine': {'directory': 'dash-engine',
                 'images': ['docker-dash-engine', 'docker-dash-engine-dbg'],
                 'aggregate': True,
                 'module': 'tools/bazel/ci/dash_engine_loaded.py',
                 'owner_sha256': '4f52ef698a352de568cbcf2ff9e05917506bd1afef650e4a474a7108dc5ff843',
                 'per_image_validation': True},
 'dash-ha': {'directory': 'dash-ha',
             'images': ['docker-dash-ha', 'docker-dash-ha-dbg'],
             'aggregate': False,
             'module': 'tools/bazel/ci/dash_ha_loaded.py',
             'owner_sha256': 'f12f261c64077ffbdfbb68b709e40cf005d73ec08b1f7397291b7ab737b162ac',
             'per_image_validation': True},
 'database': {'directory': 'database',
              'images': ['docker-database', 'docker-database-dbg'],
              'aggregate': True,
              'module': 'tools/bazel/ci/database_loaded.py',
              'owner_sha256': '1b2379c121af52d9a1290c78790e54a58fabda0a45a56a541d069f6b8c59326d',
              'per_image_validation': False},
 'eventd': {'directory': 'eventd',
            'images': ['docker-eventd', 'docker-eventd-dbg'],
            'aggregate': True,
            'module': 'tools/bazel/ci/eventd_loaded.py',
            'owner_sha256': '1405920ea898468a1e8a019295d5ac5b8e8f2bb14d70c386fb9fd21e786badee',
            'per_image_validation': False},
 'frr': {'directory': 'frr',
         'images': ['docker-fpm-frr', 'docker-fpm-frr-dbg'],
         'aggregate': False,
         'per_image_validation': True},
 'gnmi': {'directory': 'gnmi',
          'images': ['docker-sonic-gnmi', 'docker-sonic-gnmi-dbg'],
          'aggregate': True,
          'module': 'tools/bazel/ci/gnmi_loaded.py',
          'owner_sha256': 'a931c4a8171feff6889c76b6e558872aadd8b251b7007e3eb0ecd267141b53c1',
          'per_image_validation': False},
 'lldp': {'directory': 'lldp',
          'images': ['docker-lldp', 'docker-lldp-dbg'],
          'aggregate': True,
          'module': 'tools/bazel/ci/lldp_loaded.py',
          'owner_sha256': '337c291651213270cf8710ca38f627ad39c195e664753abc2dfb61fc10b98993',
          'per_image_validation': False},
 'macsec': {'directory': 'macsec',
            'images': ['docker-macsec', 'docker-macsec-dbg'],
            'aggregate': False,
            'module': 'tools/bazel/ci/macsec_loaded.py',
            'owner_sha256': 'ac7fbe91567445a926c61eaefcc3bd2a3b438bf7ea8915241262d824cc9c9879',
            'per_image_validation': True},
 'mgmt-framework': {'directory': 'mgmt-framework',
                    'images': ['docker-sonic-mgmt-framework', 'docker-sonic-mgmt-framework-dbg'],
                    'aggregate': True,
                    'module': 'tools/bazel/ci/mgmt_framework_loaded.py',
                    'owner_sha256': 'beb68754fcd79ac7a6e126b34f39e27696bba109b88f975b5bc30c96aae8798e',
                    'per_image_validation': False},
 'mux': {'directory': 'mux',
         'images': ['docker-mux', 'docker-mux-dbg'],
         'aggregate': True,
         'module': 'tools/bazel/ci/mux_loaded.py',
         'owner_sha256': '788ab7cb7a2f1e7741083801473f308b172ca8918a6dfb071087a92d89020160',
         'per_image_validation': False},
 'otel': {'directory': 'otel',
          'images': ['docker-sonic-otel', 'docker-sonic-otel-dbg'],
          'aggregate': False,
          'module': 'tools/bazel/ci/otel_loaded.py',
          'owner_sha256': '4ecb5c56739936c3feb2d222f76dc7e4aa0662a648c810baa7c9a34da809d0a2',
          'per_image_validation': True},
 'radv': {'directory': 'radv',
          'images': ['docker-router-advertiser', 'docker-router-advertiser-dbg'],
          'aggregate': True,
          'module': 'tools/bazel/ci/radv_loaded.py',
          'owner_sha256': '522aa0a81d2c9805385363539884ba46628a5aef50732c63e8090baf96f89438',
          'per_image_validation': False},
 'sflow': {'directory': 'sflow',
           'images': ['docker-sflow', 'docker-sflow-dbg'],
           'aggregate': False,
           'module': 'tools/bazel/ci/sflow_loaded.py',
           'owner_sha256': 'e72c477f3b3c5a4b47dd530733424d78fffc9087b4c8ce78568f0761b740d18c',
           'per_image_validation': True},
 'snmp': {'directory': 'snmp',
          'images': ['docker-snmp', 'docker-snmp-dbg'],
          'aggregate': False,
          'per_image_validation': True},
 'swss-layer': {'directory': 'swss-layer',
                'images': ['docker-swss-layer-trixie'],
                'aggregate': False,
                'per_image_validation': False},
 'sysmgr': {'directory': 'sysmgr',
            'images': ['docker-sysmgr', 'docker-sysmgr-dbg'],
            'aggregate': False,
            'module': 'tools/bazel/ci/sysmgr_loaded.py',
            'owner_sha256': 'a79e58e3193cbdc534f0752dcde9ec2f582d837b066295703c44f4e8482bce3a',
            'per_image_validation': True},
 'teamd': {'directory': 'teamd',
           'images': ['docker-teamd', 'docker-teamd-dbg'],
           'aggregate': False,
           'per_image_validation': True},
 'orchagent': {'directory': 'swss',
               'images': ['docker-orchagent', 'docker-orchagent-dbg'],
               'aggregate': False,
               'module': 'tools/bazel/ci/orchagent_loaded.py',
               'owner_sha256': '306696e234fbec8e0b40d2d8bc5a32c3b84dab0e8996dd112e4c340b96b85133',
               'per_image_validation': True},
 'base': {'directory': 'base',
          'images': ['docker-base-trixie', 'docker-config-engine-trixie'],
          'aggregate': False,
          'source_owners': {'tools/bazel/ci/validate_runtime.py': 'b38453b8d06d53ce628f0ca8b304a8bbd6ec46e7fca183caf4d71ba4e6f532fe',
                            'tools/bazel/oci/base_runtime_smoke.py': 'e4992da6711b44c6a2c80baa1b9209b2c834763c0e84a0a851e692b56b9ebf38'},
          'per_image_validation': False}}

_COMPONENT_RUNTIME_INLINE = {
    'frr': r'''python3 - <<'PY'
import hashlib,json,subprocess
from pathlib import Path
from tools.bazel.ci.validate_runtime import load_archive, verify_loaded_image
directory=Path('__ARTIFACTS__')
receipt=json.loads((directory/'validation.json').read_text())
assert receipt['status']=='passed' and receipt['architecture']=='amd64'
assert receipt['revision']==subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
for name in ['docker-fpm-frr','docker-fpm-frr-dbg']:
    path=directory/(name+'.gz')
    assert hashlib.sha256(path.read_bytes()).hexdigest()==receipt['artifacts'][path.name]['sha256']
    expected=json.loads((directory/(name+'.validation.json')).read_text())
    tag='sonic-frr-validation-'+name+':latest'
    actual,config,_=load_archive(path,tag,expected['config_digest'])
    verify_loaded_image(actual,config,expected['config_digest'])
    assert actual['Os']=='linux' and actual['Architecture']=='amd64'
    assert actual['RootFS']['Layers']==expected['rootfs']['diff_ids']
assert hashlib.sha256((directory/'test-redis.tar').read_bytes()).hexdigest()==receipt['artifacts']['test-redis.tar']['sha256']
PY
mkdir -p __ARTIFACTS__/redis-fixture
tar --no-same-owner -xf __ARTIFACTS__/test-redis.tar -C __ARTIFACTS__/redis-fixture

set -euo pipefail
for image in docker-fpm-frr docker-fpm-frr-dbg; do
  probe_args=()
  if [[ "$image" == *-dbg ]]; then probe_args+=(--debug); fi
  docker run --rm --network=none --cap-drop=ALL --security-opt=no-new-privileges \
    --read-only --user=300:300 --group-add=102 --pids-limit=128 --memory=4g --cpus=2 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m \
    --mount "type=bind,src=$PWD/dockers/docker-fpm-frr/bazel/runtime_probe.py,dst=/probe/runtime_probe.py,readonly" \
    --mount "type=bind,src=__ARTIFACTS__/redis-fixture,dst=/probe/redis,readonly" \
    --mount "type=bind,src=__ARTIFACTS__/validation.json,dst=/probe/validation.json,readonly" \
    --entrypoint /usr/bin/python3 "sonic-frr-validation-$image:latest" -B /probe/runtime_probe.py --validation /probe/validation.json "${probe_args[@]}" \
    | tee "__ARTIFACTS__/$image.runtime.json"
done
''',
    'snmp': r'''python3 - <<'PY'
import hashlib,json,subprocess
from pathlib import Path
directory=Path('__ARTIFACTS__')
receipt=json.loads((directory/'validation.json').read_text())
assert receipt['status']=='passed' and receipt['architecture']=='amd64'
assert receipt['revision']==subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
for name in ['docker-snmp','docker-snmp-dbg']:
    path=directory/(name+'.gz')
    assert hashlib.sha256(path.read_bytes()).hexdigest()==receipt['artifacts'][path.name]['sha256']
    subprocess.run(['docker','load','--input',str(path)],check=True)
    actual=json.loads(subprocess.check_output(['docker','image','inspect',name+':latest']))[0]
    expected=json.loads((directory/(name+'.validation.json')).read_text())
    assert actual['Os']=='linux' and actual['Architecture']=='amd64'
    assert actual['RootFS']['Layers']==expected['rootfs']['diff_ids']
    meaningful=lambda values:{k:v for k,v in values.items() if v not in (None,'',[],{},False)}
    assert meaningful(actual['Config'])==meaningful(expected['config'])
assert hashlib.sha256((directory/'test-redis.tar').read_bytes()).hexdigest()==receipt['artifacts']['test-redis.tar']['sha256']
PY
mkdir -p __ARTIFACTS__/redis-fixture
tar --no-same-owner -xf __ARTIFACTS__/test-redis.tar -C __ARTIFACTS__/redis-fixture

set -euo pipefail
for image in docker-snmp docker-snmp-dbg; do
  docker run --rm --network=none --cap-drop=ALL --security-opt=no-new-privileges \
    --read-only --user=101:102 --pids-limit=128 --memory=4g --cpus=2 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m \
    --mount "type=bind,src=$PWD/dockers/docker-snmp/bazel/runtime_probe.py,dst=/probe/runtime_probe.py,readonly" \
    --mount "type=bind,src=__ARTIFACTS__/redis-fixture,dst=/probe/redis,readonly" \
    --entrypoint /usr/bin/python3 "$image:latest" -B /probe/runtime_probe.py \
    | tee "__ARTIFACTS__/$image.runtime.json"
done
''',
    'teamd': r'''python3 - <<'PY'
import hashlib,json,subprocess
from pathlib import Path
from tools.bazel.ci.validate_runtime import load_archive, verify_loaded_image
directory=Path('__ARTIFACTS__')
receipt=json.loads((directory/'validation.json').read_text())
assert receipt['status']=='passed' and receipt['architecture']=='amd64'
assert receipt['revision']==subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
for name in ['docker-teamd','docker-teamd-dbg']:
    path=directory/(name+'.gz')
    assert hashlib.sha256(path.read_bytes()).hexdigest()==receipt['artifacts'][path.name]['sha256']
    expected=json.loads((directory/(name+'.validation.json')).read_text())
    tag='sonic-teamd-validation-'+name+':latest'
    actual,config,_=load_archive(path,tag,expected['config_digest'])
    verify_loaded_image(actual,config,expected['config_digest'])
    assert actual['Os']=='linux' and actual['Architecture']=='amd64'
    assert actual['RootFS']['Layers']==expected['rootfs']['diff_ids']
assert hashlib.sha256((directory/'test-redis.tar').read_bytes()).hexdigest()==receipt['artifacts']['test-redis.tar']['sha256']
PY
mkdir -p __ARTIFACTS__/redis-fixture
tar --no-same-owner -xf __ARTIFACTS__/test-redis.tar -C __ARTIFACTS__/redis-fixture

set -euo pipefail
for image in docker-teamd docker-teamd-dbg; do
  probe_args=()
  if [[ "$image" == *-dbg ]]; then probe_args+=(--debug); fi
  # teamdctl resolves the interface through PF_PACKET/SOCK_DGRAM;
  # that read-only ioctl socket requires NET_RAW as well as NET_ADMIN.
  docker run --rm --network=none --cap-drop=ALL --cap-add=NET_ADMIN --cap-add=NET_RAW --security-opt=no-new-privileges \
    --read-only --pids-limit=128 --memory=4g --cpus=2 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m \
    --tmpfs /run/teamd:rw,nosuid,nodev,noexec,size=16m \
    --tmpfs /var/warmboot:rw,nosuid,nodev,noexec,size=16m \
    --mount type=volume,dst=/run/redis \
    --mount "type=bind,src=$PWD/dockers/docker-teamd/bazel/runtime_probe.py,dst=/probe/runtime_probe.py,readonly" \
    --mount "type=bind,src=$PWD/tools/bazel/oci/runtime_smoke.py,dst=/probe/runtime_smoke.py,readonly" \
    --mount "type=bind,src=__ARTIFACTS__/redis-fixture,dst=/probe/redis,readonly" \
    --mount "type=bind,src=__ARTIFACTS__/validation.json,dst=/probe/validation.json,readonly" \
    --entrypoint /usr/bin/python3 "sonic-teamd-validation-$image:latest" -B /probe/runtime_probe.py --validation /probe/validation.json "${probe_args[@]}" \
    | tee "__ARTIFACTS__/$image.runtime.json"
done
''',
    'swss-layer': r'''python3 - <<'PY'
import hashlib, json, subprocess
from pathlib import Path
from tools.bazel.ci.validate_runtime import load_archive, verify_loaded_image
root = Path('__ARTIFACTS__')
receipt = json.loads((root / 'public-receipt.json').read_text())
assert receipt['status'] == 'passed' and receipt['architecture'] == 'amd64'
assert receipt['revision'] == subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
name = 'docker-swss-layer-trixie.gz'
assert hashlib.sha256((root / name).read_bytes()).hexdigest() == receipt['artifacts'][name]['sha256']
report = json.loads((root / 'validation.json').read_text())
actual, config, _ = load_archive(root / name, report['archive']['tag'], report['config_digest'])
verify_loaded_image(actual, config, report['config_digest'])
assert actual['Architecture'] == report['architecture'] == 'amd64'
assert actual['RootFS']['Layers'] == report['rootfs']['diff_ids']
PY

set -o pipefail
docker run --rm --network=none --cap-drop=ALL --security-opt=no-new-privileges \
  --read-only --pids-limit=128 --memory=4g --cpus=2 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
  --mount "type=bind,src=$PWD/tools/bazel/oci/swss_layer_runtime.py,dst=/probe/swss_layer_runtime.py,readonly" \
  --mount "type=bind,src=$PWD/tools/bazel/oci/runtime_smoke.py,dst=/probe/runtime_smoke.py,readonly" \
  --mount "type=bind,src=__ARTIFACTS__/validation.json,dst=/probe/validation.json,readonly" \
  --entrypoint /usr/bin/python3 docker-swss-layer-trixie:latest \
  -B /probe/swss_layer_runtime.py /probe/validation.json | tee __RUNTIME_REPORT__
''',
    'base': r'''python3 - <<'PY'
import hashlib, json, os, subprocess
from pathlib import Path
directory = Path('__ARTIFACTS__')
receipt = json.loads((directory / 'receipt.json').read_text())
assert receipt['status'] == 'passed' and receipt['architecture'] == os.environ['EXPECTED_ARCH']
assert receipt['revision'] == subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
assert hashlib.sha256((directory / 'installed-images-policy.json').read_bytes()).hexdigest() == receipt['installed_images_policy_sha256']
for name in ('docker-base-trixie.gz', 'docker-config-engine-trixie.gz'):
    with (directory / name).open('rb') as archive:
        actual = hashlib.file_digest(archive, 'sha256').hexdigest()
    assert actual == receipt['artifacts'][name]['sha256'], name
PY
python3 -B tools/bazel/ci/validate_runtime.py \
  --images-policy __ARTIFACTS__/installed-images-policy.json \
  --target __ARTIFACTS__ \
  --output __ARTIFACTS__/installed-images.json
set -o pipefail
docker run --rm --network=none --cap-drop=ALL --security-opt=no-new-privileges \
  --read-only --pids-limit=128 --memory=4g --cpus=2 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \
  --mount "type=bind,src=$PWD/tools/bazel/oci/base_runtime_smoke.py,dst=/probe/smoke.py,readonly" \
  --mount "type=bind,src=__ARTIFACTS__/docker-config-engine-trixie.validation.json,dst=/probe/validation.json,readonly" \
  --entrypoint /usr/bin/python3 docker-config-engine-trixie:latest \
  -B /probe/smoke.py /probe/validation.json | tee __ARTIFACTS__/runtime-smoke.json
''',
}

_COMPONENT_RUNTIME_INLINE_BINDINGS = {'frr': {'workflow': '/home/dev-user/code/sonic-public/docker-migration-plan-pr64-20261102/implementation/final23-integration/ci-reconcile-20261113T063055Z/current-pr206/all-workflows/bazel-frr-image.yml',
         'workflow_sha256': 'da9dca87f541dd5bd67a0771b00e249508651f9b3805dd3c042e701c59908a1d',
         'load_block_sha256': 'ad316d97df04c79b4f423cae062cbd85c570b65d15692363d9c9719570dafa65',
         'probe_block_sha256': '706c1f1a2ebe22b3d4e16f46d041442db14d61261a94dbb7156bbcb74808c129'},
 'snmp': {'workflow': '/home/dev-user/code/sonic-public/docker-migration-plan-pr64-20261102/implementation/final23-integration/ci-reconcile-20261113T063055Z/current-pr206/all-workflows/bazel-snmp.yml',
          'workflow_sha256': '942b787644481b949642568686fbc1c23d1297957d1c3773d65ec03114fa8670',
          'load_block_sha256': '911155ce10ab6da92915eefad0bd97cb18c95e466b419f3f57420cfb7640f04b',
          'probe_block_sha256': '17e9164bf528bb8293e6242565434fb3b5cb100c94b5e47799319d9737bcb21b'},
 'teamd': {'workflow': '/home/dev-user/code/sonic-public/docker-migration-plan-pr64-20261102/implementation/final23-integration/ci-reconcile-20261113T063055Z/current-pr206/.github/workflows/bazel-oci.yml',
           'workflow_sha256': '4a67f4a31997434f272738c8343f79cddddb39d3027a7435bb083ac60688f13d',
           'load_block_sha256': '55496b41ad07b9cff654382c7c3a0ef6a65b2e31821b134ce60fd3c6da59f486',
           'probe_block_sha256': '80bd37ae85a4a89b3216c21d4c0408640380c71b580c0f4796970ef15cd67204',
           'kernel_module_step_not_included': True},
 'swss-layer': {'workflow': '/home/dev-user/code/sonic-public/docker-migration-plan-pr64-20261102/implementation/final23-integration/ci-reconcile-20261113T063055Z/current-pr206/.github/workflows/bazel-oci.yml',
                'workflow_sha256': '4a67f4a31997434f272738c8343f79cddddb39d3027a7435bb083ac60688f13d',
                'load_block_sha256': '5947d820f640961aa09c33f31e9bed8b484727e2fcb65dfe1021b4003314bc30',
                'probe_block_sha256': 'de8e0052cc985f867f7d7e5f73870be327ddd3707854990f1984ff519e89e3c9'},
 'base': {'workflow': '/home/dev-user/code/sonic-public/docker-migration-plan-pr64-20261102/implementation/final23-integration/ci-reconcile-20261113T063055Z/current-pr206/all-workflows/bazel-base-images.yml',
          'workflow_sha256': 'b970b0a0686a17161727081ec1dfd42b0608fe7936040b0db36e8d0406bbd522',
          'load_block_sha256': 'fb0bd44a4944a7c368d2d22a2286e0ed39136e33a852b9d2482e00ee5a3d01b1',
          'probe_block_sha256': 'abdc799975ecb5c64f5920e51cbb16f28af0ba158d02ed40bfcb979faa48be35',
          'fixed_environment': {'EXPECTED_ARCH': 'amd64'}}}

def component_workload(source, private, family, docker, client_env, paths, runtime_owner):
    """Run one fixed current-source runtime group inside the existing private daemon."""
    require = runtime_owner.require
    require(family in _COMPONENT_RUNTIME_FIXED, 'Different component runtime family')
    require(client_env.get('HOME') == os.environ.get('HOME') and
            ('HOME' in client_env) == ('HOME' in os.environ), 'Component HOME differs')
    require('PYTHONOPTIMIZE' not in client_env and not sys.flags.optimize,
            'Original runtime assertions must remain enabled')
    source, private = Path(source).resolve(strict=True), Path(private).resolve(strict=True)
    require(source.is_dir() and private.is_dir(), 'Missing reviewed runtime roots')
    selected = shutil.which('docker', path=client_env['PATH'])
    require(selected is not None and Path(selected).resolve(strict=True) ==
            Path(docker).resolve(strict=True), 'Different private Docker client')
    spec = _COMPONENT_RUNTIME_FIXED[family]
    directory = private / spec['directory']
    output = directory / 'runtime'
    require(directory.is_dir() and not directory.is_symlink() and output.is_dir() and
            not output.is_symlink() and not any(output.iterdir()), 'Runtime output is not empty')
    require(output.stat().st_uid == 1000 and output.stat().st_gid == 1000,
            'Runtime output is not worker-owned')
    # These are fixed job paths inserted into original shell/Python literals.
    for path in (source, private, directory, output):
        require(re.fullmatch(r'/[A-Za-z0-9._/+-]+', str(path)) is not None,
                'Unsupported fixed runtime path')
    revision = 'c02d93956919e4ea6886f347be4ac0ab6e3e36f0'

    def sha(path):
        require(path.is_file() and not path.is_symlink(), 'Missing regular runtime input')
        before = runtime_owner.file_identity(path.stat())
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        require(runtime_owner.file_identity(path.stat()) == before, 'Runtime input changed while reading')
        return {'bytes': path.stat().st_size, 'sha256': digest.hexdigest()}

    def read_report(path):
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 4 * 1024 * 1024,
                'Missing or oversized runtime JSON')
        value = json.loads(path.read_bytes())
        require(type(value) is dict, 'Different runtime JSON shape')
        return value

    guarded, archive_records = {}, {}

    def bind_inputs(folder, images, shared=False, individual=False):
        build_path, validation_path = folder / 'build-receipt.json', folder / 'validation.json'
        build, validation = read_report(build_path), read_report(validation_path)
        require(build['status'] == 'built' and validation['status'] == 'passed' and
                build['revision'] == validation['revision'] == revision and
                validation['architecture'] == 'amd64', 'Different validated runtime source')
        guarded[build_path] = sha(build_path)
        guarded[validation_path] = sha(validation_path)
        for name in images:
            path = folder / (name + '.gz')
            expected = build['artifacts'][path.name]
            actual = sha(path)
            require(actual == {'bytes': expected['bytes'], 'sha256': expected['sha256']},
                    'Runtime archive differs from the original build')
            guarded[path] = actual
            archive_records[name] = actual
            if individual:
                original = folder / (name + '.validation.json')
                require(read_report(original)['status'] == 'passed', 'Image validation did not pass')
                guarded[original] = sha(original)
        if shared:
            public_path = folder / 'public-receipt.json'
            public = read_report(public_path)
            require(public['status'] == 'passed' and public['architecture'] == 'amd64' and
                    public['revision'] == revision, 'SWSS runtime requires its passed public receipt')
            guarded[public_path] = sha(public_path)

    if family == 'base':
        receipt_path = directory / 'receipt.json'
        receipt = read_report(receipt_path)
        require(receipt['status'] == 'passed' and receipt['architecture'] == 'amd64' and
                receipt['revision'] == revision, 'Different original base receipt')
        guarded[receipt_path] = sha(receipt_path)
        policy_path = directory / 'installed-images-policy.json'
        policy = read_report(policy_path)
        guarded[policy_path] = sha(policy_path)
        require(guarded[policy_path]['sha256'] == receipt['installed_images_policy_sha256'],
                'Original base installed policy differs')
        require(policy['architecture'] == 'amd64' and
                {item['name'] for item in policy['images']} == set(spec['images']) and
                len(policy['images']) == 2, 'Different original two-image base policy')
        for name in spec['images']:
            archive = directory / (name + '.gz')
            expected = receipt['artifacts'][archive.name]
            actual = sha(archive)
            require(actual == {'bytes': expected['bytes'], 'sha256': expected['sha256']},
                    'Runtime archive differs from the original base build')
            guarded[archive] = archive_records[name] = actual
            validation_path = directory / (name + '.validation.json')
            detail = read_report(validation_path)
            require(detail['manifest_digest'] == receipt['images'][name]['manifest_digest'],
                    'Detailed base validation differs from its original receipt')
            guarded[validation_path] = sha(validation_path)
        for relative, expected in spec['source_owners'].items():
            owner = source / relative
            require(sha(owner)['sha256'] == expected, 'Different original base runtime owner')
    else:
        bind_inputs(directory, spec['images'], family == 'swss-layer', spec['per_image_validation'])
    report_paths = {name + '.runtime.json': directory / (name + '.runtime.json')
                    for name in spec['images']} if family != 'swss-layer' else {
                        'runtime-smoke.json': directory / 'runtime-smoke.json'}
    if family == 'base':
        report_paths = {name: directory / name for name in
                        ('installed-images.json', 'runtime-smoke.json')}
    if spec['aggregate']:
        report_paths['loaded-validation.json'] = directory / 'loaded-validation.json'
    dependency_report = None
    if family in ('sflow', 'orchagent'):
        shared = private / 'swss-layer'
        require(shared.is_dir() and not shared.is_symlink(), 'Missing original SWSS dependency')
        bind_inputs(shared, ['docker-swss-layer-trixie'], True)
        dependency_report = directory / 'swss-layer.runtime-smoke.json'
        report_paths[dependency_report.name] = dependency_report
    for path in report_paths.values():
        require(not path.exists() and not path.is_symlink(), 'Original runtime report already exists')
    if family == 'teamd':
        require(all(Path('/sys/module', name).is_dir() for name in ('team', 'team_mode_loadbalance')),
                'Existing Teamd kernel modules are required; prerequisite remains pending')

    def inline(kind, folder, report, label):
        script = _COMPONENT_RUNTIME_INLINE[kind].replace('__ARTIFACTS__', str(folder))
        if kind == 'swss-layer':
            script = script.replace('__RUNTIME_REPORT__', str(report))
        runtime_owner.bounded(['/bin/bash', '-euo', 'pipefail', '-c', script], paths['state'],
                              label, 1200, environment={**client_env, **(
                                  {'EXPECTED_ARCH': 'amd64'} if kind == 'base' else {})},
                              worker_user=True,
                              working_directory=str(source))

    if dependency_report is not None:
        inline('swss-layer', private / 'swss-layer', dependency_report,
               'component-' + family + '-swss')
        require(read_report(dependency_report).get('status') == 'passed',
                'Original SWSS runtime dependency did not pass')
    if 'module' in spec:
        owner = source / spec['module']
        require(sha(owner)['sha256'] == spec['owner_sha256'], 'Different loaded runtime owner')
        runtime_owner.bounded([sys.executable, '-B', str(owner), '--artifacts', str(directory)],
                              paths['state'], 'component-' + family, 1200,
                              environment=dict(client_env), worker_user=True,
                              working_directory=str(source))
    else:
        inline(family, directory, report_paths.get('runtime-smoke.json'), 'component-' + family)
    for path, expected in guarded.items():
        require(sha(path) == expected, 'Validated input changed during original runtime checks')

    aggregate = None
    if spec['aggregate']:
        aggregate = read_report(directory / 'loaded-validation.json')
        require(aggregate.get('status') == 'passed' and aggregate.get('revision') == revision and
                set(aggregate.get('images', {})) == set(spec['images']),
                'Loaded aggregate differs from the original source/images')
    results, copies = {}, {}
    for name, path in report_paths.items():
        value = read_report(path)
        if not (family == 'database' and name.endswith('.runtime.json')):
            require(value.get('status') == 'passed', 'Original runtime check did not pass')
        if name.endswith('.runtime.json') and 'module' in spec:
            image = name.removesuffix('.runtime.json')
            require(value.get('archive_sha256') == archive_records[image]['sha256'],
                    'Runtime report differs from the validated archive')
            if aggregate is not None:
                require(aggregate['images'][image] == value,
                        'Per-image runtime report differs from the original aggregate')
            else:
                require(value.get('revision') == revision,
                        'Runtime report differs from the validated source')
        if family == 'base' and name == 'installed-images.json':
            require(value.get('architecture') == 'amd64' and
                    set(value.get('images', {})) == set(spec['images']) and
                    all(value['images'][image].get('archive_sha256') ==
                        archive_records[image]['sha256'] for image in spec['images']),
                    'Installed base report differs from its original archives')
        if name == 'loaded-validation.json':
            require(value.get('revision') == revision, 'Loaded report source differs')
        record = sha(path)
        destination = output / name
        with destination.open('xb') as stream:
            stream.write(path.read_bytes())
            os.fchmod(stream.fileno(), 0o644)
            os.fchown(stream.fileno(), 1000, 1000)
        require(sha(destination) == record and sha(path) == record, 'Runtime report copy changed')
        results[name] = value
        copies[name] = {'original_path': str(path), 'path': str(destination), **record}
    return {'status': 'passed', 'scope': 'original component runtime checks', 'family': family,
            'revision': revision, 'archives': archive_records, 'reports': copies,
            'original_owner_results': results, 'shared_swss_dependency': dependency_report is not None,
            'native_acceptance_claimed': False, 'full_vs_admission_claimed': False}


def component_runtime_families():
    return ('base', 'swss-layer', 'orchagent',
            *sorted(name for name in COMPONENTS if name != 'swss-layer'))


def component_runtime_paths(runtime, row, temporary, family):
    require(family in component_runtime_families(), 'Different fixed runtime group')
    paths = runtime.state_paths(row, temporary)
    suffix = '-component-' + family
    paths['state'] = paths['state'].with_name(paths['state'].name + suffix)
    paths['exec'] = paths['exec'].with_name(paths['exec'].name + suffix)
    paths['docker_data'] = paths['state'] / ('source-image-docker-' + row['run'] + '-' + row['attempt'])
    paths['component_runtime'] = True
    return paths


def stage_component_runtime(plan, source, native, scratch, control):
    root = scratch / 'runtime-control'
    require(not root.exists() and not root.is_symlink(), 'Preserve existing runtime controls')
    root.mkdir(mode=0o700)
    for name, expected in plan['runtime_control_files'].items():
        require(expected['origin'] in ('source', 'native', 'control'), 'Different runtime owner domain')
        source_name = expected.get('source_path', name)
        component_owner = name == 'tools/bazel/ci/vs_runtime_check.py'
        required_source = 'tools/bazel/ci/vs_component_runtime_owner.py' if component_owner else name
        require(source_name == required_source and (not component_owner or expected['origin'] == 'control'),
                'Different runtime control source path')
        original = {'source': source, 'native': native, 'control': control}[expected['origin']] / source_name
        require(
                original.is_file() and not original.is_symlink(), 'Different runtime control source')
        raw = original.read_bytes()
        require(len(raw) == expected['bytes'] and hashlib.sha256(raw).hexdigest() == expected['sha256'],
                'Runtime control source seal differs')
        copied = root / name
        copied.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with copied.open('xb') as stream:
            stream.write(raw)
    return root


def load_component_runtime(plan, root):
    require(not any(name == 'tools' or name.startswith('tools.') for name in sys.modules),
            'Runtime owner requires a separate process')
    for name, expected in plan['runtime_control_files'].items():
        path = root / name
        require(path.is_file() and not path.is_symlink(), 'Missing runtime control')
        raw = path.read_bytes()
        require(len(raw) == expected['bytes'] and hashlib.sha256(raw).hexdigest() == expected['sha256'],
                'Runtime control seal differs')
    spec = importlib.util.spec_from_file_location('cohort_private_runtime',
        root / 'tools/bazel/ci/vs_runtime_check.py')
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    for name, value in tuple(sys.modules.items()):
        if name.startswith('tools.') and getattr(value, '__file__', None):
            require(Path(value.__file__).resolve().is_relative_to(root), 'Mixed runtime controls')
    return owner


def component_root_command(context_path, context_sha256, home_present):
    command = ['sudo', '-n', '--preserve-env=HOME'] if home_present else ['sudo', '-n']
    if not home_present:
        command.extend(['/usr/bin/env', '-u', 'HOME'])
    return [*command, sys.executable, '-I', '-S', '-B', str(Path(__file__).resolve()),
            'runtime-root', '--runtime-context', str(context_path),
            '--runtime-context-sha256', context_sha256]


def component_runtime_root(context_path, expected_sha256):
    require(os.geteuid() == 0 and context_path.is_file() and not context_path.is_symlink(),
            'Different component supervisor scope')
    raw = context_path.read_bytes()
    require(len(raw) <= 64 * 1024 and hashlib.sha256(raw).hexdigest() == expected_sha256,
            'Component supervisor context changed')
    context = json.loads(raw)
    require(context['schema'] == 1 and context['family'] in component_runtime_families(),
            'Different component supervisor context')
    plan_path = Path(context['plan_path'])
    require(plan_path.is_file() and not plan_path.is_symlink() and
            hashlib.sha256(plan_path.read_bytes()).hexdigest() == context['plan_sha256'],
            'Component runtime plan changed')
    plan = load_plan(plan_path)
    require(context['source_revision'] == plan['source_revision'] and
            context['source_tree'] == plan['source_tree'] and plan.get('component_refresh') is True,
            'Component runtime source differs')
    require(('HOME' in os.environ) == context['home_present'] and
            os.environ.get('HOME') == context['invoking_home'], 'Supervisor reset invoking HOME')
    root = Path(context['runtime_control'])
    runtime = load_component_runtime(plan, root)
    paths = component_runtime_paths(runtime, context['identity'], context['temporary'], context['family'])
    require(str(paths['state']) == context['state'] and str(paths['exec']) == context['exec'],
            'Component supervisor paths differ')
    def workload(docker, client_env, actual_paths):
        return component_workload(Path(context['source']), Path(context['private']),
            context['family'], docker, client_env, actual_paths, runtime)
    result = runtime.private_daemon(paths, component_workload=workload)
    print(json.dumps(result, sort_keys=True))
    return 0  # The parent retains failure and verifies the original cleanup result.


def public_component_runtime_result(result):
    public = {key: result[key] for key in ('schema', 'status', 'source_revision',
              'source_tree', 'operator', 'cohort_sha256', 'host_kernel_configuration_changed',
              'full_vs_admission_claimed')}
    public['archives'] = {name: {key: value[key] for key in ('bytes', 'sha256')}
                          for name, value in result['archives'].items()}
    public['groups'] = {}
    for family, value in result['groups'].items():
        if value['status'] == 'pending':
            public['groups'][family] = dict(value)
            continue
        callback = value.get('component_runtime', {})
        public['groups'][family] = {
            'status': value['status'], 'revision': callback.get('revision'),
            'daemon_started': value.get('daemon_started', False),
            'daemon_closed': value.get('daemon_closed', False),
            'private_data_removed': value.get('native_stop', {}).get('private_data_removed', False),
            'archives': {name: {key: item[key] for key in ('bytes', 'sha256')}
                         for name, item in callback.get('archives', {}).items()},
            'reports': {name: {key: item[key] for key in ('bytes', 'sha256')}
                        for name, item in callback.get('reports', {}).items()},
        }
    public['pending_runtime_groups'] = result.get('pending_runtime_groups', [])
    return public


def run_component_runtimes(plan, plan_path, source, native, scratch, output, control):
    import signal
    import time
    require(os.getuid() == os.geteuid() == os.getgid() == os.getegid() == 1000,
            'Component runtime must start as the worker user')
    setup.source_check(source, plan['source_revision'])
    original = output / 'reports/cohort.json'
    require(original.is_file() and not original.is_symlink(), 'Missing original cohort build result')
    cohort_raw = original.read_bytes()
    cohort = json.loads(cohort_raw)
    require(cohort['status'] == 'COHERENT_POLICIES_AND_SOURCE_IMAGES_VALIDATED' and
            cohort['source_revision'] == plan['source_revision'] and
            cohort['source_tree'] == plan['source_tree'] and
            cohort['accepted_native_archive_equality'] is True,
            'Component runtimes require the genuine completed source gates')
    result_path = output / 'reports/component-runtime.json'
    require(not result_path.exists() and not result_path.is_symlink(), 'Preserve original runtime result')
    setup.source_check(native, plan['native_revision'])
    runtime_root = stage_component_runtime(plan, source, native, scratch, control)
    cleanup = runtime_root.with_name(runtime_root.name + '-native-cleanup')
    setup.source_check(cleanup, plan['runtime_cleanup_revision'])
    runtime = load_component_runtime(plan, runtime_root)
    public_json = importlib.import_module('tools.bazel.ci.public_artifacts').safe_json
    private_result_path = scratch / 'component-runtime-private.json'
    require(not private_result_path.exists() and not private_result_path.is_symlink(),
            'Preserve original private supervision result')
    runtime.checked_helper(cleanup / 'tools/bazel/ci/source_image_worker.py', runtime.NATIVE_WORKER_SHA256)
    row = runtime.identity(os.environ['RUNNER_NAME'], os.environ)
    require(cohort['operator']['GITHUB_RUN_ID'] == row['run'] and
            cohort['operator']['GITHUB_RUN_ATTEMPT'] == row['attempt'] and
            cohort['operator']['GITHUB_SHA'] == row['controller'], 'Runtime operator differs from build')
    observed = runtime.inspection.collect(row['worker'])
    runtime.reviewed_observation(observed)
    private = scratch / 'cohort-build'
    result = {'schema': 1, 'status': 'failed', 'source_revision': plan['source_revision'],
              'source_tree': plan['source_tree'], 'operator': row,
              'cohort_sha256': hashlib.sha256(cohort_raw).hexdigest(),
              'archives': cohort['archives'], 'groups': {}, 'prerequisites': observed,
              'host_kernel_configuration_changed': False, 'full_vs_admission_claimed': False}
    deadline = time.monotonic() + 90 * 60
    def interrupted(signum, frame):
        raise runtime.ProbeFailure('Component runtime interrupted')
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for family in component_runtime_families():
            if family == 'teamd' and not all(Path('/sys/module', name).is_dir()
                                             for name in ('team', 'team_mode_loadbalance')):
                result['groups'][family] = {'status': 'pending', 'original_runtime_executed': False,
                    'reason': 'required existing kernel modules unavailable',
                    'required_modules': ['team', 'team_mode_loadbalance']}
                continue
            require(deadline - time.monotonic() >= 1650,
                    'Insufficient remaining runtime budget for original body and cleanup')
            paths = component_runtime_paths(runtime, row, os.environ['RUNNER_TEMP'], family)
            for path in (paths['state'], paths['exec']):
                runtime.no_links(path)
                require(not path.exists() and not path.is_symlink(), 'Preserve existing runtime group')
            paths['state'].mkdir(mode=0o700)
            directory = private / ('swss' if family == 'orchagent' else family)
            require(directory.is_dir() and not directory.is_symlink(), 'Missing original family inputs')
            (directory / 'runtime').mkdir(mode=0o700)
            context = {'schema': 1, 'family': family, 'identity': row,
                'source_revision': plan['source_revision'], 'source_tree': plan['source_tree'],
                'source': str(source), 'private': str(private), 'runtime_control': str(runtime_root),
                'plan_path': str(plan_path), 'plan_sha256': hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                'state': str(paths['state']), 'exec': str(paths['exec']),
                'temporary': os.environ['RUNNER_TEMP'],
                'home_present': 'HOME' in os.environ, 'invoking_home': os.environ.get('HOME')}
            context_path = paths['state'] / 'context.json'
            with context_path.open('x') as stream:
                json.dump(context, stream, sort_keys=True)
            data = runtime.bounded(component_root_command(context_path,
                hashlib.sha256(context_path.read_bytes()).hexdigest(), context['home_present']),
                paths['state'], 'component-supervisor', 1650, environment=dict(os.environ), root_supervisor=True)
            value = json.loads(data)
            result['groups'][family] = value
            require(value['status'] == 'passed' and value['daemon_started'] is True and
                    value['daemon_closed'] is True and value['container_workload_executed'] is True and
                    value['native_stop']['status'] == 'passed' and
                    value['native_stop']['private_data_removed'] is True and
                    not paths['docker_data'].exists(), 'Original component runtime or private cleanup failed')
            for name, identity in value['component_runtime']['reports'].items():
                copied = Path(identity['path'])
                require(copied.is_file() and not copied.is_symlink() and
                        copied.stat().st_size == identity['bytes'] and
                        hashlib.sha256(copied.read_bytes()).hexdigest() == identity['sha256'],
                        'Original runtime report changed')
                require(re.fullmatch(r'[A-Za-z0-9._-]+\.json', name) is not None,
                        'Different original runtime report name')
                public_json(json.loads(copied.read_bytes()))
                destination = output / 'reports/runtime' / family / name
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with destination.open('xb') as stream:
                    stream.write(copied.read_bytes())
        setup.source_check(source, plan['source_revision'])
        require(original.read_bytes() == cohort_raw, 'Original cohort build result changed')
        pending = [name for name, value in result['groups'].items() if value['status'] == 'pending']
        require(not pending or pending == ['teamd'], 'Unexpected pending runtime groups')
        result['status'] = 'pending' if pending else 'passed'
        result['pending_runtime_groups'] = pending
    except BaseException as error:
        result['failure'] = runtime.safe_failure(error)
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        with private_result_path.open('x') as stream:
            json.dump(result, stream, indent=2, sort_keys=True)
            stream.write('\n')
        public = public_component_runtime_result(result)
        public_json(public)
        with result_path.open('x') as stream:
            json.dump(public, stream, indent=2, sort_keys=True)
            stream.write('\n')
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('cache', 'build', 'runtime', 'runtime-root'))
    names = ('plan', 'source', 'native', 'scratch', 'output', 'archives', 'mirrors')
    for name in names:
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--control', type=Path)
    parser.add_argument('--runtime-context', type=Path)
    parser.add_argument('--runtime-context-sha256')
    args = parser.parse_args()
    if args.phase == 'runtime-root':
        require(args.runtime_context is not None and args.runtime_context_sha256 is not None and
                re.fullmatch(r'[0-9a-f]{64}', args.runtime_context_sha256) is not None and
                all(getattr(args, name) is None for name in names) and args.control is None,
                'Root runtime phase accepts only its sealed context')
        return component_runtime_root(args.runtime_context, args.runtime_context_sha256)
    require(all(getattr(args, name) is not None for name in names) and
            args.runtime_context is None and args.runtime_context_sha256 is None,
            'Worker phase requires the original source/control arguments')
    require((args.phase == 'runtime') == (args.control is not None),
            'Runtime control root must be explicit only for runtime phase')
    plan = load_plan(args.plan)
    source, native, scratch = (p.resolve(strict=True) for p in (args.source, args.native, args.scratch))
    require(len({source, native, scratch}) == 3 and not any(a.is_relative_to(b)
            for a in (source, native, scratch) for b in (source, native, scratch) if a != b),
            'Source and private scratch must be separate')
    environment_check(plan, scratch, source)
    if args.phase == 'runtime':
        require(plan.get('component_refresh') is True, 'Different component runtime plan')
        run_component_runtimes(plan, args.plan.resolve(strict=True), source, native, scratch,
                               args.output.resolve(strict=True), args.control.resolve(strict=True))
        return 0
    if args.phase == 'cache':
        setup.prepare_cache(plan, source, native, scratch,
                            args.archives.resolve(strict=True), args.mirrors.resolve(strict=True))
        # prepare_cache has verified and imported this immutable native owner.
        validation_tools = scratch / 'validation-tools'
        validation_tools.mkdir(mode=0o700)
        importlib.import_module('tools.bazel.ci.source_image_worker').prepare_validation_tools(validation_tools)
        if plan.get('component_refresh'):
            prepare_component_heap(plan, source, scratch,
                importlib.import_module('tools.bazel.ci.source_image_worker'),
                importlib.import_module('tools.bazel.ci.source_worker_preflight'))
            prepare_component_proxy(source, scratch)
    else:
        setup.source_check(source, plan['source_revision'])
        orch = setup.import_from(source, 'tools.bazel.ci.orchagent_image')
        modules = [importlib.import_module('tools.bazel.ci.' + name) for name in
                   ('syncd_image', 'bazel_commands', 'build', 'command_log', 'resolution', 'validate_runtime')]
        syncd, audit, builder, logs, resolution, runtime = modules
        exporter = importlib.import_module('tools.bazel.build_helpers').export_archive
        public_json = importlib.import_module('tools.bazel.ci.public_artifacts').safe_json
        components = None
        if plan.get('component_refresh'):
            components = {}
            for family, spec in COMPONENTS.items():
                path = source / 'tools/bazel/ci' / (spec['module'] + '.py')
                require(path.is_file() and not path.is_symlink() and
                        hashlib.sha256(path.read_bytes()).hexdigest() == spec['owner_sha256'],
                        'Component source owner seal differs')
                components[family] = importlib.import_module('tools.bazel.ci.' + spec['module'])
        build_cohort(plan, source, scratch, args.output,
                     (orch, syncd, audit, builder, logs, exporter, resolution, runtime.merge_images_policies, public_json),
                     components)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
