"""Regression checks for FF's footer-loaded command helpers."""
from pathlib import Path
import unittest
from jinja2 import DictLoader, Environment


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parents[1] / 'templates'

    def test_initial_queries_wait_for_footer_scripts(self):
        schedule = (self.root / 'ff_plex_scan_recovery_main_schedule.html').read_text()
        status = (self.root / 'ff_plex_scan_recovery_main_status.html').read_text()
        self.assertIn('$(document).ready(refresh);', schedule)
        self.assertIn('$(document).ready(function(){refresh();', status)

    def test_saved_schedules_render_without_ajax(self):
        name = 'ff_plex_scan_recovery_main_schedule.html'
        env = Environment(loader=DictLoader({
            'base.html': '{% block content %}{% endblock %}',
            name: (self.root / name).read_text(),
        }), autoescape=True)
        class Macros:
            def setting_input_text(self, *args, **kwargs): return ''
            def m_button_group(self, *args, **kwargs): return ''
        jobs = [dict(section_id=i, name='섹션<%d>' % i,
                     schedule='0 3 * * *', enabled=True) for i in range(1, 30)]
        html = env.get_template(name).render(macros=Macros(), arg=dict(
            error='', section_jobs=jobs, sections_list=[], sections='', schedule='*/5 * * * *'))
        self.assertEqual(html.count('예약 활성 · 등록 확인 중'), 29)
        self.assertIn('섹션&lt;29&gt;', html)
