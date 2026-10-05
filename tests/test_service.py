import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('service', Path(__file__).parents[1] / 'service.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = s.Store(str(Path(self.tmp.name)/'state.db'))
        self.rows = [dict(id=1,target='/media/show/a.mkv',target_section_id=3,mode='ADD',status='FINISH_TIMEOVER')]
        self.jobs, self.sent = {}, []
        self.inspector = type('Inspector', (), {})()
        self.inspector.roots = lambda: [(3, '/media')]
        self.inspector.history = lambda cursor, days: [r for r in self.rows if r['id'] > cursor]
        self.inspector.registered = lambda targets: set()
        self.inspector.active = lambda: False
        self.inspector.latest_removed = lambda path: False
        self.inspector.job = lambda callback: self.jobs.get(callback)
        self.recovery = s.Recovery(self.store,self.inspector,self.enqueue,lambda: False,exists=lambda p: True)

    def enqueue(self,path,section,callback):
        self.sent.append(path)
        self.jobs[callback] = dict(id=10,status='SCANNING')
        return 10

    def test_playback_does_not_even_read_history(self):
        self.recovery.playing = lambda: True
        self.inspector.history = lambda *args: self.fail('history read while playing')
        self.recovery.tick({})
        self.assertEqual(self.sent, [])

    def test_one_job_until_verified_and_retry_cap(self):
        self.recovery.tick({})
        self.recovery.tick({})
        self.assertEqual(len(self.sent),1)
        callback = self.store.rows(('queued',))[0]['callback']
        self.jobs[callback]['status'] = 'FINISH_ADD_FOLDER'
        self.recovery.tick({'max_attempts':1})
        self.assertEqual(self.store.results()['counts']['failed'],1)

    def test_db_presence_not_finish_status_confirms_success(self):
        self.recovery.tick({})
        callback = self.store.rows(('queued',))[0]['callback']
        self.jobs[callback]['status'] = 'FINISH_SCANNING'
        self.inspector.registered = lambda targets: {('/media/show/a.mkv',3)}
        self.recovery.tick({})
        self.assertEqual(self.store.results()['counts']['recovered'],1)

    def test_removal_and_nfo_never_queued(self):
        self.rows += [dict(self.rows[0],id=2,mode='REMOVE_FILE'),dict(self.rows[0],id=3,target='/media/show/a.nfo')]
        self.recovery.tick({})
        self.assertEqual(self.sent,[])

    def test_ambiguous_submission_survives_restart_without_duplicate(self):
        def fail(*args):
            raise RuntimeError('lost response')
        self.recovery.enqueue = fail
        with self.assertRaises(RuntimeError):
            self.recovery.tick({})
        self.recovery.enqueue = self.enqueue
        self.recovery.tick({})
        self.assertEqual(self.sent,[])

    def test_boundary_and_requested_section(self):
        roots = [(3,'/media'),(4,'/media/special')]
        self.assertEqual(s.section_for('/media/special/a.mkv',roots),4)
        self.assertIsNone(s.section_for('/media2/a.mkv',roots))
        self.assertIsNone(s.section_for('/media/a.mkv',roots,4))

    def test_replayed_history_does_not_reset_attempts(self):
        self.recovery.tick({})
        self.store.reset_cursor()
        self.recovery.tick({})
        self.assertEqual(self.store.rows(('queued',))[0]['attempts'],1)

    def test_refresh_events_are_not_add_requests(self):
        self.rows = [dict(self.rows[0],mode='REFRESH')]
        self.recovery.tick({})
        self.assertEqual(self.sent,[])

    def test_selection_is_applied_before_page_limit(self):
        self.store.add_files(['/media/a.mkv','/other/a.mkv'],[(3,'/media'),(4,'/other')])
        self.assertEqual(self.store.rows(('pending',),1,[4])[0]['section'],4)

    def test_existing_queue_and_playback_fail_closed(self):
        self.inspector.active = lambda: True
        self.recovery.tick({})
        self.assertEqual(self.sent,[])
        self.recovery.playing = lambda: (_ for _ in ()).throw(RuntimeError('Plex unavailable'))
        with self.assertRaises(RuntimeError):
            self.recovery.tick({})

    def secondary(self):
        second = type('Secondary',(),{})()
        second.present = False
        second.sent = []
        second.check = lambda path: second.present
        def enqueue(path,callback):
            second.sent.append((path,callback))
            return '42'
        second.enqueue = enqueue
        second.job = lambda job_id: {'status':'completed'}
        self.recovery.secondary = second
        return second

    def test_shyni_only_missing_does_not_rescan_plex(self):
        second = self.secondary()
        self.inspector.registered = lambda targets: {('/media/show/a.mkv',3)}
        self.recovery.tick({})
        self.assertEqual(self.sent,[])
        self.assertEqual(len(second.sent),1)
        second.present = True
        self.recovery.tick({})
        self.assertEqual(self.store.results()['counts']['recovered'],1)

    def test_both_missing_are_scanned_sequentially(self):
        second = self.secondary()
        self.recovery.tick({})
        self.assertEqual(len(self.sent),1)
        self.assertEqual(second.sent,[])
        target = self.store.rows(('queued',))[0]
        self.jobs[target['callback']]['status'] = 'FINISH_ADD'
        self.inspector.registered = lambda targets: {('/media/show/a.mkv',3)}
        self.recovery.tick({})
        self.assertEqual(len(second.sent),1)
        second.present = True
        self.recovery.tick({})
        self.assertEqual(self.store.results()['counts']['recovered'],1)

    def test_shyni_api_failure_never_becomes_missing(self):
        second = self.secondary()
        second.check = lambda path: (_ for _ in ()).throw(RuntimeError('auth error'))
        with self.assertRaises(RuntimeError):
            self.recovery.tick({})
        self.assertEqual(self.sent,[])
        self.assertEqual(second.sent,[])

    def test_missing_shyni_library_is_not_scanned(self):
        second = self.secondary()
        second.check = lambda path: None
        self.inspector.registered = lambda targets: {('/media/show/a.mkv',3)}
        self.recovery.tick({})
        self.assertEqual(second.sent,[])
        self.assertEqual(self.store.results()['counts']['present'],1)

    def test_review_only_never_submits_followup_shyni_job(self):
        second=self.secondary()
        self.recovery.tick({})
        target=self.store.rows(('queued',))[0]
        self.jobs[target['callback']]['status']='FINISH_ADD'
        self.inspector.registered=lambda targets: {('/media/show/a.mkv',3)}
        self.recovery.tick({'submit':False})
        self.assertEqual(second.sent,[])

    def test_review_only_checks_all_page_candidates(self):
        self.rows.append(dict(self.rows[0],id=2,target='/media/show/b.mkv'))
        self.recovery.tick({'submit':False})
        self.assertEqual(self.sent,[])
        self.assertEqual(self.store.results()['counts']['missing'],2)
        self.recovery.tick({})
        self.assertEqual(len(self.sent),1)

    def test_shared_history_cursor_preserves_other_sections(self):
        self.inspector.roots=lambda:[(3,'/media'),(4,'/other')]
        self.rows.append(dict(self.rows[0],id=2,target='/other/show/b.mkv',target_section_id=4))
        self.recovery.tick({'sections':[3],'submit':False})
        self.assertEqual(self.store.results()['total'],2)
        self.recovery.tick({'sections':[4]})
        self.assertEqual(self.sent,['/other/show/b.mkv'])

    def test_scheduled_queue_coalesces_and_survives_restart(self):
        self.store.request_run('3',[3])
        self.store.request_run('4',[4])
        self.store.request_run('3',[3])
        restored=s.Store(self.store.path)
        self.assertEqual(len(restored.pending_runs()),2)
        self.assertEqual(restored.pop_run()['sections'],[3])
        self.assertEqual(restored.pop_run()['sections'],[4])
        self.assertIsNone(restored.pop_run())

    def test_schedule_disable_only_removes_its_own_pending_request(self):
        self.store.request_run('common',[3])
        self.store.request_run('4',[4])
        self.store.remove_runs('common')
        self.assertEqual(self.store.pending_runs()[0]['sections'],[4])


if __name__ == '__main__':
    unittest.main()


class SQLiteInspectorTests(unittest.TestCase):
    def test_soft_deleted_parts_are_not_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'plex.db')
            with sqlite3.connect(path) as db:
                db.executescript('''CREATE TABLE media_items(id INTEGER, library_section_id INTEGER, deleted_at INTEGER);
                    CREATE TABLE media_parts(media_item_id INTEGER,file TEXT,deleted_at INTEGER);
                    INSERT INTO media_items VALUES(1,3,NULL),(2,3,100);
                    INSERT INTO media_parts VALUES(1,'/media/a.mkv',NULL),(1,'/media/b.mkv',100),(2,'/media/c.mkv',NULL);''')
            inspector=s.Inspector(path,path)
            result=inspector.registered([{'path':'/media/'+x+'.mkv','section':3} for x in 'abc'])
            self.assertEqual(result,{('/media/a.mkv',3)})
