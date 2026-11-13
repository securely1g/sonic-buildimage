"""Real tiny TAR inventories; no network, image execution or source builds."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.bazel.ci import observe_final_native_layer11 as subject
from tools.bazel.ci import recover_vs_original_inputs as original
from tools.bazel.oci import image_validation as checks
from tools.bazel.ci import observe_original_archive as metadata_owner


class OriginalLayerInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def layer(self, filename, *, mtime=7, order=('one','two'), body=b'original payload', pax=None):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w', format=tarfile.PAX_FORMAT) as archive:
            for name in order:
                info = tarfile.TarInfo('usr/lib/python/'+name)
                info.size=len(body); info.mode=0o644; info.uid=0; info.gid=0
                info.mtime=mtime; info.uname='root'; info.gname='root'
                info.pax_headers={} if pax is None else dict(pax)
                archive.addfile(info,io.BytesIO(body))
        path=self.root/filename
        path.write_bytes(gzip.compress(data.getvalue(),mtime=0))
        return SimpleNamespace(layers=[self.root/'not-selected']*11+[path])

    def inventory(self, view, **kwargs):
        return subject.layer_inventory(view, checks, original, tar_info=metadata_owner.ObservationTarInfo, **kwargs)[0]

    def test_only_selected_layer_is_read_with_real_existing_inventory_owner(self):
        value=self.inventory(self.layer('one.gz'))
        self.assertEqual(set(value['files']),{'usr/lib/python/one','usr/lib/python/two'})
        self.assertEqual(value['files']['usr/lib/python/one']['sha256'],hashlib.sha256(b'original payload').hexdigest())
        self.assertEqual([x['order']for x in value['headers']],[0,1])
        self.assertNotIn('original payload',json.dumps(value))

    def test_timestamp_only_change_is_visible_when_file_hashes_match(self):
        left=self.inventory(self.layer('left.gz',mtime=7));right=self.inventory(self.layer('right.gz',mtime=8))
        self.assertEqual(left['files'],right['files'])
        self.assertNotEqual(left['ordered_headers_sha256'],right['ordered_headers_sha256'])
        rows=subject.differences({x['normalized_name']:x for x in left['headers']},
                                {x['normalized_name']:x for x in right['headers']})['changed']
        self.assertEqual(len(rows),2)
        self.assertTrue(all(row['left']['mtime']==7 and row['right']['mtime']==8 for row in rows))

    def test_member_order_change_is_visible_when_effective_files_match(self):
        left=self.inventory(self.layer('left.gz'));right=self.inventory(self.layer('right.gz',order=('two','one')))
        self.assertEqual(left['files'],right['files'])
        self.assertNotEqual(left['ordered_headers_sha256'],right['ordered_headers_sha256'])
        self.assertEqual(left['headers'][0]['normalized_name'],'usr/lib/python/one')
        self.assertEqual(right['headers'][0]['normalized_name'],'usr/lib/python/two')

    def test_changed_payload_is_reported_as_hash_without_contents(self):
        left=self.inventory(self.layer('left.gz'));right=self.inventory(self.layer('right.gz',body=b'changed private fixture'))
        result=subject.differences(left['files'],right['files'])
        self.assertEqual(len(result['changed']),2)
        self.assertNotIn('changed private fixture',json.dumps(result))

    def test_pax_value_change_is_retained_only_as_digest(self):
        left=self.inventory(self.layer('left.gz',pax={'fixture.note':'first-value'}))
        right=self.inventory(self.layer('right.gz',pax={'fixture.note':'second-value'}))
        self.assertEqual(left['files'],right['files'])
        self.assertNotEqual(left['headers'][0]['pax_ordered_sha256'],right['headers'][0]['pax_ordered_sha256'])
        self.assertNotIn('second-value',json.dumps(right))

    def test_repeated_inner_entries_retain_each_occurrence_and_effective_last_entry(self):
        value=self.inventory(self.layer('repeated.gz',order=('one','one')))
        self.assertEqual(list(value['files']),['usr/lib/python/one'])
        self.assertEqual(len(value['headers']),2)
        self.assertEqual([row['occurrence']for row in value['headers']],[0,1])
        self.assertEqual([row['order']for row in value['headers']],[0,1])
        keys=[subject.encoded([row['normalized_name'],row['occurrence']])for row in value['headers']]
        self.assertEqual(len(set(keys)),2)

    def test_oversized_pax_is_rejected_before_inventory_or_payload_allocation(self):
        info=tarfile.TarInfo('fixed-extension');info.type=tarfile.XHDTYPE
        info.size=metadata_owner.MAX_JSON+1
        path=self.root/'oversized.gz';path.write_bytes(gzip.compress(info.tobuf(format=tarfile.GNU_FORMAT),mtime=0))
        with mock.patch.object(checks,'apply_layer') as inventory:
            with self.assertRaisesRegex(ValueError,'extended TAR metadata exceeds'):
                self.inventory(SimpleNamespace(layers=[self.root/'unused']*11+[path]))
            inventory.assert_not_called()

    def test_sparse_header_is_rejected_before_inventory(self):
        info=tarfile.TarInfo('fixed-sparse');info.type=tarfile.GNUTYPE_SPARSE
        path=self.root/'sparse.gz';path.write_bytes(gzip.compress(info.tobuf(format=tarfile.GNU_FORMAT),mtime=0))
        with mock.patch.object(checks,'apply_layer') as inventory:
            with self.assertRaisesRegex(ValueError,'sparse TAR metadata'):
                self.inventory(SimpleNamespace(layers=[self.root/'unused']*11+[path]))
            inventory.assert_not_called()

    def test_missing_selected_layer_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'layer11 is absent'):
            self.inventory(SimpleNamespace(layers=[]))

    def test_count_and_json_budgets_are_enforced(self):
        view=self.layer('input.gz')
        with self.assertRaisesRegex(ValueError,'exceeds bound'):self.inventory(view,maximum_headers=1)
        with self.assertRaisesRegex(ValueError,'exceeds byte bound'):self.inventory(view,maximum_bytes=1)
        with mock.patch.object(subject,'MAX_REPORT',16):
            with self.assertRaisesRegex(ValueError,'Difference report exceeds bound'):
                subject.differences({'original':{'sha256':'a'*64}},{'original':{'sha256':'b'*64}})

    def test_wrong_fixed_diff_id_stops_before_archive_and_restores_timer(self):
        library=mock.Mock();library.original_proof.return_value={'rootfs':{'diff_ids':['actual']*12}}
        plan={'inventory_deadline_seconds':10,'images':[{'name':'fixture','expected_diff_id':'other'}]}
        report={'images':[]}; prior=signal.getsignal(signal.SIGALRM)
        with self.assertRaisesRegex(ValueError,'differs from frozen validation'):
            subject.inspect_two(plan,self.root,self.root,self.root,self.root,
                                (original,None,checks),library,report)
        library.inspect_archive.assert_not_called()
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))
        self.assertEqual(signal.getsignal(signal.SIGALRM),prior)

    def test_actual_operator_is_preserved_without_credentials_or_event_rewrite(self):
        values={'GITHUB_REPOSITORY':'securely1g/sonic-buildimage','GITHUB_SHA':'a'*40,
                'GITHUB_RUN_ID':'1','GITHUB_RUN_ATTEMPT':'2','GITHUB_WORKFLOW_REF':'fixed/manual',
                'GITHUB_JOB':'actual_metadata_job','GITHUB_EVENT_NAME':'workflow_dispatch','GH_TOKEN':'fixture-secret'}
        with mock.patch.dict(os.environ,values,clear=True):
            result=subject.operator_identity()
            self.assertEqual(result['GITHUB_SHA'],'a'*40)
            self.assertNotIn('GH_TOKEN',result)
            os.environ['GITHUB_EVENT_NAME']='pull_request'
            with self.assertRaisesRegex(ValueError,'Unexpected audit operator'):subject.operator_identity()
            self.assertEqual(os.environ['GITHUB_EVENT_NAME'],'pull_request')

    def test_changed_source_owner_is_rejected_without_importing_it(self):
        path=self.root/'tools/bazel/ci/fixed.py';path.parent.mkdir(parents=True)
        path.write_bytes(b'not executed')
        with self.assertRaisesRegex(ValueError,'Fixed source owner differs'):
            subject.verify_sources(self.root,{'tools/bazel/ci/fixed.py':{'bytes':12,'sha256':'0'*64}},original)
        self.assertEqual(path.read_bytes(),b'not executed')


if __name__ == '__main__':
    unittest.main()
