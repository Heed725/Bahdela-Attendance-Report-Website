import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from employee_management import report_site_for
from hikvision import HikvisionError
from report_roster import current_report_events, current_report_roster
from roster_store import RosterStore


class CurrentReportTests(unittest.TestCase):
    def setUp(self):
        self.site = {'employees': {'Shop': {
            'members': ['Deleted Person', 'Current Person', 'Absent Person'],
            'member_ids': {'Deleted Person': '101', 'Current Person': '102', 'Absent Person': '103'},
        }}, 'dept_order': ['Shop'], 'dept_colors': {'Shop': '#123456'}}
        self.users = [{'employeeNo': '102', 'name': 'Current Person'},
                      {'employeeNo': '103', 'name': 'Absent Person'}]

    def test_missing_terminal_account_stays_out_after_local_override_is_lost(self):
        original = copy.deepcopy(self.site)
        current = current_report_roster(self.site, self.users)
        self.assertEqual(current['employees']['Shop']['members'], ['Current Person', 'Absent Person'])
        self.assertEqual(self.site, original)

    def test_old_scans_cannot_restore_deleted_user_or_affect_counts(self):
        current = current_report_roster(self.site, self.users)
        events = [{'employeeNoString': '101', 'name': 'Deleted Person'},
                  {'employeeNoString': '102', 'name': 'CURRENT   PERSON'},
                  {'employeeNoString': '999', 'name': 'Current Person'}]
        filtered = current_report_events(events, current)
        self.assertEqual(filtered, [{'employeeNoString': '102', 'name': 'Current Person'}])
        self.assertEqual(events[1]['name'], 'CURRENT   PERSON')

    def test_scan_with_same_first_two_names_cannot_impersonate_current_user(self):
        self.site['employees']['Shop'] = {'members': ['Same Two Current']}
        current = current_report_roster(self.site, [{'employeeNo': '200', 'name': 'Same Two Current'}])
        self.assertEqual(current_report_events([
            {'employeeNoString': '199', 'name': 'Same Two Deleted'}], current), [])

    def test_reused_id_does_not_import_deleted_persons_old_named_scans(self):
        current = current_report_roster(self.site, self.users)
        self.assertEqual(current_report_events([
            {'employeeNoString': '102', 'name': 'Deleted Person'}], current), [])
        self.assertEqual(current_report_events([
            {'employeeNoString': '102'}], current), [{'employeeNoString': '102', 'name': 'Current Person'}])

    def test_current_absent_employee_is_retained_without_any_scans(self):
        current = current_report_roster(self.site, self.users)
        self.assertIn('Absent Person', current['employees']['Shop']['members'])
        self.assertEqual(current_report_events([], current), [])

    def test_idless_static_roster_uses_normalized_exact_full_name(self):
        self.site['employees']['Shop']['member_ids'] = {}
        current = current_report_roster(self.site, [
            {'employeeNo': '102', 'name': ' current  PERSON '},
            {'employeeNo': '999', 'name': 'Deleted Person Junior'}])
        self.assertEqual(current['employees']['Shop']['members'], ['Current Person'])
        self.assertEqual(current['employees']['Shop']['member_ids'], {'Current Person': '102'})

    def test_duplicate_full_names_need_configured_id(self):
        self.site['employees']['Shop']['member_ids'] = {}
        users = [self.users[0], {**self.users[0], 'employeeNo': '999'}]
        self.assertEqual(current_report_roster(self.site, users)['employees']['Shop']['members'], [])
        self.site['employees']['Shop']['member_ids'] = {'Current Person': '102'}
        self.assertEqual(current_report_roster(self.site, users)['employees']['Shop']['members'], ['Current Person'])

    def test_known_id_does_not_attach_a_different_person_with_same_name(self):
        current = current_report_roster(self.site, [{'employeeNo': '999', 'name': 'Current Person'}])
        self.assertEqual(current['employees']['Shop']['members'], [])

    def test_visiting_manager_keeps_cross_site_journey(self):
        self.site['employees']['Visiting'] = {'visitor_only': True, 'members': ['Mobile Manager']}
        current = current_report_roster(self.site, self.users)
        events = [{'employeeNoString': str(i), 'name': 'Mobile Manager', '_site_name': site}
                  for i, site in [(501, 'A'), (777, 'B')]]
        self.assertEqual(current_report_events(events, current, 'Mobile Manager'), events)
        current['employees']['Visiting']['members'] = []
        self.assertEqual(current_report_events(events, current, 'Mobile Manager'), [])

    def test_cross_session_saved_deletion_and_transfer_are_applied_before_search(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RosterStore(Path(folder) / 'roster.db')
            sites = {'A': copy.deepcopy(self.site), 'B': copy.deepcopy(self.site)}
            store.save('HR', 'transfer', '102', 'Current Person', source='A', destination='B', department='Shop')
            with patch('employee_management.client_for') as client_for:
                client_for.return_value.search_users.return_value = self.users
                source = report_site_for('A', sites, store)
                target = report_site_for('B', sites, store)
            self.assertNotIn('Current Person', source['employees']['Shop']['members'])
            self.assertIn('Current Person', target['employees']['Shop']['members'])

    def test_failed_terminal_search_does_not_mutate_or_empty_saved_roster(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RosterStore(Path(folder) / 'roster.db')
            original = copy.deepcopy(self.site)
            with patch('employee_management.client_for') as client_for:
                client_for.return_value.search_users.side_effect = HikvisionError('Unavailable')
                with self.assertRaises(HikvisionError):
                    report_site_for('A', {'A': self.site}, store)
            self.assertEqual(self.site, original)
            self.assertEqual(store.history(), [])


if __name__ == '__main__':
    unittest.main()
