import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('shyni',Path(__file__).parents[1]/'shyni.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Response:
    status_code = 200
    headers = {}
    def __init__(self,data):
        self.data = data
    def json(self):
        return self.data
    def raise_for_status(self):
        pass


class ShyniTests(unittest.TestCase):
    def test_exact_available_path_and_idempotent_nonrecursive_scan(self):
        calls=[]
        session=type('Session',(),{})()
        def get(url,**kwargs):
            calls.append((url,kwargs))
            if url.endswith('/section_by_path'):
                return Response({'section_id':99})
            return Response({'data':[{'path':'/remote/show/a.mkv','status':'available'}]})
        def post(url,**kwargs):
            calls.append((url,kwargs))
            return Response({'job_id':42})
        session.get,session.post = get,post
        client = module.ShyniClient('http://shyni','secret','/media|/remote',session=session)
        self.assertTrue(client.check('/media/show/a.mkv'))
        self.assertEqual(client.enqueue('/media/show/a.mkv','key'),'42')
        self.assertEqual(calls[-1][1]['json']['path'],'/remote/show/a.mkv')
        self.assertFalse(calls[-1][1]['json']['recursive'])
        self.assertEqual(calls[-1][1]['headers']['Idempotency-Key'],'key-shyni')
        self.assertEqual(calls[-1][0],'http://shyni/library/sections/99/refresh')

    def test_mapping_respects_directory_boundary(self):
        client=module.ShyniClient('http://shyni','secret','/media|/remote')
        self.assertEqual(client.path('/media2/a.mkv'),'/media2/a.mkv')
