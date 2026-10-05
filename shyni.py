"""Shyni compatibility API: exact file lookup, idempotent partial scan, job polling."""
import os
import unicodedata

import requests


class ShyniClient:
    def __init__(self, url, token, rules='', session_token='', session=None):
        if not url or not token:
            raise ValueError('Plex Mate에 샤이니 주소·토큰을 설정하세요.')
        self.url = url.rstrip('/')
        self.headers = {'X-Plex-Token': token, 'Accept': 'application/json'}
        self.rules = [tuple(x.strip() for x in line.split('|',1)) for line in rules.splitlines() if '|' in line]
        self.session_token = session_token
        self.http = session or requests.Session()
        self.section_cache = {}

    def path(self, path):
        for src, dst in self.rules:
            if src and (path == src.rstrip('/') or path.startswith(src.rstrip('/') + '/')):
                path = dst.rstrip('/') + path[len(src.rstrip('/')):]
                break
        return unicodedata.normalize('NFC', path.replace('\\','/'))

    def get(self, endpoint, **params):
        response = self.http.get(self.url + endpoint, params=params, headers=self.headers, timeout=(3,10))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def section(self, path):
        parent = os.path.dirname(path)
        if parent not in self.section_cache:
            found = self.get('/compat/section_by_path',path=path)
            self.section_cache[parent] = int(found['section_id']) if found else None
        return self.section_cache[parent]

    def check(self, original):
        path = self.path(original)
        section = self.section(path)
        if section is None:
            return None  # No configured library; never submit to an invented section.
        data = self.get('/compat/parts',path=path,section=section,limit=1)
        if data is None:
            raise RuntimeError('샤이니 정확한 파일 조회 API를 사용할 수 없습니다.')
        return any(x['path'] == path and x.get('status') == 'available' for x in data['data'])

    def enqueue(self, original, callback):
        path = self.path(original)
        section = self.section(path)
        if section is None:
            raise RuntimeError('샤이니 섹션을 찾을 수 없습니다.')
        response = self.http.post(self.url + '/library/sections/%s/refresh' % section,
            headers={**self.headers,'Idempotency-Key':callback + '-shyni'},
            json={'path':path,'mode':'add','wait':600,'recursive':False}, timeout=(3,30))
        response.raise_for_status()
        job_id = response.headers.get('X-Job-Id') or response.json().get('job_id')
        if not job_id:
            raise RuntimeError('샤이니 작업 ID 미확인: 완료로 처리하지 않습니다.')
        return str(job_id)

    def job(self, job_id):
        return self.get('/compat/jobs/' + str(job_id))

    def playing(self):
        if not self.session_token:
            return False
        response = self.http.get(self.url + '/playback/sessions',
            headers={'Authorization':'Bearer ' + self.session_token},timeout=(3,10))
        response.raise_for_status()
        return int(response.json()['count']) > 0
