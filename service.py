"""Bounded, persistent recovery. This module does not depend on FlaskFarm."""
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta

EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.ts', '.m2ts', '.wmv', '.m4v',
              '.mp3', '.flac', '.m4a', '.aac', '.wav', '.ogg', '.opus', '.wma'}
PREFIX = 'ff_plex_scan_recovery'


def readonly(path):
    db = sqlite3.connect('file:' + os.path.abspath(path) + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def section_for(path, roots, requested=0):
    path = os.path.normpath(path)
    matches = [(len(root), section) for section, root in roots
               if (not requested or section == int(requested))
               and path.startswith(os.path.normpath(root).rstrip('/') + '/')]
    return max(matches)[1] if matches else None


class Store:
    def __init__(self, path):
        self.path = path
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS target (
                    id INTEGER PRIMARY KEY, path TEXT NOT NULL, section INTEGER NOT NULL,
                    source INTEGER, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
                    job_id INTEGER, callback TEXT, detail TEXT NOT NULL DEFAULT '',
                    updated REAL NOT NULL, UNIQUE(path,section));
                CREATE INDEX IF NOT EXISTS target_due ON target(status,due,id);
                CREATE TABLE IF NOT EXISTS event (
                    id INTEGER PRIMARY KEY, target_id INTEGER, stage TEXT,
                    detail TEXT, created REAL);
            ''')
            columns = {r[1] for r in db.execute('PRAGMA table_info(target)')}
            for name in ('backend','shyni_job','plex_state','shyni_state'):
                if name not in columns:
                    db.execute('ALTER TABLE target ADD COLUMN ' + name + ' TEXT')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def get(self, key, default='0'):
        with closing(self.connect()) as db:
            row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
            return row[0] if row else default

    def ingest(self, rows, roots):
        with closing(self.connect()) as db, db:
            for row in rows:
                path = row['target'] or ''
                # No directory traversal, NFO probing, image reads, or cloud stat here.
                if not os.path.isabs(path) or os.path.splitext(path)[1].lower() not in EXTENSIONS:
                    continue
                section = section_for(path, roots, row['target_section_id'] or 0)
                if section is None:
                    continue
                if row['mode'] not in ('ADD', 'REMOVE_FILE', 'REMOVE_FOLDER'):
                    continue
                ignored = row['mode'].startswith('REMOVE') or 'CANCEL' in (row['status'] or '')
                db.execute('''INSERT INTO target(path,section,source,status,updated)
                    VALUES(?,?,?,?,?) ON CONFLICT(path,section) DO UPDATE SET
                    source=excluded.source,
                    status=CASE WHEN target.status IN ('queued','submitting') THEN target.status
                                ELSE excluded.status END,
                    attempts=CASE WHEN target.status IN ('queued','submitting') THEN target.attempts ELSE 0 END,
                    due=0, updated=excluded.updated
                    WHERE excluded.source > target.source''',
                    (path, section, row['id'], 'ignored' if ignored else 'pending', time.time()))
            if rows:
                db.execute("INSERT OR REPLACE INTO state VALUES('cursor',?)", (str(rows[-1]['id']),))

    def update(self, target_id, status, detail, stage=None, **fields):
        fields.update(status=status, detail=detail, updated=time.time())
        if set(fields) - {'status', 'detail', 'updated', 'attempts', 'due', 'job_id', 'callback', 'backend', 'shyni_job', 'plex_state', 'shyni_state'}:
            raise ValueError('Invalid update field')
        with closing(self.connect()) as db, db:
            db.execute('UPDATE target SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?',
                       tuple(fields.values()) + (target_id,))
            db.execute('INSERT INTO event(target_id,stage,detail,created) VALUES(?,?,?,?)',
                       (target_id, stage or status, detail, time.time()))
            db.execute('DELETE FROM event WHERE id < (SELECT MAX(id)-20000 FROM event)')

    def rows(self, statuses, limit=100, sections=()):
        with closing(self.connect()) as db:
            section_sql = ' AND section IN (' + ','.join('?' for _ in sections) + ')' if sections else ''
            return [dict(x) for x in db.execute(
                'SELECT * FROM target WHERE status IN (' + ','.join('?' for _ in statuses) +
                ') AND due<=?' + section_sql + ' ORDER BY id LIMIT ?', tuple(statuses) + (time.time(),) + tuple(sections) + (limit,))]

    def results(self, page=1):
        page = max(1, int(page))
        with closing(self.connect()) as db:
            rows = [dict(x) for x in db.execute('SELECT * FROM target ORDER BY updated DESC,id DESC LIMIT 100 OFFSET ?', ((page-1)*100,))]
            for row in rows:
                row['events'] = [dict(e) for e in db.execute('SELECT stage,detail,created FROM event WHERE target_id=? ORDER BY id DESC LIMIT 12', (row['id'],))]
            counts = dict(db.execute('SELECT status,COUNT(*) FROM target GROUP BY status'))
            return {'rows': rows, 'counts': counts, 'page': page, 'total': sum(counts.values())}

    def reset_cursor(self):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO state VALUES('cursor','0')")

    def add_files(self, paths, roots):
        with closing(self.connect()) as db, db:
            for path in paths:
                section = section_for(path, roots)
                if section is not None:
                    db.execute('''INSERT INTO target(path,section,source,status,updated)
                        VALUES(?,?,0,'pending',?) ON CONFLICT(path,section) DO UPDATE SET
                        status='pending',due=0,attempts=0,updated=excluded.updated
                        WHERE target.status NOT IN ('queued','submitting')''', (path,section,time.time()))

    def retry(self, target_id):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE target SET status='pending',due=0,attempts=0,updated=? WHERE id=? AND status IN ('failed','ignored','retry')", (time.time(),int(target_id)))


class Inspector:
    def __init__(self, mate_db, plex_db):
        self.mate_db, self.plex_db = mate_db, plex_db

    def roots(self):
        with closing(readonly(self.plex_db)) as db:
            return [(int(r[0]), r[1]) for r in db.execute('SELECT library_section_id,root_path FROM section_locations')]

    def sections(self):
        with closing(readonly(self.plex_db)) as db:
            return [dict(x) for x in db.execute('SELECT id,name FROM library_sections ORDER BY name')]

    def history(self, cursor, days=30, limit=200):
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        with closing(readonly(self.mate_db)) as db:
            return [dict(x) for x in db.execute('''SELECT id,target,target_section_id,mode,status
                FROM scan_item WHERE id>? AND created_time>=?
                AND (callback IS NULL OR callback!=?) ORDER BY id LIMIT ?''',
                (cursor, cutoff, PREFIX, limit))]

    def registered(self, targets):
        if not targets:
            return set()
        paths = list(dict.fromkeys(t['path'] for t in targets))
        with closing(readonly(self.plex_db)) as db:
            rows = db.execute('''SELECT mp.file,mi.library_section_id FROM media_parts mp
                JOIN media_items mi ON mi.id=mp.media_item_id
                WHERE mp.file IN (''' + ','.join('?' for _ in paths) + ')', paths)
            return {(r[0], int(r[1])) for r in rows}

    def active(self):
        with closing(readonly(self.mate_db)) as db:
            row = db.execute("SELECT id FROM scan_item WHERE status IN ('READY','ENQUEUE_ADD_FIND','ENQUEUE_REMOVE_REMOVED','SCANNING') OR shyni_status='RUNNING' LIMIT 1").fetchone()
            periodic = db.execute("SELECT id FROM periodic_item WHERE status='working' LIMIT 1").fetchone()
            return bool(row or periodic)

    def job(self, callback):
        with closing(readonly(self.mate_db)) as db:
            row = db.execute('''SELECT id,status,filecheck_time,process_start_time,
                process_finish_time,completed_time FROM scan_item
                WHERE callback_id=? ORDER BY id DESC LIMIT 1''', (callback,)).fetchone()
            return dict(row) if row else None

    def latest_removed(self, path):
        ancestors = [path]
        parent = os.path.dirname(path)
        while parent and parent != '/':
            ancestors.append(parent)
            parent = os.path.dirname(parent)
        with closing(readonly(self.mate_db)) as db:
            row = db.execute("SELECT mode FROM scan_item WHERE target IN (" + ','.join('?' for _ in ancestors) + ") AND mode IN ('ADD','REMOVE_FILE','REMOVE_FOLDER') ORDER BY id DESC LIMIT 1", ancestors).fetchone()
            return bool(row and row[0].startswith('REMOVE'))


class Recovery:
    def __init__(self, store, inspector, enqueue, playing, exists=os.path.isfile, secondary=None):
        self.store, self.inspect = store, inspector
        self.enqueue, self.playing, self.exists = enqueue, playing, exists
        self.secondary = secondary

    def assessment(self, target, registered):
        plex = (target['path'],target['section']) in registered
        shyni = self.secondary.check(target['path']) if self.secondary else None
        plex_state = '등록' if plex else '누락'
        shyni_state = ('등록' if shyni else '누락') if shyni is not None else ('섹션 없음' if self.secondary else '사용 안함')
        if target.get('plex_state') != plex_state or target.get('shyni_state') != shyni_state:
            self.store.update(target['id'],target['status'],'Plex: %s / 샤이니: %s' % (plex_state,shyni_state),stage='등록 대조',plex_state=plex_state,shyni_state=shyni_state)
        return plex, shyni

    def send_shyni(self, target):
        self.store.update(target['id'],'submitting','샤이니 부분 스캔 요청 준비',backend='shyni',shyni_job=None)
        job_id = self.secondary.enqueue(target['path'],target['callback'])
        self.store.update(target['id'],'queued','샤이니 #%s' % job_id,stage='샤이니 큐 등록',backend='shyni',shyni_job=str(job_id))
        return '샤이니 파일 1개 복구 요청'

    def tick(self, settings):
        # Fail closed: playback/API/DB errors propagate and no new job is submitted.
        if settings.get('pause_playback', True) and self.playing():
            return '재생 중: 사전검토·복구 대기'
        roots = self.inspect.roots()
        selected = set(settings.get('sections', []))
        roots = [r for r in roots if not selected or r[0] in selected]
        history = self.inspect.history(int(self.store.get('cursor')), settings.get('days', 30))
        self.store.ingest(history, roots)
        active = self.store.rows(('queued', 'submitting'), 1)
        if active:
            target = active[0]
            if target.get('backend') == 'shyni':
                if not self.secondary:
                    return '진행 중인 샤이니 작업 확인을 위해 샤이니 검토를 켜주세요.'
                if not target.get('shyni_job'):
                    # The server guarantees retries with this same key return the same job.
                    return self.send_shyni(target)
                job = self.secondary.job(target['shyni_job'])
                if not job:
                    self._retry(target,settings,'샤이니 작업 기록 없음: 등록 여부 재검토 필요')
                    return '샤이니 작업 확인 실패'
                if job['status'] not in ('completed','failed'):
                    detail = '샤이니 #%s: %s' % (target['shyni_job'],job['status'])
                    if target['detail'] != detail:
                        self.store.update(target['id'],'queued',detail,stage='샤이니 스캔 진행')
                    return '샤이니 완료 대기'
                plex, shyni = self.assessment(target,self.inspect.registered([target]))
                if plex and shyni is True:
                    self.store.update(target['id'],'recovered','Plex·샤이니 실제 파일 등록 확인',stage='최종 등록 확인')
                else:
                    self._retry(target,settings,'샤이니 종료 후 등록 미확인 · ' + str(job.get('error') or job['status']))
                return '샤이니 종료 및 등록 검증 완료'
            job = self.inspect.job(target['callback'])
            if not job:
                # An ambiguous enqueue outcome must never silently submit a duplicate.
                self.store.update(target['id'], 'submitting', '큐 등록 응답 미확인: 자동 중복 요청 보류')
                return '큐 등록 확인 필요'
            status = job['status'] or ''
            detail = str(job['id']) + ': ' + status
            if not status.startswith('FINISH_'):
                if target['detail'] != detail:
                    self.store.update(target['id'], 'queued', detail, stage='스캔 진행', job_id=job['id'])
                return 'Plex Mate 완료 대기 (추가 큐 없음)'
            registered = self.inspect.registered([target])
            plex, shyni = self.assessment(target,registered)
            if plex and shyni is False:
                return self.send_shyni(target)
            if plex:
                self.store.update(target['id'], 'recovered', detail, stage='Plex DB 등록 확인', job_id=job['id'])
            else:
                self._retry(target, settings, '스캔 종료 후 DB 미등록 · ' + detail)
            return '스캔 종료 및 DB 검증 완료'
        candidates = self.store.rows(('pending', 'retry'), 5 if self.secondary else 100, sorted(selected))
        registered = self.inspect.registered(candidates)
        for target in candidates:
            if self.inspect.latest_removed(target['path']):
                self.store.update(target['id'], 'ignored', '최근 삭제 요청: 복구 제외')
                continue
            plex, shyni = self.assessment(target,registered)
            if plex and shyni is not False:
                self.store.update(target['id'], 'present', '설정된 대상에 이미 등록됨', stage='파일 체크')
                continue
            if not settings.get('submit', True):
                return '누락 발견: ' + target['path']
            if self.inspect.active():
                return '기존 Plex Mate 스캔 큐 대기'
            # Recheck just before dispatch: no filesystem/network mutation while playing.
            if settings.get('pause_playback', True) and self.playing():
                return '재생 시작: 복구 대기'
            if not self.exists(target['path']):
                self.store.update(target['id'], 'ignored', '원본 파일 없음: 복구 제외', stage='파일 체크')
                continue
            attempts = target['attempts'] + 1
            callback = '%s_%s-%s' % (PREFIX, target['id'], attempts)
            self.store.update(target['id'], 'submitting', '누락 확인: 큐 등록 준비', stage='파일 체크', attempts=attempts, callback=callback,backend='plex',shyni_job=None,job_id=None)
            target = dict(target,callback=callback,attempts=attempts)
            if plex and shyni is False:
                return self.send_shyni(target)
            try:
                job_id = self.enqueue(target['path'], target['section'], callback)
            except Exception:
                # Preserve reservation; on the next cycle reconcile by callback.
                raise
            self.store.update(target['id'], 'queued', 'Plex Mate #%s' % job_id, stage='큐 등록', job_id=job_id,backend='plex')
            return '폴더 1개 복구 요청: ' + os.path.dirname(target['path'])
        return '이력 %d건 검토 · 대기 대상 %d건' % (len(history), len(candidates))

    def _retry(self, target, settings, detail):
        exhausted = target['attempts'] >= settings.get('max_attempts', 3)
        self.store.update(target['id'], 'failed' if exhausted else 'retry', detail,
                          stage='스캔 결과', due=time.time() + settings.get('retry_minutes', 60)*60)
