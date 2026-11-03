import unittest

from employee_lookup import find_terminal_employees


class Terminal:
    def __init__(self, users=None, error=None):
        self.users, self.error = users or [], error

    def search_users(self):
        if self.error:
            raise self.error
        return self.users


class EmployeeLookupTests(unittest.TestCase):
    def test_searches_unrostered_accounts_and_isolates_failed_terminals(self):
        matches, statuses = find_terminal_employees({
            'Buguruni': Terminal([{'employeeNo': '9999', 'name': 'HARUNI Kenedy Saimoni'}]),
            'Ocean': Terminal([{'employeeNo': '1652', 'name': 'Different Name'}]),
            'Offline': Terminal(error=OSError('No route to host')),
        }, 'Haruni, 1652')
        self.assertEqual([m['Station'] for m in matches], ['Buguruni', 'Ocean'])
        self.assertIn('Could not check', statuses[2]['Status'])
        self.assertEqual(statuses[0]['Records checked'], 1)

    def test_numeric_ids_are_exact_and_empty_query_is_rejected(self):
        matches, _ = find_terminal_employees({'A': Terminal([
            {'employeeNo': '16520', 'name': 'Person 1652'},
        ])}, '1652')
        self.assertEqual(matches, [])
        with self.assertRaises(ValueError):
            find_terminal_employees({}, ' , ')

    def test_reports_enrollment_metadata_without_credentials(self):
        matches, _ = find_terminal_employees({'Ocean': Terminal([{
            'employeeNo': '1652', 'name': 'Haruni', 'numOfFP': 1,
            'numOfFace': 1, 'numOfCard': 0, 'userType': 'normal',
            'Valid': {'enable': True, 'beginTime': '2026-01-01', 'endTime': '2036-12-31'},
            'doorRight': '1', 'password': 'must not be returned',
        }])}, '1652')
        self.assertEqual(matches[0]['Fingerprints'], 1)
        self.assertEqual(matches[0]['Valid until'], '2036-12-31')
        self.assertNotIn('password', matches[0])
