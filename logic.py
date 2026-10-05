import os
import json
import threading
import time
import xml.etree.ElementTree as ET

import requests
from flask import jsonify, render_template
from framework import F, path_data
from plugin import Job, PluginModuleBase
from apscheduler.triggers.cron import CronTrigger

from .service import EXTENSIONS, Inspector, Recovery, Store, section_for
from .setup import P
from .shyni import ShyniClient


class Logic(PluginModuleBase):
    db_default = {
        'enabled': 'False', 'schedule': '*/5 * * * *', 'sections': '',
        'pause_playback': 'True', 'history_days': '30', 'retry_minutes': '60',
        'max_attempts': '3', 'poll_seconds': '15', 'cycles_per_run': '20',
        'folder_target': '',
        'use_shyni': 'Auto', 'shyni_session_token': '',
        'section_schedules': '[]', 'registered_section_jobs': '[]',
    }

    def __init__(self, PM):
        super().__init__(PM, name='main', first_menu='setting')
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.state_lock = threading.Lock()
        self.state = {'running': False, 'message': '대기', 'last_run': ''}
        self.store = None
        self.pending_lock = threading.Lock()
        self.retry_timer = None

    def plugin_load(self):
        self._store()
        self.sync_schedule()
        if self._store().pending_runs():
            self.defer_pending()

    def plugin_unload(self):
        self.stop.set()
        if self.retry_timer:
            self.retry_timer.cancel()
        for job_id in [self.job_id] + json.loads(P.ModelSetting.get('registered_section_jobs') or '[]'):
            if F.scheduler.is_include(job_id):
                F.scheduler.remove_job(job_id)

    @property
    def job_id(self):
        return P.package_name + '_recovery'

    def _store(self):
        if self.store is None:
            self.store = Store(os.path.join(path_data, 'db', P.package_name + '_recovery.db'))
        return self.store

    def _mate(self):
        mate = F.PluginManager.get_plugin_instance('plex_mate')
        if mate is None:
            raise RuntimeError('Plex Mate가 실행 중이어야 합니다.')
        return mate

    def _inspector(self):
        mate = self._mate()
        return Inspector(os.path.join(path_data, 'db', 'plex_mate.db'), mate.ModelSetting.get('base_path_db'))

    def _playing(self):
        mate = self._mate()
        url = (mate.ModelSetting.get('base_url') or '').rstrip('/')
        token = mate.ModelSetting.get('base_token')
        if not url or not token:
            raise RuntimeError('Plex Mate의 Plex 주소·토큰을 먼저 설정하세요.')
        response = requests.get(url + '/status/sessions', headers={'X-Plex-Token': token}, timeout=(3,10))
        response.raise_for_status()
        root = ET.fromstring(response.content)
        # Any session, including paused/buffering, conservatively defers recovery.
        plex_playing = bool(list(root)) or int(root.get('size', '0')) > 0
        if plex_playing:
            return True
        secondary = self._shyni()
        return secondary.playing() if secondary else False

    def _shyni(self):
        mate = self._mate()
        use = P.ModelSetting.get('use_shyni')
        enabled = use == 'True' or (use == 'Auto' and mate.ModelSetting.get('scan_shyni_use') == 'True')
        if not enabled:
            return None
        return ShyniClient(mate.ModelSetting.get('intro_shyni_url'),mate.ModelSetting.get('intro_shyni_token'),
            mate.ModelSetting.get('scan_shyni_path_rule') or '',P.ModelSetting.get('shyni_session_token') or '')

    def _enqueue(self, path, section, callback):
        with F.app.app_context():
            model = self._mate().get_module('scan').web_list_model
            item = model(path, mode='ADD', target_section_id=str(section), callback_id=callback)
            # Recovery tracks each destination sequentially. Skip automatic fan-out
            # for this item only; ordinary Plex Mate/GDS requests remain unchanged.
            if hasattr(item,'shyni_status'):
                item.shyni_status = 'SKIP'
            item.save()
            return int(item.id)

    def options(self, submit=True):
        get = P.ModelSetting.get
        return {'sections': [int(x) for x in (get('sections') or '').split(',') if x.strip()],
                'pause_playback': get('pause_playback') == 'True', 'submit': submit,
                'days': max(1,min(365,int(get('history_days')))),
                'retry_minutes': max(1,int(get('retry_minutes'))),
                'max_attempts': max(1,min(10,int(get('max_attempts'))))}

    def set_state(self, **values):
        with self.state_lock:
            self.state.update(values)

    def sync_schedule(self):
        jobs = self.section_jobs()
        for job in jobs:
            self.validate_schedule(job['schedule'])
        if P.ModelSetting.get('enabled') == 'True':
            self.validate_schedule(P.ModelSetting.get('schedule'))
        previous = json.loads(P.ModelSetting.get('registered_section_jobs') or '[]')
        current = [self.section_job_id(j['section_id']) for j in jobs]
        for job_id in set(previous + current):
            if F.scheduler.is_include(job_id):
                F.scheduler.remove_job(job_id)
        if F.scheduler.is_include(self.job_id):
            F.scheduler.remove_job(self.job_id)
        if P.ModelSetting.get('enabled') == 'True':
            F.scheduler.add_job_instance(Job(P.package_name,self.job_id,P.ModelSetting.get('schedule'),self.scheduler_function,'누락 스캔 복구'))
        registered = []
        for job in jobs:
            if not job['enabled']:
                continue
            job_id = self.section_job_id(job['section_id'])
            F.scheduler.add_job_instance(Job(P.package_name,job_id,job['schedule'],self.scheduler_function,
                '누락 복구: ' + job['name'],args=(job['section_id'],)))
            registered.append(job_id)
        P.ModelSetting.set('registered_section_jobs',json.dumps(registered))

    def section_job_id(self, section_id):
        return self.job_id + '_section_' + str(int(section_id))

    def section_jobs(self):
        return json.loads(P.ModelSetting.get('section_schedules') or '[]')

    @staticmethod
    def validate_schedule(value):
        value = ' '.join((value or '').split())
        if value.isdigit():
            if int(value) < 1:
                raise ValueError('분 단위 주기는 1 이상이어야 합니다.')
        else:
            CronTrigger.from_crontab(value)
        return value

    def setting_save_after(self, changes):
        self.options()
        self._store().recheck_completed()
        self.sync_schedule()

    def scheduler_function(self, section_id=None):
        with F.app.app_context():
            if section_id is None:
                sections = self.options()['sections'] or [int(x['id']) for x in self._inspector().sections()]
                overridden = {j['section_id'] for j in self.section_jobs() if j['enabled']}
                sections = [s for s in sections if s not in overridden]
                scope = 'common'
            else:
                sections, scope = [int(section_id)],str(int(section_id))
            if not sections:
                return
            self._store().request_run(scope,sections)
            self.start_pending()

    def defer_pending(self):
        with self.state_lock:
            if self.retry_timer and self.retry_timer.is_alive():
                return
            def resume():
                with self.state_lock:
                    self.retry_timer = None
                if not self.stop.is_set():
                    with F.app.app_context():
                        self.start_pending()
            self.retry_timer = threading.Timer(60,resume)
            self.retry_timer.daemon = True
            self.retry_timer.start()

    def start_pending(self):
        if self.lock.locked():
            return
        if not self.pending_lock.acquire(blocking=False):
            self.defer_pending()
            return
        try:
            if not self._store().pending_runs():
                return
            if P.ModelSetting.get('pause_playback') == 'True' and self._playing():
                self.set_state(message='재생 중: 섹션 예약을 대기 큐에 보관')
                self.defer_pending()
                return
            request = self._store().pop_run()
            if request:
                result = self.start(sections_override=request['sections'],run_scope=request['scope'])
                if result['ret'] != 'success':
                    self._store().request_run(request['scope'],request['sections'])
        except Exception:
            P.logger.exception('Section schedule deferred')
            self.defer_pending()
        finally:
            self.pending_lock.release()

    def start(self, submit=True, folder=None, sections_override=None,run_scope=None):
        if not self.lock.acquire(blocking=False):
            return {'ret': 'warning', 'msg': '이미 검토·복구 작업이 실행 중입니다.'}
        self.stop.clear()
        self.set_state(running=True,message='작업 준비',sections=sections_override)
        def work():
            deferred = False
            try:
                with F.app.app_context():
                    inspector = self._inspector()
                    options = self.options(submit)
                    if sections_override is not None:
                        options['sections'] = list(sections_override)
                    store = self._store()
                    if folder is not None:
                        self.inspect_folder(folder, inspector, options)
                    else:
                        engine = Recovery(store,inspector,self._enqueue,self._playing,secondary=self._shyni())
                        cycles = max(1,min(100,int(P.ModelSetting.get('cycles_per_run'))))
                        for _ in range(cycles):
                            if self.stop.is_set():
                                break
                            message = engine.tick(options)
                            self.set_state(message=message,last_run=time.strftime('%Y-%m-%d %H:%M:%S'))
                            P.logger.info(message)
                            if '재생' in message and run_scope is not None:
                                store.request_run(run_scope,sections_override)
                                deferred = True
                            if '재생' in message or '확인 필요' in message or '검토 전용 실행은' in message:
                                break
                            if self.stop.wait(max(5,int(P.ModelSetting.get('poll_seconds')))):
                                break
            except Exception as exc:
                self.set_state(message='작업 보류: ' + str(exc))
                P.logger.exception('Recovery cycle failed')
                if run_scope is not None:
                    self._store().request_run(run_scope,sections_override)
                    deferred = True
            finally:
                self.set_state(running=False)
                self.lock.release()
                if not self.stop.is_set():
                    with F.app.app_context():
                        if deferred:
                            self.defer_pending()
                        else:
                            self.start_pending()
        threading.Thread(target=work,daemon=True).start()
        return {'ret': 'success', 'msg': '작업을 시작했습니다. 상태 화면에서 확인하세요.'}

    def inspect_folder(self, folder, inspector, options):
        if options['pause_playback'] and self._playing():
            raise RuntimeError('재생 중: 폴더 검토 대기')
        folder = os.path.normpath(folder)
        roots = [r for r in inspector.roots() if not options['sections'] or r[0] in options['sections']]
        if not os.path.isabs(folder) or section_for(folder,roots) is None:
            raise ValueError('선택 섹션 안의 하위 폴더를 지정하세요. 라이브러리 루트 전체 검사는 지원하지 않습니다.')
        paths = []
        with os.scandir(folder) as entries:
            for index, entry in enumerate(entries):
                if index >= 2000:
                    raise ValueError('폴더 항목이 2,000개를 초과합니다. 더 작은 하위 폴더를 지정하세요.')
                if self.stop.is_set():
                    return
                if os.path.splitext(entry.name)[1].lower() in EXTENSIONS:
                    paths.append(os.path.join(folder,entry.name))
        self._store().add_files(paths,roots)
        self.set_state(message='폴더 파일 %d개 검토 대상으로 추가 (하위 폴더 제외)' % len(paths))

    def process_menu(self, sub, req):
        arg = P.ModelSetting.to_dict()
        arg['sections_list'], arg['error'] = [], ''
        arg['section_jobs'] = self.section_jobs()
        try:
            arg['sections_list'] = self._inspector().sections()
        except Exception as exc:
            arg['error'] = str(exc)
        return render_template(P.package_name + '_main_' + (sub if sub in ('setting','schedule','status') else 'setting') + '.html',arg=arg)

    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            if command == 'run':
                return jsonify(self.start())
            if command == 'inspect':
                return jsonify(self.start(submit=False))
            if command == 'folder':
                return jsonify(self.start(submit=False,folder=(arg1 or '').strip()))
            if command == 'stop':
                self.stop.set()
                self._store().remove_runs()
                return jsonify(ret='success',msg='현재 요청 이후 중단합니다. 이미 등록된 Plex Mate 작업은 완료를 추적합니다.')
            if command == 'status':
                result = self._store().results(arg1 or 1)
                with self.state_lock:
                    result['worker'] = dict(self.state)
                result['scheduled'] = F.scheduler.is_include(self.job_id)
                result['section_schedules'] = [dict(j,registered=F.scheduler.is_include(self.section_job_id(j['section_id']))) for j in self.section_jobs()]
                result['pending_runs'] = self._store().pending_runs()
                return jsonify(ret='success',data=result)
            if command == 'save_section_schedule':
                section_id = int(arg1)
                names = {int(x['id']):x['name'] for x in self._inspector().sections()}
                if section_id not in names:
                    raise ValueError('존재하지 않는 섹션입니다.')
                schedule = self.validate_schedule(arg2)
                jobs = [j for j in self.section_jobs() if j['section_id'] != section_id]
                jobs.append(dict(section_id=section_id,name=names[section_id],schedule=schedule,enabled=arg3=='true'))
                jobs.sort(key=lambda j:j['section_id'])
                P.ModelSetting.set('section_schedules',json.dumps(jobs,ensure_ascii=False))
                self._store().reset_cursor()
                self.sync_schedule()
                if arg3 != 'true':
                    self._store().remove_runs(section_id)
                return jsonify(ret='success',msg='섹션별 스케줄 저장 완료')
            if command == 'delete_section_schedule':
                section_id = int(arg1)
                jobs = [j for j in self.section_jobs() if j['section_id'] != section_id]
                P.ModelSetting.set('section_schedules',json.dumps(jobs,ensure_ascii=False))
                self._store().remove_runs(section_id)
                self.sync_schedule()
                return jsonify(ret='success',msg='섹션별 스케줄 삭제 완료')
            if command == 'run_section':
                section_id = int(arg1)
                if section_id not in {int(s['id']) for s in self._inspector().sections()}:
                    raise ValueError('존재하지 않는 섹션입니다.')
                self._store().request_run(str(section_id),[section_id])
                self.start_pending()
                return jsonify(ret='success',msg='섹션 작업을 순차 실행 큐에 등록했습니다.')
            if command == 'sections':
                ids = sorted(set(int(x) for x in (arg1 or '').split(',') if x.strip()))
                valid = {int(s['id']) for s in self._inspector().sections()}
                if not set(ids).issubset(valid):
                    raise ValueError('존재하지 않는 섹션입니다.')
                P.ModelSetting.set('sections',','.join(map(str,ids)))
                self._store().reset_cursor()
                return jsonify(ret='success',msg='대상 섹션 저장 완료. 기존 이력을 다시 대조합니다.')
            if command == 'schedule':
                if arg1 == 'true':
                    self.validate_schedule(P.ModelSetting.get('schedule'))
                P.ModelSetting.set('enabled','True' if arg1 == 'true' else 'False')
                self.sync_schedule()
                if arg1 != 'true':
                    self._store().remove_runs('common')
                return jsonify(ret='success',msg='공통 자동 복구 ' + ('등록' if arg1 == 'true' else '해제'))
            if command == 'retry':
                self._store().retry(arg1)
                return jsonify(ret='success',msg='다음 실행에서 다시 검토합니다.')
            if command == 'connection':
                playing = self._playing()
                sections = self._inspector().sections()
                secondary = self._shyni()
                if secondary:
                    # Check authenticated API without reading any remote media.
                    roots = self._inspector().roots()
                    if roots:
                        secondary.get('/compat/section_by_path',path=secondary.path(roots[0][1] + '/connection-check.mkv'))
                return jsonify(ret='success',msg='Plex·DB 연결 정상 · %d개 섹션 · %s · 샤이니 %s' % (len(sections),'재생 중' if playing else '재생 없음','연결 확인' if secondary else '검토 해제'))
            raise ValueError('지원하지 않는 명령입니다.')
        except Exception as exc:
            return jsonify(ret='danger',msg=str(exc))
