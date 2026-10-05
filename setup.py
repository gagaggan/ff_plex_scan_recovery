from plugin import *

setting = {
    'filepath': __file__, 'use_db': True, 'use_default_setting': True,
    'home_module': 'main', 'setting_menu': None, 'default_route': 'normal',
    'menu': {'uri': __package__, 'name': 'Plex Scan Recovery', 'list': [
        {'uri': 'main', 'name': '누락 스캔 복구', 'list': [
            {'uri': 'setting', 'name': '설정'},
            {'uri': 'schedule', 'name': '스케줄'},
            {'uri': 'status', 'name': '상태'}]},
        {'uri': 'log', 'name': '로그'}]}}
P = create_plugin_instance(setting)
from .logic import Logic
P.set_module_list([Logic])
