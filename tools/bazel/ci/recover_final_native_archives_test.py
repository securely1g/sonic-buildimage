"""Small mocked ranges; no live network, image data or package generation."""
from email.message import Message
import hashlib
import io
from pathlib import Path
import signal
import struct
import tempfile
import time
import unittest
from unittest import mock
import urllib.error
import zlib

import recover_final_native_archives as subject
import recover_vs_original_inputs as original


def headers(**values):
    result = Message()
    for key, value in values.items():
        result[key.replace('_', '-')] = str(value)
    return result


class Response:
    def __init__(self, raw, start, total):
        self.raw = io.BytesIO(raw)
        self.status = 206
        self.headers = headers(Content_Range=f'bytes {start}-{start+len(raw)-1}/{total}', Content_Length=len(raw))
        self.reads, self.closed = [], False

    def read(self, count):
        self.reads.append(count)
        return self.raw.read(count)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Opener:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def limits():
    return {'deadline': time.monotonic()+30, 'requests': 0, 'maximum_requests': 10,
            'hosting_declared': 0, 'maximum_hosting_bytes': 200000}


def fixture(name='docker-syncd-vs.gz', payload=b'original archive bytes'):
    artifact_id = subject.FIXED[name][0]
    row = {'name': name, 'bytes': len(payload), 'compressed_bytes': len(payload),
           'sha256': hashlib.sha256(payload).hexdigest(), 'crc32': zlib.crc32(payload),
           'method': 0, 'flags': 8, 'local_header_offset': 100,
           'name_sha256': hashlib.sha256(name.encode()).hexdigest(),
           'artifact': {'id': artifact_id, 'name': 'fixed-original', 'digest': 'sha256:'+'a'*64,
                        'size_in_bytes': 4096,
                        'archive_download_url': f'https://api.github.com/repos/securely1g/sonic-buildimage/actions/artifacts/{artifact_id}/zip',
                        'workflow_run': {'id': 43477122783, 'head_sha': '1e423956afe2cb706c239c8be93c1000bbc7a479'}}}
    api = mock.Mock(return_value=dict(row['artifact'], expired=False, expires_at='2036-01-01T00:00:00Z'))
    name_bytes = name.encode()
    local = struct.pack('<4s5H3I2H', b'PK\x03\x04', 20, 8, 0, 0, 0, 0, 0, 0, len(name_bytes), 0)
    replies = [Response(local, 100, 4096), Response(name_bytes, 130, 4096),
               Response(payload, 130+len(name_bytes), 4096)]
    redirect = urllib.error.HTTPError(row['artifact']['archive_download_url'], 302, 'redirect',
        headers(Location='https://fixture.blob.core.windows.net/original?sig=test-only'), io.BytesIO())
    return row, api, Opener([redirect, *replies]), replies


class FixedOriginalTransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_one(self, data=None):
        row, api, opener, replies = fixture() if data is None else data
        result = subject.recover_one(row, api, opener, 'test-only-token', self.root, original, limits())
        return result, row, api, opener, replies

    def test_exact_bytes_sha_crc_and_host_token_separation(self):
        result, row, api, opener, replies = self.run_one()
        self.assertEqual((self.root/row['name']).read_bytes(), b'original archive bytes')
        self.assertEqual(result['sha256'], row['sha256'])
        self.assertEqual(result['CRC32'], format(row['crc32'], '08x'))
        self.assertFalse(result['whole_ZIP_authenticated'])
        self.assertFalse(result['source_or_image_admission'])
        self.assertIn('Authorization', opener.requests[0][0].headers)
        self.assertTrue(all('Authorization' not in r.headers for r, _ in opener.requests[1:]))
        self.assertTrue(all(r.closed for r in replies))
        self.assertLessEqual(max(replies[-1].reads), original.CHUNK)
        api.assert_called_once_with('/actions/artifacts/'+str(row['artifact']['id']))

    def test_status_200_rejected_before_read(self):
        data=fixture(); data[3][0].status=200
        with self.assertRaisesRegex(ValueError, 'headers differ'): self.run_one(data)
        self.assertEqual(data[3][0].reads, [])
        self.assertTrue(data[3][0].closed)

    def test_range_length_encoding_and_transfer_headers_fail_before_read(self):
        for field,value in [('Content-Range','bytes 0-29/4096'),('Content-Length','31'),
                            ('Content-Encoding','gzip'),('Transfer-Encoding','chunked')]:
            with self.subTest(field=field):
                data=fixture(); response=data[3][0]
                if field in response.headers: del response.headers[field]
                response.headers[field]=value
                with self.assertRaisesRegex(ValueError,'headers differ'): self.run_one(data)
                self.assertEqual(response.reads,[])

    def test_duplicate_header_rejected(self):
        data=fixture(); data[3][0].headers['Content-Length']='30'
        with self.assertRaisesRegex(ValueError,'headers differ'): self.run_one(data)
        self.assertEqual(data[3][0].reads,[])

    def test_redirect_origin_and_duplicates_rejected(self):
        for value in ['http://fixture.blob.core.windows.net/x','https://user@fixture.blob.core.windows.net/x',
                      'https://unrelated.example/x','https://fixture.blob.core.windows.net/x#fragment']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                subject.validate_location(headers(Location=value))
        repeated=headers(Location='https://fixture.blob.core.windows.net/x')
        repeated['Location']='https://fixture.blob.core.windows.net/y'
        with self.assertRaisesRegex(ValueError,'not unique'): subject.validate_location(repeated)

    def test_host_redirect_is_closed_without_following(self):
        row,api,opener,replies=fixture(); body=io.BytesIO()
        opener.replies[1]=urllib.error.HTTPError('https://fixture.blob.core.windows.net/x',302,'redirect',
            headers(Location='https://unrelated.example'),body)
        with self.assertRaisesRegex(ValueError,'Unexpected hosting'): self.run_one((row,api,opener,replies))
        self.assertTrue(body.closed)
        self.assertEqual(len(opener.requests),2)

    def test_local_name_and_method_cannot_select_another_member(self):
        data=fixture(); data[3][1].raw=io.BytesIO(b'x'*len(data[0]['name']))
        with self.assertRaises(AssertionError): self.run_one(data)
        data=fixture(); data[0]['method']=8
        with self.assertRaisesRegex(ValueError,'stored'): self.run_one(data)
        self.assertFalse(data[1].called)

    def test_zip64_local_sizes_must_agree(self):
        row=fixture()[0]; name=row['name'].encode()
        raw=struct.pack('<4s5H3I2H',b'PK\x03\x04',45,8,0,0,0,0,0xffffffff,0xffffffff,len(name),20)
        header=subject.local_header(raw,row)
        extra=struct.pack('<HHQQ',1,16,row['bytes'],row['bytes'])
        self.assertEqual(subject.member_offset(name+extra,header,row),100+30+len(name)+20)
        bad=struct.pack('<HHQQ',1,16,row['bytes']+1,row['bytes'])
        with self.assertRaises(AssertionError): subject.member_offset(name+bad,header,row)

    def test_truncated_and_oversized_streams_are_fatal(self):
        for body in [b'short',b'original archive bytes plus surplus']:
            with self.subTest(body=body):
                data=fixture(); data[3][-1].raw=io.BytesIO(body)
                with self.assertRaises(ValueError): self.run_one(data)
                path=self.root/data[0]['name']
                if path.exists(): path.unlink()

    def test_wrong_digest_and_crc_are_fatal(self):
        for key,value in [('sha256','0'*64),('crc32',0)]:
            with self.subTest(key=key):
                data=fixture(); data[0][key]=value
                with self.assertRaisesRegex(ValueError,'digest or CRC'): self.run_one(data)
                (self.root/data[0]['name']).unlink()

    def test_output_write_failure_is_not_source_unavailability(self):
        data=fixture()
        with mock.patch.object(original,'copy_member',side_effect=OSError(28,'fixture output failure')):
            with self.assertRaises(OSError): self.run_one(data)

    def test_existing_destination_is_never_overwritten(self):
        data=fixture(); path=self.root/data[0]['name']; path.write_bytes(b'preserve')
        with self.assertRaises(FileExistsError): self.run_one(data)
        self.assertEqual(path.read_bytes(),b'preserve')

    def test_api_identity_and_expiry_rejected_before_hosting(self):
        for key,value in [('digest','sha256:'+'b'*64),('expired',True)]:
            with self.subTest(key=key):
                data=fixture(); data[1].return_value[key]=value
                with self.assertRaises(ValueError): self.run_one(data)
                self.assertEqual(data[2].requests,[])

    def test_deadline_and_range_budgets_stop_before_requests(self):
        row,api,opener,_=fixture(); budget=limits(); budget['deadline']=time.monotonic()-1
        with self.assertRaises(TimeoutError): subject.recover_one(row,api,opener,'test',self.root,original,budget)
        api.assert_not_called()
        for start,end,total in [(-1,29,4096),(0,4096,4096)]:
            with self.assertRaises(ValueError): subject.open_range(opener,'unused',start,end,total,limits())
        budget=limits(); budget['maximum_hosting_bytes']=2
        with self.assertRaises(ValueError): subject.open_range(opener,'unused',0,29,4096,budget)
        self.assertEqual(opener.requests,[])

    def test_exact_plan_and_changed_plan_rejected(self):
        path=Path(__file__).with_name('vs_final_native_layer11_inputs.json')
        value=subject.load_plan(path)
        self.assertEqual(sum(r['bytes']for r in value['archives']),1381692771)
        changed=self.root/'plan.json'; changed.write_bytes(path.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'Fixed plan differs'): subject.load_plan(changed)

    def test_later_failure_keeps_first_group_and_restores_timer(self):
        row,api,opener,_=fixture(); second=fixture('docker-gbsyncd-vs.gz')[0]
        api.side_effect=[api.return_value,ValueError('fixed metadata failure')]
        plan={'archives':[row,second],'maximum_seconds':30,'maximum_HTTP_requests_including_metadata':10,
              'maximum_hosting_body_bytes':200000}
        report={}; prior=signal.getsignal(signal.SIGALRM)
        with mock.patch.object(subject.urllib.request,'build_opener',return_value=opener):
            with self.assertRaisesRegex(RuntimeError,'Fixed original archive recovery failed'):
                subject.recover_two(plan,api,'test-only-token',self.root/'outputs',original,report)
        self.assertEqual(report['status'],'INCOMPLETE')
        self.assertEqual(len(report['archives']),1)
        self.assertEqual(report['active_artifact'],second['artifact']['id'])
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL),(0.0,0.0))
        self.assertEqual(signal.getsignal(signal.SIGALRM),prior)


if __name__ == '__main__':
    unittest.main()
