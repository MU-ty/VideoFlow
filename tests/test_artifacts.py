import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

from artifacts import archive_current, trusted_download
from asset_runtime import AssetRuntime, Conflict
from asset_worker import pass_once
from research.mock_provider import MockProvider

ROOT=Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    mode='ok'
    def log_message(self,*args):pass
    def do_GET(self):
        if self.mode=='redirect':
            self.send_response(302);self.send_header('Location','/elsewhere');self.end_headers();return
        data=b'0123456789'
        self.send_response(200)
        if self.mode!='chunked':self.send_header('Content-Length','100' if self.mode=='truncated' else str(len(data)))
        self.end_headers();self.wfile.write(data)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        Handler.mode='ok'
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.tmp=tempfile.TemporaryDirectory();self.file=Path(self.tmp.name)/'output'
        self.base='http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.tmp.cleanup();self.server.shutdown();self.server.server_close();self.thread.join()

    def test_stream_download(self):
        trusted_download(self.base+'/view?filename=x.mp4',self.base,self.file,20,3)
        self.assertEqual(self.file.read_bytes(),b'0123456789')

    def test_external_origin_rejected_before_request(self):
        for url in ('file:///etc/passwd','https://example.invalid/view','http://example.invalid/view',self.base+'/other'):
            with self.subTest(url=url),self.assertRaises(ValueError):
                trusted_download(url,self.base,self.file,20,3)

    def test_size_limit_declared_and_streamed(self):
        for mode in ('ok','chunked'):
            Handler.mode=mode
            with self.subTest(mode=mode),self.assertRaises(ValueError):
                trusted_download(self.base+'/view',self.base,self.file,5,3)

    def test_no_redirect(self):
        Handler.mode='redirect'
        with self.assertRaises(HTTPError):trusted_download(self.base+'/view',self.base,self.file,20,3)

    def test_incomplete_response_rejected(self):
        Handler.mode='truncated'
        with self.assertRaises(ValueError):trusted_download(self.base+'/view',self.base,self.file,200,3)


class ArchiveAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.r=AssetRuntime(str(self.root/'db'))
        self.r.initialize(json.loads((ROOT/'fixtures/assets.json').read_text()))
        self.b=MockProvider()
        pass_once(self.r,self.b,max_submit=20);pass_once(self.r,self.b,max_submit=20)
        self.assertIsNotNone(self.r.snapshot()['timeline'])

    def tearDown(self):self.r.close();self.tmp.cleanup()

    def test_no_manifest_on_partial_download_failure(self):
        def fail(url,backend,path,limit,timeout):
            Path(path).write_bytes(b'partial')
            raise OSError('injected interruption')
        with self.assertRaises(OSError):archive_current(self.r,self.root/'out',self.b.base,downloader=fail)
        self.assertFalse((self.root/'out/revision-1.json').exists())
        self.assertFalse(list((self.root/'out').glob('.artifact-*')))

    def test_revision_change_during_download_rejects_publication(self):
        changed=False
        def mutate(url,backend,path,limit,timeout):
            nonlocal changed
            self.b.download(url,backend,path,limit,timeout)
            if not changed:
                changed=True
                event=json.loads((ROOT/'fixtures/edit_costume.json').read_text())
                self.r.edit(event)
        with self.assertRaises(Conflict):archive_current(self.r,self.root/'out',self.b.base,downloader=mutate)
        self.assertFalse((self.root/'out/revision-1.json').exists())

    def test_archive_path_cannot_escape_root(self):
        path=self.root/'out'
        archive_current(self.r,path,self.b.base,downloader=self.b.download)
        file=path/'revision-1.json';data=json.loads(file.read_text())
        data['assets'][0]['file']='../escape'
        file.write_text(json.dumps(data))
        with self.assertRaises(ValueError):archive_current(self.r,path,self.b.base,downloader=self.b.download)

if __name__=='__main__':unittest.main()
