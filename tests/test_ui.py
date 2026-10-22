import os
import tempfile
import time
import unittest
import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from roster_store import RosterStore
from hikvision import HikvisionError


class FakeTerminal:
    def __init__(self, url):
        self.base_url = url
        self.users = {}

    def create_user(self, record):
        self.users[record['employee_no']] = {'employeeNo': record['employee_no'], 'name': record['name']}

    def get_user(self, employee_id):
        return self.users.get(employee_id)

    def search_users(self):
        return list(self.users.values())

    def delete_user(self, employee_id):
        del self.users[employee_id]


class InterfaceTests(unittest.TestCase):
    def test_streamlit_runner_does_not_launch_second_server_and_shows_release(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'ROSTER_DB_PATH': folder+'/roster.db'}), \
                patch('subprocess.call') as relaunch:
            os.environ.pop('_APP_LAUNCHED', None)
            at = AppTest.from_file(str(Path(__file__).parents[1] / 'App.py'), default_timeout=30).run()
            self.assertFalse(at.exception)
            relaunch.assert_not_called()
            self.assertTrue(any('Version 2026.10.22.3' in item.value for item in at.markdown))

    def test_lunch_button_is_on_right_and_only_visible_for_buguruni(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'_APP_LAUNCHED': '1', 'ROSTER_DB_PATH': folder+'/roster.db'}):
            at = AppTest.from_file(str(Path(__file__).parents[1] / 'App.py'), default_timeout=30).run()
            self.assertFalse(at.exception)
            lunch = at.button(key='buguruni_lunch_report')
            self.assertEqual(lunch.label, 'View report with lunch time')
            report_columns = [column for column in at.get('column') if any(
                button.label in ['View / Generate Reports', 'View report with lunch time'] for button in column.button)]
            self.assertEqual([column.button[0].label for column in report_columns],
                             ['View / Generate Reports', 'View report with lunch time'])
            for site_name in ['Puma Upanga', 'Puma Ocean Road', 'Puma Survey', 'India', 'Kibaha', 'Livingstone']:
                at.radio(key='site_selector_row').set_value(site_name).run()
                self.assertFalse(at.exception)
                self.assertFalse(any(button.key == 'buguruni_lunch_report' for button in at.button))
                self.assertIn('View / Generate Reports', [button.label for button in at.button])

    def test_lunch_button_generates_four_clockings_and_six_downloads(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'_APP_LAUNCHED': '1', 'ROSTER_DB_PATH': folder+'/roster.db'}):
            terminal = FakeTerminal('http://fake.test')
            terminal.users = {'102': {'employeeNo': '102', 'name': 'Abdallah Iddi Omary'},
                              '103': {'employeeNo': '103', 'name': 'Abubakary Hussein Sharif'}}
            day = datetime.now(timezone(timedelta(hours=3))).date().isoformat()
            events = [{'employeeNoString': '102', 'name': 'Abdallah Iddi Omary',
                       'time': day+'T'+time+':00+03:00'} for time in ['08:00', '13:00', '14:00', '16:00']]
            # A deleted employee's scans must still be excluded in the new mode.
            events.append({'employeeNoString': '999', 'name': 'Deleted Person', 'time': day+'T08:00:00+03:00'})
            response = type('Response', (), {'status_code': 200, 'json': lambda self: {
                'AcsEvent': {'InfoList': events, 'totalMatches': len(events), 'numOfMatches': len(events)}}})()
            with patch('employee_management.client_for', return_value=terminal), \
                 patch('requests.post', return_value=response) as fetch, \
                 patch('streamlit.download_button', return_value=False) as downloads:
                at = AppTest.from_file(str(Path(__file__).parents[1] / 'App.py'), default_timeout=30).run()
                at.button(key='buguruni_lunch_report').click().run()
                self.assertFalse(at.exception)
                self.assertFalse(at.error)
                self.assertEqual(fetch.call_count, 1)
                metrics = {metric.label: metric.value for metric in at.metric}
                self.assertEqual(metrics['Complete Days'], '1')
                self.assertEqual(metrics['Staff Roster'], '2')
                data = at.dataframe[0].value
                present = data[data['ID'] == '102'].iloc[0]
                self.assertEqual([present[key] for key in ['Check In', 'Lunch Out', 'Lunch In', 'Check Out']],
                                 ['08:00', '13:00', '14:00', '16:00'])
                self.assertEqual(present['Net Hours'], '7h 00m')
                self.assertEqual(data[data['ID'] == '103'].iloc[0]['Status'], 'Absent')
                self.assertNotIn('Deleted Person', data.to_string())
                self.assertEqual(downloads.call_count, 6)
                for call in downloads.call_args_list:
                    self.assertIn('_With_Lunch_', call.kwargs['file_name'])

    def test_three_upanga_aliases_load_in_delete_and_transfer_lists(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'_APP_LAUNCHED': '1', 'ROSTER_DB_PATH': folder+'/roster.db'}):
            terminal = FakeTerminal('http://fake.test')
            terminal.users = {str(index): {'employeeNo': str(index), 'name': name}
                              for index, name in enumerate(['Athumani Y Shebly', 'Hamlat S ghindu', 'Mnyeto M Rashid'], 901)}
            terminal.users.pop('903')
            terminal.users['1365'] = {'employeeNo': '1365', 'name': 'Mnyeto Miraji Rashidi'}
            with patch('employee_management.client_for', return_value=terminal):
                at = AppTest.from_file(str(Path(__file__).parents[1] / 'App.py'), default_timeout=30).run()
                at.session_state['management_account'] = 'HR'
                at.session_state['management_last_seen'] = time.time()
                at.session_state['management_open'] = True
                at.run()
                at.selectbox(key='manage_delete_site').select('Puma Upanga').run()
                at.button(key='manage_delete_Puma Upanga_refresh').click().run()
                for employee_id, user in terminal.users.items():
                    self.assertIn(user['name'], at.checkbox(key='manage_delete_Puma Upanga_tick_'+employee_id).label)
                self.assertEqual(at.checkbox(key='manage_delete_Puma Upanga_tick_1365').label,
                                 'Mnyeto Miraji Rashidi · ID 1365 · Security · SECURITY')
                at.selectbox(key='manage_transfer_source').select('Puma Upanga').run()
                at.button(key='manage_transfer_Puma Upanga_refresh').click().run()
                options = at.selectbox(key='manage_transfer_Puma Upanga_choice').options
                self.assertEqual(len(options), 3)
                for user in terminal.users.values():
                    self.assertTrue(any(user['name'] in option for option in options))
                self.assertFalse(at.exception)

    def test_generated_viewer_and_six_exports_exclude_deleted_staff_after_redeploy(self):
        # Start with no saved overrides, as happens on a cloud replacement.
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'_APP_LAUNCHED': '1', 'ROSTER_DB_PATH': folder+'/roster.db'}):
            terminal = FakeTerminal('http://fake.test')
            terminal.users = {
                '102': {'employeeNo': '102', 'name': 'Abdallah Iddi Omary'},
                '103': {'employeeNo': '103', 'name': 'Abubakary Hussein Sharif'},
            }
            day = (datetime.now(timezone(timedelta(hours=3))).date() - timedelta(days=1)).isoformat()
            events = [{'employeeNoString': employee_id, 'name': name, 'time': day+'T08:00:00+03:00'}
                      for employee_id, name in [('101', 'Ally Mohamedi Sadaka'),
                                                ('999', 'Abdallah Iddi Removed'),
                                                ('102', 'ABDALLAH IDDI OMARY')]]
            response = type('Response', (), {'status_code': 200, 'json': lambda self: {
                'AcsEvent': {'InfoList': events, 'totalMatches': len(events), 'numOfMatches': len(events)}}})()
            with patch('employee_management.client_for', return_value=terminal), \
                 patch('requests.post', return_value=response), \
                 patch('streamlit.download_button', return_value=False) as downloads:
                at = AppTest.from_file(str(Path(__file__).parents[1] / 'App.py'), default_timeout=30).run()
                at.date_input[0].set_value(datetime.fromisoformat(day).date())
                next(b for b in at.button if b.label == 'View / Generate Reports').click().run()
                self.assertFalse(at.exception)
                self.assertFalse(at.error)
                metrics = {m.label: m.value for m in at.metric}
                self.assertEqual(metrics['Staff Roster'], '2')
                self.assertEqual(metrics['Total Records'], '1')
                self.assertEqual(metrics['Unique Staff'], '1')
                rendered = ' '.join(frame.value.to_string() for frame in at.dataframe)
                self.assertIn('Abdallah Iddi Omary', rendered)
                self.assertIn('Abubakary Hussein Sharif', rendered)
                self.assertIn('Absent', rendered)
                self.assertNotIn('Ally Mohamedi Sadaka', rendered)
                self.assertNotIn('Abdallah Iddi Removed', rendered)
                self.assertEqual(downloads.call_count, 6)
                for call in downloads.call_args_list:
                    filename = call.kwargs['file_name']
                    data = call.kwargs['data']
                    data = data.getvalue() if hasattr(data, 'getvalue') else data
                    if filename.endswith('.pdf'):
                        self.assertTrue(data.startswith(b'%PDF'))
                        continue
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        text = ' '.join(archive.read(name).decode('utf-8') for name in archive.namelist() if name.endswith('.xml'))
                    self.assertIn('Abdallah Iddi Omary', text)
                    self.assertIn('Abubakary Hussein Sharif', text)
                    self.assertNotIn('Ally Mohamedi Sadaka', text)
                    self.assertNotIn('Abdallah Iddi Removed', text)

    def test_report_does_not_generate_when_current_employee_list_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'_APP_LAUNCHED': '1', 'ROSTER_DB_PATH': folder+'/roster.db'}):
            with patch('employee_management.client_for') as client_for, \
                 patch('requests.post') as fetch, \
                 patch('streamlit.download_button') as downloads:
                client_for.return_value.search_users.side_effect = HikvisionError('Terminal unavailable')
                at = AppTest.from_file(str(Path(__file__).parents[1] / 'App.py'), default_timeout=30).run()
                next(b for b in at.button if b.label == 'View / Generate Reports').click().run()
                self.assertFalse(at.exception)
                self.assertIn('Could not verify the current employee roster', at.error[0].value)
                fetch.assert_not_called()
                downloads.assert_not_called()

    def test_public_reports_and_employee_add_delete(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'_APP_LAUNCHED': '1', 'ROSTER_DB_PATH': folder+'/roster.db'}):
            terminal = FakeTerminal('http://fake.test')
            with patch('employee_management.client_for', return_value=terminal):
                at = AppTest.from_file(str(Path(__file__).parents[1] / 'App.py'), default_timeout=30).run()
                self.assertFalse(at.exception)
                self.assertFalse(at.tabs)
                self.assertIn('View / Generate Reports', [b.label for b in at.button])
                self.assertIn('login_form', [f.key for f in at.get('form')])
                at.session_state['management_account'] = 'HR'
                at.session_state['management_last_seen'] = time.time()
                at.session_state['management_open'] = True
                at.run()
                self.assertFalse(at.exception)
                self.assertEqual(len(at.tabs), 4)
                at.selectbox(key='manage_add_site').select('Buguruni').run()
                at.selectbox(key='manage_add_department_Buguruni').select('Shop').run()
                at.selectbox(key='manage_add_job_Buguruni_Shop').select('SHOP')
                next(t for t in at.text_input if t.label == 'Employee ID *').set_value('98765')
                next(t for t in at.text_input if t.label == 'Full name *').set_value('UI Test Person')
                next(c for c in at.checkbox if c.label.startswith('I confirm this person')).check()
                next(b for b in at.button if b.label == 'Add employee').click().run()
                self.assertFalse(at.exception)
                self.assertIn('98765', terminal.users)
                terminal.users['99999'] = {'employeeNo': '99999', 'name': 'Unlisted Terminal Person'}
                self.assertEqual(RosterStore(folder+'/roster.db').history()[0]['action'], 'add')
                at.selectbox(key='manage_delete_site').select('Buguruni').run()
                at.button(key='manage_delete_Buguruni_refresh').click().run()
                self.assertEqual(at.checkbox(key='manage_delete_Buguruni_tick_98765').label, 'UI Test Person · ID 98765 · Shop · SHOP')
                self.assertFalse(any(c.key == 'manage_delete_Buguruni_tick_99999' for c in at.checkbox))
                at.selectbox(key='manage_transfer_source').select('Buguruni').run()
                at.button(key='manage_transfer_Buguruni_refresh').click().run()
                self.assertEqual(at.selectbox(key='manage_transfer_Buguruni_choice').options, ['98765 · UI Test Person · Shop · SHOP'])
                at.checkbox(key='manage_delete_Buguruni_tick_98765').check().run()
                at.button(key='manage_delete_Buguruni_submit').click().run()
                self.assertFalse(at.exception)
                self.assertNotIn('98765', terminal.users)
                self.assertIn('99999', terminal.users)
                self.assertEqual(RosterStore(folder+'/roster.db').history()[0]['action'], 'delete')
                next(b for b in at.button if b.label == 'Log out').click().run()
                self.assertFalse(at.exception)
                self.assertFalse(at.tabs)
                self.assertIn('View / Generate Reports', [b.label for b in at.button])

    def test_create_site_department_and_add_employee_with_job(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'_APP_LAUNCHED': '1', 'ROSTER_DB_PATH': folder+'/roster.db'}):
            terminal = FakeTerminal('http://fake.test')
            with patch('employee_management.client_for', return_value=terminal):
                at = AppTest.from_file(str(Path(__file__).parents[1] / 'App.py'), default_timeout=30).run()
                at.session_state['management_account'] = 'HR'
                at.session_state['management_last_seen'] = time.time()
                at.session_state['management_open'] = True
                at.run()
                at.selectbox(key='manage_add_site').select('Buguruni').run()
                at.text_input(key='manage_add_department_Buguruni_new_name').set_value('Accounts')
                at.text_input(key='manage_add_department_Buguruni_new_job').set_value('Cashier')
                at.button(key='manage_add_department_Buguruni_create').click().run()
                self.assertFalse(at.exception)
                self.assertIn('Accounts', at.selectbox(key='manage_add_department_Buguruni').options)
                at.selectbox(key='manage_add_department_Buguruni').select('Accounts').run()
                self.assertEqual(at.selectbox(key='manage_add_job_Buguruni_Accounts').options, ['Cashier', 'Add a new job title…'])
                at.selectbox(key='manage_add_job_Buguruni_Accounts').select('Add a new job title…').run()
                at.text_input(key='manage_add_job_Buguruni_Accounts_new').set_value('Senior Cashier')
                next(t for t in at.text_input if t.label == 'Employee ID *').set_value('202')
                next(t for t in at.text_input if t.label == 'Full name *').set_value('New Department Person')
                next(c for c in at.checkbox if c.label.startswith('I confirm this person')).check()
                next(b for b in at.button if b.label == 'Add employee').click().run()
                self.assertFalse(at.exception)
                self.assertIn('202', terminal.users)
                sites = {n: {'employees': {}, 'dept_order': [], 'dept_colors': {}} for n in ['Buguruni', 'Puma Ocean Road']}
                RosterStore(folder+'/roster.db').apply(sites)
                self.assertEqual(sites['Buguruni']['employees']['Accounts']['member_jobs']['New Department Person'], 'Senior Cashier')
                self.assertNotIn('Accounts', sites['Puma Ocean Road']['employees'])
                at.selectbox(key='manage_add_site').select('Puma Ocean Road').run()
                self.assertNotIn('Accounts', at.selectbox(key='manage_add_department_Puma Ocean Road').options)
                self.assertFalse(at.exception)

    def test_delete_checkbox_list_removes_multiple_checked_people_only(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'_APP_LAUNCHED': '1', 'ROSTER_DB_PATH': folder+'/roster.db'}):
            terminal = FakeTerminal('http://fake.test')
            store = RosterStore(folder+'/roster.db')
            for employee_id, name in [('301', 'Checked One'), ('302', 'Checked Two'), ('303', 'Unchecked Person')]:
                terminal.users[employee_id] = {'employeeNo': employee_id, 'name': name}
                store.save('HR', 'add', employee_id, name, destination='Buguruni', department='Shop', job='SHOP')
            terminal.users['999'] = {'employeeNo': '999', 'name': 'Not In Report'}
            with patch('employee_management.client_for', return_value=terminal):
                at = AppTest.from_file(str(Path(__file__).parents[1] / 'App.py'), default_timeout=30).run()
                at.session_state['management_account'] = 'HR'
                at.session_state['management_last_seen'] = time.time()
                at.session_state['management_open'] = True
                at.run()
                at.selectbox(key='manage_delete_site').select('Buguruni').run()
                at.button(key='manage_delete_Buguruni_refresh').click().run()
                self.assertTrue(at.button(key='manage_delete_Buguruni_submit').disabled)
                self.assertFalse(any(s.key == 'manage_delete_Buguruni_choice' for s in at.selectbox))
                self.assertEqual(len([c for c in at.checkbox if c.key and c.key.startswith('manage_delete_Buguruni_tick_')]), 3)
                at.checkbox(key='manage_delete_Buguruni_tick_301').check()
                at.checkbox(key='manage_delete_Buguruni_tick_302').check().run()
                self.assertFalse(at.button(key='manage_delete_Buguruni_submit').disabled)
                at.button(key='manage_delete_Buguruni_submit').click().run()
                self.assertFalse(at.exception)
                self.assertEqual(set(terminal.users), {'303', '999'})
                self.assertTrue(any('Checked One' in message.value and 'Checked Two' in message.value for message in at.success))
                self.assertFalse(at.checkbox(key='manage_delete_Buguruni_tick_303').value)
                self.assertTrue(at.button(key='manage_delete_Buguruni_submit').disabled)


if __name__ == '__main__':
    unittest.main()
