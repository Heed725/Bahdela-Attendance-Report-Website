"""Regression cases for the three reported Upanga terminal names."""
import ast
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from employee_management import filter_report_users, delete_employees, prepare_transfer, finish_transfer
from hikvision import ApiResult
from report_roster import current_report_events, current_report_roster
from roster_store import RosterStore


def upanga_site():
    tree = ast.parse((Path(__file__).parents[1] / 'App.py').read_text())
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'SITES' for target in node.targets))
    return ast.literal_eval(assignment.value)['Puma Upanga']


class UpangaNameTests(unittest.TestCase):
    def setUp(self):
        self.site = upanga_site()
        self.sites = {'Puma Upanga': self.site}
        self.users = [
            {'employeeNo': 'test901', 'name': 'Athumani Y Shebly'},
            {'employeeNo': 'test902', 'name': 'Hamlat S ghindu'},
            {'employeeNo': '1365', 'name': 'Mnyeto M Rashid'},
        ]
        self.canonical = ['Athumani Yusuph Sheby', 'Hamlati Shaibu Ghindu', 'Mnyeto Miraji Rashidi']

    def test_all_three_load_in_their_correct_upanga_departments(self):
        before = copy.deepcopy(self.sites)
        before['Puma Upanga']['employees']['Oil'].pop('member_aliases')
        before['Puma Upanga']['employees']['Security'].pop('member_aliases')
        self.assertEqual(filter_report_users(self.users, 'Puma Upanga', before), [])
        matches = filter_report_users(self.users, 'Puma Upanga', self.sites)
        self.assertEqual({row['name'] for row in matches}, {row['name'] for row in self.users})
        self.assertEqual({row['roster_name'] for row in matches}, set(self.canonical))
        self.assertEqual({row['name']: row['department'] for row in matches}, {
            'Athumani Y Shebly': 'Oil', 'Hamlat S ghindu': 'Oil', 'Mnyeto M Rashid': 'Security'})

    def test_screenshot_full_name_and_id_load_once_in_security(self):
        user = {'employeeNo': '1365', 'name': 'Mnyeto Miraji Rashidi'}
        matches = filter_report_users([user], 'Puma Upanga', self.sites)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['department'], 'Security')
        assignments = [key for key, dept in self.site['employees'].items() if user['name'] in dept['members']]
        self.assertEqual(assignments, ['Security'])
        current = current_report_roster(self.site, [user])
        self.assertEqual(current['employees']['Security']['member_ids'][user['name']], '1365')
        self.assertNotIn(user['name'], current['employees']['Oil']['members'])
        self.assertEqual(filter_report_users([{**user, 'employeeNo': 'wrong-id'}], 'Puma Upanga', self.sites), [])

    def test_matching_is_case_insensitive_and_scoped_to_upanga(self):
        users = [{**user, 'name': '  '+user['name'].upper().replace(' ', '  ')+'  '} for user in self.users]
        self.assertEqual(len(filter_report_users(users, 'Puma Upanga', self.sites)), 3)
        other = copy.deepcopy(self.site)
        other['employees']['Oil'].pop('member_aliases')
        other['employees']['Security'].pop('member_aliases')
        self.assertEqual(filter_report_users(self.users, 'Other', {'Other': other}), [])

    def test_alias_needs_unique_user_and_cannot_override_a_known_id(self):
        duplicate = {**self.users[0], 'employeeNo': 'test999', 'name': self.canonical[0]}
        matches = filter_report_users(self.users+[duplicate], 'Puma Upanga', self.sites)
        self.assertNotIn(self.canonical[0], [row['roster_name'] for row in matches])
        self.site['employees']['Oil']['member_ids'] = {self.canonical[0]: 'correct-id'}
        self.assertNotIn(self.canonical[0], [row['roster_name'] for row in
                         filter_report_users(self.users, 'Puma Upanga', self.sites)])

    def test_report_includes_all_three_aliases_by_current_id(self):
        current = current_report_roster(self.site, self.users)
        self.assertEqual(current['employees']['Oil']['members'], self.canonical[:2])
        self.assertEqual(current['employees']['Security']['members'], self.canonical[2:])
        events = [{'employeeNoString': user['employeeNo'], 'name': user['name']} for user in self.users]
        events.append({'employeeNoString': 'test999', 'name': self.users[0]['name']})
        filtered = current_report_events(events, current)
        self.assertEqual([event['name'] for event in filtered], self.canonical)

    def test_delete_alias_removes_original_roster_and_cannot_reappear(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RosterStore(Path(folder) / 'roster.db')
            state = {'management_account': 'HR', 'management_last_seen': time.time()}
            client = MagicMock()
            client.get_user.side_effect = lambda employee_id: next(user for user in self.users if user['employeeNo'] == employee_id)
            results = delete_employees(state, client, store, self.users, 'Puma Upanga', self.sites)
            self.assertEqual(results['failed'], [])
            self.assertEqual(len(results['deleted']), 3)
            fresh = {'Puma Upanga': upanga_site()}
            store.apply(fresh)
            self.assertTrue(set(self.canonical).isdisjoint(fresh['Puma Upanga']['employees']['Oil']['members']))
            self.assertNotIn(self.canonical[2], fresh['Puma Upanga']['employees']['Security']['members'])
            self.assertEqual(filter_report_users(self.users, 'Puma Upanga', fresh), [])
            self.assertEqual(current_report_roster(fresh['Puma Upanga'], self.users)['employees']['Oil']['members'], [])

    def test_completed_alias_transfer_removes_source_canonical_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RosterStore(Path(folder) / 'roster.db')
            state = {'management_account': 'HR', 'management_last_seen': time.time()}
            self.sites['Other'] = {'employees': {'Shop': {'members': []}}, 'dept_order': ['Shop']}
            source = MagicMock(base_url='http://source.test')
            target = MagicMock(base_url='http://target.test')
            live = {**self.users[0], 'numOfCard': 0, 'numOfFP': 0, 'numOfFace': 0}
            source.get_user.return_value = live
            target.get_user.side_effect = [None, live, live]
            target._request.return_value = ApiResult(True, 200, {'statusCode': 1})
            pending = prepare_transfer(state, source, target, store, self.users[0], 'Puma Upanga', 'Other', 'Shop', self.sites)
            target.get_user.side_effect = None
            target.get_user.return_value = live
            finish_transfer(state, source, target, store, pending, self.sites)
            store.apply(self.sites)
            self.assertNotIn(self.canonical[0], self.site['employees']['Oil']['members'])
            self.assertEqual(self.sites['Other']['employees']['Shop']['members'], [self.users[0]['name']])
            self.assertEqual(filter_report_users([self.users[0]], 'Puma Upanga', self.sites), [])

    def test_saved_alias_placement_does_not_duplicate_original_member(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RosterStore(Path(folder) / 'roster.db')
            user = self.users[0]
            store.save('HR', 'prepare transfer', user['employeeNo'], user['name'],
                       destination='Puma Upanga', department='Oil')
            store.apply(self.sites)
            members = self.site['employees']['Oil']['members']
            self.assertNotIn(self.canonical[0], members)
            self.assertEqual(members.count(user['name']), 1)
            self.assertEqual(len(filter_report_users([user], 'Puma Upanga', self.sites)), 1)
            current = current_report_roster(self.site, [user])
            # Existing full-name scans and abbreviated scans belong to one ID.
            events = [{'employeeNoString': user['employeeNo'], 'name': name}
                      for name in [self.canonical[0], user['name']]]
            self.assertEqual(len(current_report_events(events, current)), 2)


if __name__ == '__main__':
    unittest.main()
