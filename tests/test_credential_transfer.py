import copy
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import MagicMock, patch
from email import policy
from email.parser import BytesParser

from PIL import Image
import requests

from credential_transfer import export_credentials, read_face, search_cards
from employee_management import prepare_transfer, retry_transfer_credentials, finish_transfer
from hikvision import HikvisionClient, HikvisionError
from roster_store import RosterStore
from biometric_protocol import prepare_face_jpeg


def jpeg(color='red'):
    stream = io.BytesIO()
    Image.new('RGB', (128, 128), color).save(stream, 'JPEG')
    return stream.getvalue()


class MemoryTerminal:
    """Two independent ISAPI devices; real client HTTP is replaced in memory."""
    def __init__(self, url):
        self.client = HikvisionClient(url, 'test', 'test')
        self.client.session.request = MagicMock(side_effect=self.request)
        self.users, self.cards, self.fingers, self.faces = {}, [], {}, {}
        self.fail_path = None
        self.reject_finger = False
        self.drop_finger = False
        self.drop_face = False
        self.face_mode = 'multipart'
        self.face_wrong_id = False
        self.reported_fp_count = None
        self.page_size = 30
        self.calls = []
        self.events = ['historical attendance event']

    def user(self, employee_no):
        if employee_no not in self.users:
            return None
        user = copy.deepcopy(self.users[employee_no])
        user.update(numOfCard=sum(c['employeeNo'] == employee_no for c in self.cards),
                    numOfFP=len(self.fingers.get(employee_no, [])), numOfFace=int(employee_no in self.faces))
        if self.reported_fp_count is not None:
            user['numOfFP'] = self.reported_fp_count
        return user

    @staticmethod
    def response(payload=None, content=None, content_type='application/json', status=200):
        response = requests.Response()
        response.status_code = status
        response.headers['Content-Type'] = content_type
        response._content = json.dumps(payload).encode() if content is None else content
        return response

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        self.calls.append((method, path))
        if path == self.fail_path:
            return self.response({'statusCode': 4, 'statusString': 'Invalid Operation', 'subStatusCode': 'notSupport'})
        body = kwargs.get('json', {})
        ok = {'statusCode': 1, 'statusString': 'OK'}
        if path.endswith('/UserInfo/Search'):
            condition = body['UserInfoSearchCond']
            if 'EmployeeNoList' in condition:
                employee_no = condition['EmployeeNoList'][0]['employeeNo']
                user = self.user(employee_no)
                users = [user] if user else []
            else:
                users = [self.user(employee_no) for employee_no in self.users]
            return self.response({'UserInfoSearch': {'totalMatches': len(users), 'UserInfo': users}})
        if path.endswith('/UserInfo/Record') and method == 'POST':
            user = copy.deepcopy(body['UserInfo'])
            if user['employeeNo'] in self.users:
                return self.response({'statusCode': 4, 'statusString': 'duplicate'})
            self.users[user['employeeNo']] = user
            return self.response(ok)
        if path.endswith('/CardInfo/Search'):
            condition = body['CardInfoSearchCond']
            if 'EmployeeNoList' in condition:
                employee_no = condition['EmployeeNoList'][0]['employeeNo']
                cards = [c for c in self.cards if c['employeeNo'] == employee_no]
            else:
                card_no = condition['CardNoList'][0]['cardNo']
                cards = [c for c in self.cards if c['cardNo'] == card_no]
            position = condition['searchResultPosition']
            return self.response({'CardInfoSearch': {'totalMatches': len(cards),
                                 'CardInfo': copy.deepcopy(cards[position:position+self.page_size])}})
        if path.endswith('/CardInfo/Record') and method == 'POST':
            card = copy.deepcopy(body['CardInfo'])
            if any(c['cardNo'] == card['cardNo'] for c in self.cards):
                return self.response({'statusCode': 4, 'statusString': 'duplicateCard'})
            self.cards.append({k: v for k, v in card.items() if not k.startswith('check')})
            return self.response(ok)
        if path.endswith('/FingerPrintUpload'):
            employee_no = body['FingerPrintCond']['employeeNo']
            fingers = copy.deepcopy(self.fingers.get(employee_no, []))
            return self.response({'FingerPrintInfo': {'status': 'OK' if fingers else 'NoFP',
                                                      'FingerPrintList': fingers}})
        if path.endswith('/FingerPrint/SetUp') and method == 'POST':
            finger = copy.deepcopy(body['FingerPrintCfg'])
            assert finger['enableCardReader'] == [1]
            if self.reject_finger:
                return self.response({'FingerPrintStatus': {'StatusList': [{'id': 1, 'cardReaderRecvStatus': '4'}]}})
            if not self.drop_finger:
                employee_no = finger.pop('employeeNo')
                finger.pop('enableCardReader')
                finger.pop('checkEmployeeNo')
                current = self.fingers.setdefault(employee_no, [])
                current[:] = [f for f in current if f['fingerPrintID'] != finger['fingerPrintID']]
                current.append(finger)
            return self.response({'FingerPrintStatus': {'StatusList': [{'id': 1, 'cardReaderRecvStatus': '1'}]}})
        if ((path.endswith('/FaceDataRecord') and method == 'POST') or
                (path.endswith('/FDSearch') and method == 'PUT')):
            message = BytesParser(policy=policy.default).parsebytes(
                ('Content-Type: ' + kwargs['headers']['Content-Type'] + '\r\n\r\n').encode() + kwargs['data'])
            fields = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
                      for part in message.iter_parts()}
            metadata = json.loads(fields['FaceDataRecord'])
            if not self.drop_face:
                self.faces[metadata['FPID']] = fields['FaceImage']
            return self.response(ok)
        if path.endswith('/FDSearch') and method == 'POST':
            employee_no = body['FPID']
            exists = employee_no in self.faces
            match = {'FPID': 'wrong' if self.face_wrong_id else employee_no}
            if self.face_mode == 'url':
                match['faceURL'] = f'http://192.168.1.10/LOCALS/pic/{employee_no}.jpg'
            elif self.face_mode == 'model':
                match['modelData'] = 'not-a-photograph'
            payload = {**ok, 'numOfMatches': int(exists), 'totalMatches': int(exists),
                       'MatchList': [match] if exists else []}
            if not exists or self.face_mode != 'multipart':
                return self.response(payload)
            raw = (b'--testboundary\r\nContent-Type: application/json\r\n\r\n' + json.dumps(payload).encode() +
                   b'\r\n--testboundary\r\nContent-Type: image/jpeg\r\n\r\n' + self.faces[employee_no] +
                   b'\r\n--testboundary--\r\n')
            return self.response(content=raw, content_type='multipart/form-data; boundary=testboundary')
        if path.startswith('/LOCALS/pic/') and method == 'GET':
            assert url.startswith(self.client.base_url + '/')
            assert kwargs['allow_redirects'] is False
            employee_no = path.rsplit('/', 1)[-1].removesuffix('.jpg')
            return self.response(content=self.faces[employee_no], content_type='image/jpeg')
        if path.endswith('/UserInfoDetail/Delete') and method == 'PUT':
            selected = body['UserInfoDetail']['EmployeeNoList']
            assert len(selected) == 1
            employee_no = selected[0]['employeeNo']
            del self.users[employee_no]
            self.cards[:] = [c for c in self.cards if c['employeeNo'] != employee_no]
            self.fingers.pop(employee_no, None)
            self.faces.pop(employee_no, None)
            return self.response(ok)
        raise AssertionError((method, path))


class CredentialTransferTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = RosterStore(Path(self.folder.name) / 'roster.db')
        self.state = {'management_account': 'HR', 'management_last_seen': time.time()}
        self.sites = {n: {'employees': {'Shop': {'members': ['Test Person'] if n == 'A' else []}},
                          'dept_order': ['Shop']} for n in ('A', 'B')}
        self.source = MemoryTerminal('http://source.test')
        self.target = MemoryTerminal('http://target.test')
        self.employee = {'employeeNo': '201', 'name': 'Test Person', 'userType': 'normal',
                         'Valid': {'enable': True}, 'doorRight': '1'}
        self.source.users['201'] = copy.deepcopy(self.employee)
        self.source.fingers['201'] = [
            {'fingerPrintID': 1, 'fingerType': 'normalFP', 'fingerData': 'template-one'},
            {'fingerPrintID': 7, 'fingerType': 'hijackFP', 'fingerData': 'template-seven', 'leaderFP': [1]},
        ]
        self.source.cards = [{'employeeNo': '201', 'cardNo': number, 'cardType': 'normalCard'}
                             for number in ('001234', '005678')]
        self.source.faces['201'] = jpeg()

    def prepare(self, **kwargs):
        return prepare_transfer(self.state, self.source.client, self.target.client, self.store,
                                self.employee, 'A', 'B', 'Shop', sites=self.sites, job='CASHIER', **kwargs)

    def finish(self, pending):
        finish_transfer(self.state, self.source.client, self.target.client, self.store, pending, sites=self.sites)

    def existing_copy(self):
        for key in ('users', 'cards', 'fingers', 'faces'):
            setattr(self.target, key, copy.deepcopy(getattr(self.source, key)))

    def assert_no_destination_writes(self):
        writes = [path for method, path in self.target.calls
                  if path.endswith(('/Record', '/SetUp', '/Delete')) or method == 'PUT']
        self.assertEqual(writes, [])

    def test_existing_account_still_rejected_without_explicit_reconciliation(self):
        self.existing_copy()
        with self.assertRaisesRegex(HikvisionError, 'already exists'):
            self.prepare()
        self.assert_no_destination_writes()
        self.assertNotIn('management_transfer', self.state)

    def test_matching_existing_account_verified_without_overwriting_then_transfer_finishes(self):
        self.existing_copy()
        pending = self.prepare(reconcile_existing=True)
        self.assertTrue(pending['existing_destination'])
        self.assertTrue(pending['credentials_verified'])
        self.assertEqual(pending['credential_counts'], {'cards': 2, 'fingerprints': 2, 'faces': 1})
        self.assert_no_destination_writes()
        self.finish(pending)
        self.assertNotIn('201', self.source.users)
        self.assertIn('201', self.target.users)
        self.assert_no_destination_writes()

    def test_existing_id_for_other_person_cannot_be_reconciled(self):
        self.existing_copy()
        self.target.users['201']['name'] = 'Other Person'
        with self.assertRaisesRegex(HikvisionError, 'different person'):
            self.prepare(reconcile_existing=True)
        self.assert_no_destination_writes()
        self.assertEqual(self.store.history(), [])

    def test_existing_different_credentials_block_reconciliation_without_overwriting(self):
        self.existing_copy()
        self.target.fingers['201'][0]['fingerData'] = 'destination-template'
        with self.assertRaisesRegex(HikvisionError, 'could not be verified'):
            self.prepare(reconcile_existing=True)
        self.assertEqual(self.target.fingers['201'][0]['fingerData'], 'destination-template')
        self.assertIn('201', self.source.users)
        self.assertNotIn('management_transfer', self.state)
        self.assert_no_destination_writes()
        self.assertEqual(self.store.history(), [])

    def test_existing_extra_credentials_cannot_be_skipped_when_source_has_none(self):
        original = {key: copy.deepcopy(getattr(self.source, key)) for key in ('cards', 'fingers', 'faces')}
        for kind in ('cards', 'fingers', 'faces'):
            with self.subTest(kind=kind):
                for key, value in original.items():
                    setattr(self.source, key, copy.deepcopy(value))
                self.existing_copy()
                self.source.cards = []
                self.source.fingers = {}
                self.source.faces = {}
                # Retain just one credential category at the destination.
                for other in ('cards', 'fingers', 'faces'):
                    if other != kind:
                        setattr(self.target, other, [] if other == 'cards' else {})
                with self.assertRaisesRegex(HikvisionError, 'could not be verified'):
                    self.prepare(reconcile_existing=True)
                self.assertNotIn('management_transfer', self.state)
                self.assertIn('201', self.source.users)
                self.assert_no_destination_writes()

    def test_existing_different_access_settings_block_source_removal(self):
        self.existing_copy()
        self.target.users['201']['Valid'] = {'enable': False}
        with self.assertRaisesRegex(HikvisionError, 'access settings differ'):
            self.prepare(reconcile_existing=True)
        self.assertIn('201', self.source.users)
        self.assert_no_destination_writes()
        self.assertEqual(self.store.history(), [])

    def test_reconciliation_retry_never_overwrites_changed_destination_credentials(self):
        self.existing_copy()
        pending = self.prepare(reconcile_existing=True)
        self.target.fingers['201'][0]['fingerData'] = 'changed-after-verification'
        with self.assertRaisesRegex(HikvisionError, 'could not be verified'):
            retry_transfer_credentials(self.state, self.source.client, self.target.client, self.store, pending, sites=self.sites)
        self.assertFalse(pending['credentials_verified'])
        self.assert_no_destination_writes()
        with self.assertRaisesRegex(HikvisionError, 'Copy and verify'):
            self.finish(pending)
        self.assertIn('201', self.source.users)

    def test_existing_access_settings_checked_again_before_source_deletion(self):
        self.existing_copy()
        pending = self.prepare(reconcile_existing=True)
        self.target.users['201']['doorRight'] = '2'
        with self.assertRaisesRegex(HikvisionError, 'access settings differ'):
            self.finish(pending)
        self.assertIn('201', self.source.users)
        self.assert_no_destination_writes()

    def test_full_transfer_preserves_all_credentials_and_history(self):
        self.source.page_size = 1
        pending = self.prepare()
        self.assertIn('201', self.source.users)
        self.assertTrue(pending['credentials_verified'])
        self.assertEqual(pending['credential_counts'], {'cards': 2, 'fingerprints': 2, 'faces': 1})
        self.assertEqual(self.target.fingers, self.source.fingers)
        self.assertEqual(self.target.cards, self.source.cards)
        self.assertEqual(self.target.faces['201'], prepare_face_jpeg(self.source.faces['201']))
        session_text = json.dumps(self.state)
        db_text = json.dumps(self.store.history())
        for secret in ('template-one', 'template-seven', '001234', '005678'):
            self.assertNotIn(secret, session_text)
            self.assertNotIn(secret, db_text)
        self.finish(pending)
        self.assertNotIn('201', self.source.users)
        self.assertIn('201', self.target.users)
        self.assertEqual(self.source.events, ['historical attendance event'])
        self.store.apply(self.sites)
        self.assertEqual(self.sites['A']['employees']['Shop']['members'], [])
        self.assertEqual(self.sites['B']['employees']['Shop']['member_jobs']['Test Person'], 'CASHIER')

    def test_unsupported_export_stops_before_destination_creation(self):
        self.source.fail_path = '/ISAPI/AccessControl/FingerPrintUpload'
        with self.assertRaisesRegex(HikvisionError, 'notSupport'):
            self.prepare()
        self.assertFalse(self.target.users)
        self.assertIn('201', self.source.users)
        self.assertEqual(self.store.history(), [])

    def test_credential_transfer_removes_canonical_roster_entry_for_terminal_alias(self):
        original_fingers = copy.deepcopy(self.source.fingers['201'])
        department = self.sites['A']['employees']['Shop']
        department['members'] = ['Canonical Report Name']
        department['member_aliases'] = {'Canonical Report Name': ['Test Person']}
        department['member_ids'] = {'Canonical Report Name': '201'}
        pending = self.prepare()
        self.assertEqual(pending['credential_counts'], {'cards': 2, 'fingerprints': 2, 'faces': 1})
        self.finish(pending)
        self.store.apply(self.sites)
        self.assertEqual(department['members'], [])
        self.assertEqual(self.sites['B']['employees']['Shop']['members'], ['Test Person'])
        self.assertEqual(self.target.fingers['201'], original_fingers)

    def test_card_owned_by_other_employee_is_never_reassigned(self):
        self.target.cards = [{'employeeNo': '999', 'cardNo': '001234', 'cardType': 'normalCard'}]
        with self.assertRaisesRegex(HikvisionError, 'another employee'):
            self.prepare()
        self.assertFalse(self.target.users)
        self.assertEqual(self.target.cards[0]['employeeNo'], '999')

    def test_partial_copy_can_retry_without_duplicate_cards_or_source_deletion(self):
        self.target.reject_finger = True
        with self.assertRaisesRegex(HikvisionError, 'reader memory is full'):
            self.prepare()
        pending = self.state['management_transfer']
        self.assertFalse(pending['credentials_verified'])
        self.assertIn('201', self.source.users)
        self.assertEqual(len(self.target.cards), 2)
        with self.assertRaisesRegex(HikvisionError, 'Copy and verify'):
            self.finish(pending)
        self.target.reject_finger = False
        retry_transfer_credentials(self.state, self.source.client, self.target.client,
                                   self.store, pending, sites=self.sites)
        self.assertEqual(len(self.target.cards), 2)
        self.assertEqual(len(self.target.fingers['201']), 2)
        self.finish(pending)
        self.assertNotIn('201', self.source.users)

    def test_http_success_without_saved_fingerprint_blocks_finish(self):
        self.target.drop_finger = True
        with self.assertRaisesRegex(HikvisionError, 'could not be verified'):
            self.prepare()
        self.assertIn('201', self.source.users)
        self.assertFalse(self.state['management_transfer']['credentials_verified'])

    def test_same_count_but_changed_destination_template_blocks_deletion(self):
        pending = self.prepare()
        self.target.fingers['201'][0]['fingerData'] = 'different'
        with self.assertRaisesRegex(HikvisionError, 'could not be verified'):
            self.finish(pending)
        self.assertIn('201', self.source.users)

    def test_source_enrollment_change_requires_recopy(self):
        pending = self.prepare()
        self.source.fingers['201'][0]['fingerData'] = 'new-template'
        with self.assertRaisesRegex(HikvisionError, 'Source credentials changed'):
            self.finish(pending)
        self.assertIn('201', self.source.users)
        retry_transfer_credentials(self.state, self.source.client, self.target.client,
                                   self.store, pending, sites=self.sites)
        self.finish(pending)
        self.assertEqual(self.target.fingers['201'][-1]['fingerData'], 'new-template')

    def test_changed_destination_face_requires_recopy(self):
        pending = self.prepare()
        self.target.faces['201'] = jpeg('blue')
        with self.assertRaisesRegex(HikvisionError, 'face photograph differs'):
            self.finish(pending)
        self.assertIn('201', self.source.users)

    def test_terminal_reconfiguration_blocks_finish(self):
        pending = self.prepare()
        self.target.client.base_url = 'http://other.test'
        with self.assertRaisesRegex(HikvisionError, 'configuration changed'):
            self.finish(pending)
        self.assertIn('201', self.source.users)

    def test_count_mismatch_cannot_silently_omit_fingerprints(self):
        self.source.reported_fp_count = 3
        with self.assertRaisesRegex(HikvisionError, 'export all fingerprints'):
            self.prepare()
        self.assertFalse(self.target.users)

    def test_face_url_uses_configured_station_not_returned_lan_host(self):
        self.source.face_mode = 'url'
        self.assertEqual(read_face(self.source.client, '201'), self.source.faces['201'])
        self.assertIn(('GET', '/LOCALS/pic/201.jpg'), self.source.calls)

    def test_model_without_photograph_blocks_transfer(self):
        self.source.face_mode = 'model'
        with self.assertRaisesRegex(HikvisionError, 'did not export'):
            self.prepare()
        self.assertFalse(self.target.users)

    def test_wrong_face_id_blocks_transfer(self):
        self.source.face_wrong_id = True
        with self.assertRaisesRegex(HikvisionError, 'different employee'):
            self.prepare()
        self.assertFalse(self.target.users)

    def test_retry_does_not_accept_a_different_face_after_ignored_upload(self):
        pending = self.prepare()
        self.target.faces['201'] = jpeg('blue')
        self.target.drop_face = True
        with self.assertRaisesRegex(HikvisionError, 'face photograph differs'):
            retry_transfer_credentials(self.state, self.source.client, self.target.client,
                                       self.store, pending, sites=self.sites)
        self.assertFalse(pending['credentials_verified'])
        self.assertIn('201', self.source.users)

    def test_user_interface_retries_partial_copy_then_finishes(self):
        import os
        from streamlit.testing.v1 import AppTest

        source_code = '''
import os
import streamlit as st
from employee_management import render_management
from roster_store import RosterStore
sites = {name: {'employees': {'Shop': {'members': ['Test Person'] if name == 'A' else [], 'label': 'SHOP'}}, 'dept_order': ['Shop']} for name in ('A', 'B')}
store = RosterStore(os.environ['TRANSFER_TEST_DB'])
store.apply(sites)
render_management(sites, store)
'''
        clients = {'A': self.source.client, 'B': self.target.client}
        self.target.reject_finger = True
        with patch.dict(os.environ, {'TRANSFER_TEST_DB': self.store.path}), patch(
                'employee_management.client_for', side_effect=lambda site, sites: clients[site]):
            at = AppTest.from_string(source_code, default_timeout=30)
            at.session_state['management_account'] = 'HR'
            at.session_state['management_last_seen'] = time.time()
            at.session_state['management_open'] = True
            at.run()
            at.selectbox(key='manage_transfer_source').select('A').run()
            at.button(key='manage_transfer_A_refresh').click().run()
            at.selectbox(key='manage_transfer_A_choice').select_index(0).run()
            at.selectbox(key='manage_transfer_target_A').select('B').run()
            at.selectbox(key='manage_transfer_department_B').select('Shop').run()
            at.selectbox(key='manage_transfer_job_B_Shop').select('SHOP').run()
            at.checkbox(key='manage_transfer_start_confirm_A').check().run()
            next(b for b in at.button if b.label == 'Prepare destination').click().run()
            self.assertFalse(at.exception)
            self.assertTrue(any('reader memory is full' in error.value for error in at.error))
            at.run()
            self.assertTrue(next(b for b in at.button if b.label == 'Finish transfer').disabled)
            self.assertTrue(at.checkbox(key='manage_transfer_confirm').disabled)
            self.assertFalse(at.get('file_uploader'))
            self.target.reject_finger = False
            at.button(key='manage_transfer_retry').click().run()
            self.assertFalse(at.exception)
            self.assertTrue(any('2 fingerprint(s)' in success.value for success in at.success))
            at.checkbox(key='manage_transfer_confirm').check().run()
            next(b for b in at.button if b.label == 'Finish transfer').click().run()
            self.assertFalse(at.exception)
            self.assertNotIn('201', self.source.users)
            self.assertIn('201', self.target.users)

    def test_employee_without_credentials_needs_no_credential_endpoints(self):
        self.source.cards.clear()
        self.source.fingers.clear()
        self.source.faces.clear()
        pending = self.prepare()
        self.finish(pending)
        self.assertEqual(pending['credential_counts'], {'cards': 0, 'fingerprints': 0, 'faces': 0})
        self.assertFalse(any('FingerPrint' in path or 'CardInfo' in path or 'FDSearch' in path
                             for method, path in self.source.calls + self.target.calls))


if __name__ == '__main__':
    unittest.main()
