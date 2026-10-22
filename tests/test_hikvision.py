import unittest
from unittest.mock import MagicMock

from hikvision import ApiResult, HikvisionClient, HikvisionError, response_message


RECORD = {
    'employee_no': '201', 'name': 'Test Person', 'gender': 'male',
    'valid_from': '2026-10-22', 'valid_until': '2036-12-31', 'access_plan': 1,
}


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.client = HikvisionClient('http://example.test', 'admin', 'test')
        self.users = {}
        self.client.session.request = MagicMock(side_effect=self.terminal_request)

    def terminal_request(self, method, url, **kwargs):
        body = kwargs['json']
        if url.endswith('/UserInfo/Search?format=json') and method == 'POST':
            employee_no = body['UserInfoSearchCond']['EmployeeNoList'][0]['employeeNo']
            users = [self.users[employee_no]] if employee_no in self.users else []
            payload = {'UserInfoSearch': {'totalMatches': len(users), 'UserInfo': users}}
        elif url.endswith('/UserInfo/Record?format=json') and method == 'POST':
            user = body['UserInfo']
            self.users[user['employeeNo']] = user
            payload = {'statusCode': 1, 'statusString': 'OK'}
        elif url.endswith('/UserInfo/Modify?format=json') and method == 'PUT':
            user = body['UserInfo']
            self.users[user['employeeNo']] = user
            payload = {'statusCode': 1, 'statusString': 'OK'}
        else:
            payload = {'statusCode': 4, 'statusString': 'Invalid Operation', 'subStatusCode': 'methodNotAllowed'}
        response = MagicMock(ok=True, status_code=200, text='')
        response.json.return_value = payload
        return response

    def test_add_creates_and_confirms_employee(self):
        saved = self.client.create_user(RECORD)
        self.assertEqual(saved['employeeNo'], '201')
        self.assertEqual(saved['name'], 'Test Person')
        self.assertEqual(saved['RightPlan'], [{'doorNo': 1, 'planTemplateNo': '1'}])
        with self.assertRaisesRegex(HikvisionError, 'already exists'):
            self.client.create_user({**RECORD, 'name': 'Other Person'})
        self.assertEqual(self.users['201']['name'], 'Test Person')

    def test_upsert_creates_then_modifies(self):
        result, action = self.client.upsert_user(RECORD)
        self.assertTrue(result.ok)
        self.assertEqual(action, 'created')
        result, action = self.client.upsert_user({**RECORD, 'name': 'Updated Person'})
        self.assertTrue(result.ok)
        self.assertEqual(action, 'updated')
        self.assertEqual(self.users['201']['name'], 'Updated Person')

    def test_terminal_error_keeps_specific_reason(self):
        status = {'statusString': 'Invalid Operation', 'subStatusCode': 'notSupport', 'errorMsg': 'Unsupported method'}
        for payload in (status, {'ResponseStatus': status}):
            self.assertEqual(response_message(ApiResult(False, 400, payload)),
                             'Invalid Operation: notSupport: Unsupported method')


if __name__ == '__main__':
    unittest.main()
