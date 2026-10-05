"""Check FF wiring in a subprocess so framework stubs cannot leak into other tests."""
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


class WiringTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('flask'), 'FF wiring test requires Flask (run inside FF)')
    def test_scheduler_settings_and_plex_only_queue_item(self):
        code = r'''
import importlib, sys, types, tempfile, pathlib
from flask import Flask
from unittest.mock import Mock
repo=pathlib.Path(sys.argv[1])
package=types.ModuleType('ff_plex_scan_recovery');package.__path__=[str(repo)]
sys.modules['ff_plex_scan_recovery']=package
app=Flask('test')
framework=types.ModuleType('framework');framework.F=Mock();framework.F.app=app
framework.F.scheduler.is_include.return_value=False
tmp=tempfile.TemporaryDirectory();pathlib.Path(tmp.name,'db').mkdir();framework.path_data=tmp.name
sys.modules['framework']=framework
plugin=types.ModuleType('plugin')
class Base:
    def __init__(self,*args,**kwargs):pass
plugin.PluginModuleBase=Base;plugin.Job=Mock()
sys.modules['plugin']=plugin
setup=types.ModuleType('ff_plex_scan_recovery.setup');setup.P=Mock();setup.P.package_name='ff_plex_scan_recovery'
sys.modules[setup.__name__]=setup
module=importlib.import_module('ff_plex_scan_recovery.logic')
values=dict(module.Logic.db_default)
setup.P.ModelSetting.get.side_effect=lambda key:values.get(key)
setup.P.ModelSetting.set.side_effect=lambda key,value:values.update({key:value})
logic=module.Logic(setup.P);logic.plugin_load()
assert not framework.F.scheduler.add_job_instance.called
values['enabled']='True';logic.sync_schedule()
assert framework.F.scheduler.add_job_instance.call_count==1
assert logic.options()['max_attempts']==3
item=types.SimpleNamespace(id=9,shyni_status=None,save=Mock())
model=Mock(return_value=item)
mate=Mock();mate.get_module.return_value.web_list_model=model
logic._mate=lambda:mate
assert logic._enqueue('/media/a.mkv',3,'callback')==9
assert item.shyni_status=='SKIP' and item.save.called
logic._inspector=lambda:types.SimpleNamespace(sections=lambda:[{'id':3,'name':'Drama'},{'id':4,'name':'Shows'},{'id':5,'name':'Documentary'}])
with app.app_context():
    result=logic.process_command('save_section_schedule','3','0 3 * * *','true',None).get_json()
    assert result['ret']=='success'
    result=logic.process_command('save_section_schedule','4','*/30 * * * *','true',None).get_json()
    assert result['ret']=='success'
assert [j['section_id'] for j in logic.section_jobs()]==[3,4]
assert plugin.Job.call_args_list[-1].kwargs['args']==(4,)
assert logic.section_jobs()[0]['schedule']=='0 3 * * *'
before=values['section_schedules']
with app.app_context():
    assert logic.process_command('save_section_schedule','3','invalid','true',None).get_json()['ret']=='danger'
assert values['section_schedules']==before
logic.start_pending=Mock()
logic.scheduler_function()
logic.scheduler_function(3)
assert [r['sections'] for r in logic._store().pending_runs()]==[[5],[3]]
with app.app_context():
    logic.process_command('save_section_schedule','3','0 3 * * *','false',None)
assert [r['sections'] for r in logic._store().pending_runs()]==[[5]]
assert not logic.section_jobs()[0]['enabled']
with app.app_context():
    logic.process_command('delete_section_schedule','4',None,None,None)
assert [j['section_id'] for j in logic.section_jobs()]==[3]
logic.stop.set();tmp.cleanup()
'''
        result = subprocess.run([sys.executable,'-c',code,str(Path(__file__).parents[1])],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
