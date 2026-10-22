import copy
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from admin_auth import DEFAULT_ACCOUNTS, login, logout, active_account, require_permission, password_hash
from employee_management import add_employee, delete_employee, prepare_transfer, finish_transfer, filter_report_users
from hikvision import HikvisionClient, HikvisionError, ApiResult
from roster_store import RosterStore

TEST_HASH = password_hash('test-password', '11' * 16)
EMPLOYEE = {'employeeNo': '123', 'name': 'Test Person', 'Valid': {'enable': True}, 'doorRight': '1',
            'numOfCard': 0, 'numOfFP': 0, 'numOfFace': 0}


class LoginTests(unittest.TestCase):
    def test_requested_accounts(self):
        self.assertEqual(set(DEFAULT_ACCOUNTS), {'Kanza', 'HR', 'admin'})
        for name in DEFAULT_ACCOUNTS:
            password = 'test-password'
            state = {}
            self.assertTrue(login(state, name, password, {name: TEST_HASH})[0])
            self.assertEqual(require_permission(state, 'delete'), name)
            self.assertFalse(login({}, name, 'wrong-password', {name: TEST_HASH})[0])

    def test_unknown_and_wrong_password(self):
        self.assertFalse(login({}, 'unknown', 'x')[0])
        self.assertFalse(login({}, 'admin', 'x')[0])

    def test_expiry_clears_sensitive_workflow(self):
        state = {'management_account': 'admin', 'management_last_seen': 1, 'manage_photo': b'x', 'management_transfer': EMPLOYEE}
        self.assertIsNone(active_account(state, now=2000))
        self.assertEqual(state, {})

    def test_cooldown(self):
        state = {}
        for _ in range(5):
            login(state, 'admin', 'wrong', now=1)
        self.assertFalse(login(state, 'admin', 'test-password', {'admin': TEST_HASH}, now=30)[0])
        self.assertTrue(login(state, 'admin', 'test-password', {'admin': TEST_HASH}, now=62)[0])


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RosterStore(Path(self.temp.name) / 'roster.db')
        self.state = {'management_account': 'HR', 'management_last_seen': time.time()}
        self.sites = {n: {'employees': {'Shop': {'members': ['Test Person'] if n == 'A' else []}}, 'dept_order': ['Shop']} for n in ['A', 'B']}
        self.source = MagicMock(base_url='http://source')
        self.target = MagicMock(base_url='http://target')
        self.source.get_user.return_value = dict(EMPLOYEE)
        self.target.get_user.side_effect = [None, dict(EMPLOYEE), dict(EMPLOYEE)]
        self.target._request.return_value = ApiResult(True, 200, {'statusCode': 1})

    def test_all_mutations_require_login(self):
        for operation, args in [(add_employee, (self.source, self.store, {}, 'A', 'Shop')),
                                (delete_employee, (self.source, self.store, EMPLOYEE, 'A', self.sites)),
                                (prepare_transfer, (self.source, self.target, self.store, EMPLOYEE, 'A', 'B', 'Shop', self.sites))]:
            with self.assertRaises(PermissionError):
                operation({}, *args)
        self.source.delete_user.assert_not_called()
        self.target._request.assert_not_called()

    def test_delete_stale_identity_is_rejected(self):
        self.source.get_user.return_value = {**EMPLOYEE, 'name': 'Other Person'}
        with self.assertRaises(HikvisionError):
            delete_employee(self.state, self.source, self.store, EMPLOYEE, 'A', sites=self.sites)
        self.source.delete_user.assert_not_called()

    def test_delete_device_failure_does_not_change_roster(self):
        self.source.delete_user.side_effect = HikvisionError('Rejected')
        with self.assertRaises(HikvisionError):
            delete_employee(self.state, self.source, self.store, EMPLOYEE, 'A', sites=self.sites)
        self.assertEqual(self.store.history(), [])

    def test_duplicate_destination_never_deletes_source(self):
        self.target.get_user.side_effect = None
        self.target.get_user.return_value = dict(EMPLOYEE)
        with self.assertRaises(HikvisionError):
            prepare_transfer(self.state, self.source, self.target, self.store, EMPLOYEE, 'A', 'B', 'Shop', sites=self.sites)
        self.source.delete_user.assert_not_called()
        self.target._request.assert_not_called()

    def test_failed_destination_never_deletes_source(self):
        self.target._request.return_value = ApiResult(False, 400, {'statusCode': 4})
        with self.assertRaises(HikvisionError):
            prepare_transfer(self.state, self.source, self.target, self.store, EMPLOYEE, 'A', 'B', 'Shop', sites=self.sites)
        self.source.delete_user.assert_not_called()
        self.assertEqual(self.store.history(), [])

    def test_transfer_in_two_steps_and_roster_persists(self):
        pending = prepare_transfer(self.state, self.source, self.target, self.store, EMPLOYEE, 'A', 'B', 'Shop', sites=self.sites)
        self.target._request.assert_called_once_with(
            'POST', '/ISAPI/AccessControl/UserInfo/Record?format=json',
            json={'UserInfo': {key: value for key, value in EMPLOYEE.items() if not key.startswith('numOf')}})
        self.source.delete_user.assert_not_called()
        self.target.get_user.side_effect = None
        self.target.get_user.return_value = dict(EMPLOYEE)
        finish_transfer(self.state, self.source, self.target, self.store, pending, sites=self.sites)
        self.source.delete_user.assert_called_once_with('123')
        sites = {n: {'employees': {'Shop': {'members': ['Test Person'] if n == 'A' else []}}, 'dept_order': ['Shop']} for n in ['A', 'B']}
        RosterStore(self.store.path).apply(sites)
        self.assertEqual(sites['A']['employees']['Shop']['members'], [])
        self.assertEqual(sites['B']['employees']['Shop']['member_ids']['Test Person'], '123')
        self.assertEqual(self.store.history()[0]['actor'], 'HR')

    def test_destination_disappeared_source_retained(self):
        pending = {'employee': EMPLOYEE, 'source': 'A', 'target': 'B', 'department': 'Shop'}
        self.target.get_user.side_effect = None
        self.target.get_user.return_value = None
        with self.assertRaises(HikvisionError):
            finish_transfer(self.state, self.source, self.target, self.store, pending, sites=self.sites)
        self.source.delete_user.assert_not_called()

    def test_same_terminal_rejected(self):
        self.target.base_url = self.source.base_url
        with self.assertRaises(HikvisionError):
            prepare_transfer(self.state, self.source, self.target, self.store, EMPLOYEE, 'A', 'B', 'Shop', sites=self.sites)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = HikvisionClient('http://example.test', 'admin', 'test')

    def test_root_error_is_not_success(self):
        self.assertFalse(self.client._is_success({'statusCode': 4, 'statusString': 'Invalid Operation'}))
        self.assertFalse(self.client._is_success({}))

    def test_single_person_delete_and_confirmation(self):
        self.client._request = MagicMock(return_value=ApiResult(True, 200, {'statusCode': 1}))
        self.client.get_user = MagicMock(return_value=None)
        self.client.delete_user('123')
        body = self.client._request.call_args.kwargs['json']['UserInfoDetail']
        self.assertEqual(body, {'mode': 'byEmployeeNo', 'EmployeeNoList': [{'employeeNo': '123'}]})

    def test_empty_delete_rejected(self):
        self.client._request = MagicMock()
        with self.assertRaises(HikvisionError):
            self.client.delete_user('')
        self.client._request.assert_not_called()

    def test_create_does_not_modify_existing_id(self):
        self.client.get_user = MagicMock(return_value=EMPLOYEE)
        self.client._request = MagicMock()
        with self.assertRaises(HikvisionError):
            self.client.create_user({'employee_no': '123'})
        self.client._request.assert_not_called()

    def test_search_pagination(self):
        self.client._request = MagicMock(side_effect=[
            ApiResult(True, 200, {'UserInfoSearch': {'totalMatches': 2, 'UserInfo': [EMPLOYEE]}}),
            ApiResult(True, 200, {'UserInfoSearch': {'totalMatches': 2, 'UserInfo': [{**EMPLOYEE, 'employeeNo': '124'}]}})])
        self.assertEqual(len(self.client.search_users()), 2)
        self.assertEqual(self.client._request.call_args.kwargs['json']['UserInfoSearchCond']['searchResultPosition'], 1)

    def test_incomplete_search_is_not_a_complete_current_roster(self):
        self.client._request = MagicMock(side_effect=[
            ApiResult(True, 200, {'UserInfoSearch': {'totalMatches': 2, 'UserInfo': [EMPLOYEE]}}),
            ApiResult(True, 200, {'UserInfoSearch': {'totalMatches': 2, 'UserInfo': []}})])
        with self.assertRaisesRegex(HikvisionError, 'incomplete'):
            self.client.search_users()

    def test_missing_count_cannot_masquerade_as_an_empty_terminal(self):
        self.client._request = MagicMock(return_value=ApiResult(True, 200, {'UserInfoSearch': {}}))
        with self.assertRaisesRegex(HikvisionError, 'complete employee count'):
            self.client.search_users()

    def test_repeated_page_cannot_hide_other_employees(self):
        self.client._request = MagicMock(return_value=ApiResult(True, 200, {
            'UserInfoSearch': {'totalMatches': 2, 'UserInfo': [EMPLOYEE]}}))
        with self.assertRaisesRegex(HikvisionError, 'repeated'):
            self.client.search_users()

    def test_verified_empty_terminal_is_valid(self):
        self.client._request = MagicMock(return_value=ApiResult(True, 200, {
            'UserInfoSearch': {'totalMatches': 0, 'UserInfo': []}}))
        self.assertEqual(self.client.search_users(), [])


if __name__ == '__main__':
    unittest.main()
