import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from employee_management import filter_report_users, delete_employee, prepare_transfer, delete_employees
from hikvision import HikvisionError
from roster_store import RosterStore


class ReportRosterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = RosterStore(Path(self.directory.name) / 'roster.db')
        self.sites = {site: {'employees': {'Shop': {'label': 'SHOP', 'members': ['Report Person'],
                      'member_ids': {'Report Person': '101'}}}, 'dept_order': ['Shop'], 'dept_colors': {}}
                      for site in ['A', 'B']}
        self.state = {'management_account': 'HR', 'management_last_seen': time.time()}
        self.person = {'name': 'Report Person', 'employeeNo': '101'}

    def test_only_report_members_and_correct_ids(self):
        users = [self.person, {'name': 'Unlisted Terminal Person', 'employeeNo': '999'},
                 {'name': 'Report Person', 'employeeNo': 'wrong'}, {'name': 'Report Person Extra', 'employeeNo': '888'}]
        filtered = filter_report_users(users, 'A', self.sites)
        self.assertEqual([u['employeeNo'] for u in filtered], ['101'])
        self.assertEqual(filtered[0]['department'], 'Shop')

    def test_idless_original_member_uses_exact_name(self):
        self.sites['A']['employees']['Shop']['member_ids'] = {}
        self.assertEqual(len(filter_report_users([self.person], 'A', self.sites)), 1)
        self.assertEqual(filter_report_users([{'name': 'Report Person Junior', 'employeeNo': '102'}], 'A', self.sites), [])
        self.assertEqual(filter_report_users([self.person, {**self.person, 'employeeNo': '102'}], 'A', self.sites), [])

    def test_visitors_do_not_become_source_roster_members(self):
        self.sites['A']['employees']['Shop']['visitor_only'] = True
        self.assertEqual(filter_report_users([self.person], 'A', self.sites), [])

    def test_unlisted_employee_cannot_be_deleted_or_transferred(self):
        client = MagicMock()
        outsider = {'name': 'Unlisted Person', 'employeeNo': '999'}
        with self.assertRaises(HikvisionError):
            delete_employee(self.state, client, self.store, outsider, 'A', sites=self.sites)
        with self.assertRaises(HikvisionError):
            prepare_transfer(self.state, client, MagicMock(), self.store, outsider, 'A', 'B', 'Shop', sites=self.sites)
        client.delete_user.assert_not_called()
        client.get_user.assert_not_called()

    def test_roster_deleted_in_another_session_cannot_be_selected(self):
        self.store.save('admin', 'delete', '101', 'Report Person', source='A')
        client = MagicMock()
        with self.assertRaises(HikvisionError):
            delete_employee(self.state, client, self.store, self.person, 'A', sites=self.sites)
        client.delete_user.assert_not_called()

    def test_new_department_and_job_persist_at_selected_site(self):
        self.store.create_department('HR', 'A', 'Accounts', 'Cashier', 'Shift: 08:00 - 17:00', '#336699')
        self.store.save('HR', 'add', '202', 'New Person', destination='A', department='Accounts', job='Senior Cashier')
        fresh = copy.deepcopy(self.sites)
        RosterStore(self.store.path).apply(fresh)
        dept = fresh['A']['employees']['Accounts']
        self.assertEqual(dept['member_jobs']['New Person'], 'Senior Cashier')
        self.assertEqual(dept['member_ids']['New Person'], '202')
        self.assertEqual(dept['jobs'], ['Cashier', 'Senior Cashier'])
        self.assertEqual(fresh['A']['dept_colors']['Accounts'], '#336699')
        self.assertIn('Accounts', fresh['A']['dept_order'])
        self.assertNotIn('Accounts', fresh['B']['employees'])
        with self.assertRaises(ValueError):
            self.store.create_department('HR', 'A', ' accounts ', 'Clerk', 'Shift: 08:00 - 17:00', '#336699')

    def test_old_database_records_remain_usable(self):
        self.store.save('HR', 'add', '101', 'Report Person', destination='A', department='Shop')
        self.store.apply(self.sites)
        self.assertEqual(filter_report_users([self.person], 'A', self.sites)[0]['job'], 'SHOP')

    def test_bulk_delete_reports_partial_failure_and_preserves_failed_employee(self):
        second = {'name': 'Second Person', 'employeeNo': '102'}
        self.sites['A']['employees']['Shop']['members'].append(second['name'])
        self.sites['A']['employees']['Shop']['member_ids'][second['name']] = second['employeeNo']
        client = MagicMock()
        client.get_user.side_effect = lambda employee_id: {'101': self.person, '102': second}[employee_id]
        client.delete_user.side_effect = lambda employee_id: None if employee_id == '101' else (_ for _ in ()).throw(HikvisionError('Device rejected deletion'))
        results = delete_employees(self.state, client, self.store, [self.person, second], 'A', self.sites)
        self.assertEqual([u['employeeNo'] for u in results['deleted']], ['101'])
        self.assertEqual(results['failed'][0]['employee']['employeeNo'], '102')
        self.store.apply(self.sites)
        self.assertEqual(self.sites['A']['employees']['Shop']['members'], ['Second Person'])
        self.assertEqual(len(self.store.history()), 1)

    def test_bulk_delete_empty_selection_never_contacts_device(self):
        client = MagicMock()
        self.assertEqual(delete_employees(self.state, client, self.store, [], 'A', self.sites), {'deleted': [], 'failed': []})
        client.get_user.assert_not_called()
        client.delete_user.assert_not_called()
