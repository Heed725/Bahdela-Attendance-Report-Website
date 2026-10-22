import io
import json
import unittest
import xml.etree.ElementTree as ET
from email import policy
from email.parser import BytesParser
from unittest.mock import MagicMock, patch

import requests
from PIL import Image

from biometric_protocol import decode_document, prepare_face_jpeg
from hikvision import ApiResult, CapturedFingerprint, HikvisionClient, HikvisionError, response_message


def reply(payload=None, raw=None, content_type='application/json', status=200):
    response = requests.Response()
    response.status_code = status
    response.headers['Content-Type'] = content_type
    response._content = raw if raw is not None else json.dumps(payload).encode()
    return response


def photo(size=(128, 128)):
    stream = io.BytesIO()
    Image.new('RGB', size, 'red').save(stream, 'JPEG')
    return stream.getvalue()


def xml_error(reason='badXmlContent'):
    return reply(raw=(f'<ResponseStatus xmlns="http://www.isapi.org/ver20/XMLSchema" version="2.0">'
                      f'<statusCode>6</statusCode><statusString>Invalid Content</statusString>'
                      f'<subStatusCode>{reason}</subStatusCode></ResponseStatus>').encode(),
                 content_type='application/xml', status=400)


CAPTURE = (b'<CaptureFingerPrint xmlns="http://www.isapi.org/ver20/XMLSchema" version="2.0">'
           b'<fingerData>dGVzdC10ZW1wbGF0ZQ==</fingerData><fingerNo>1</fingerNo>'
           b'<fingerPrintQuality>87</fingerPrintQuality></CaptureFingerPrint>')


class BiometricProtocolTests(unittest.TestCase):
    def setUp(self):
        self.client = HikvisionClient('http://terminal.test', 'test', 'test')
        self.client.session.request = MagicMock()

    def test_capture_sends_xml_condition_to_xml_endpoint_and_decodes_result(self):
        def terminal(method, url, **kwargs):
            self.assertEqual((method, url), ('POST', 'http://terminal.test/ISAPI/AccessControl/CaptureFingerPrint'))
            self.assertNotIn('json', kwargs)
            self.assertIn('application/xml', kwargs['headers']['Content-Type'])
            root = ET.fromstring(kwargs['data'])
            self.assertEqual(root.tag, '{http://www.isapi.org/ver20/XMLSchema}CaptureFingerPrintCond')
            self.assertEqual(root.find('{*}fingerNo').text, '1')
            return reply(raw=CAPTURE, content_type='application/xml')
        self.client.session.request.side_effect = terminal
        capture = self.client.capture_fingerprint()
        self.assertEqual(capture.data, 'dGVzdC10ZW1wbGF0ZQ==')
        self.assertEqual(capture.quality, 87)

    def test_json_only_firmware_uses_correct_condition_wrapper_after_explicit_rejection(self):
        self.client.session.request.side_effect = [xml_error(), reply({'CaptureFingerPrint': {
            'fingerData': 'template', 'fingerNo': 1, 'fingerPrintQuality': 90}})]
        capture = self.client.capture_fingerprint()
        self.assertEqual(capture.data, 'template')
        request = self.client.session.request.call_args
        self.assertTrue(request.args[1].endswith('/CaptureFingerPrint?format=json'))
        self.assertEqual(request.kwargs['json'], {'CaptureFingerPrintCond': {'fingerNo': 1}})

    def test_capture_decodes_multipart_xml_without_returning_fingerprint_image(self):
        raw = (b'--sample\r\nContent-Type: application/xml\r\n\r\n' + CAPTURE +
               b'\r\n--sample\r\nContent-Type: image/jpeg\r\n\r\nignored-picture\r\n--sample--\r\n')
        self.client.session.request.return_value = reply(raw=raw, content_type='multipart/form-data; boundary=sample')
        self.assertEqual(self.client.capture_fingerprint().data, 'dGVzdC10ZW1wbGF0ZQ==')

    def test_busy_capture_is_not_retried(self):
        self.client.session.request.return_value = xml_error('deviceBusy')
        capture = self.client.capture_fingerprint()
        self.assertFalse(capture.data)
        self.assertIn('deviceBusy', capture.message)
        self.client.session.request.assert_called_once()

    def test_capture_timeout_is_not_retried(self):
        self.client.session.request.side_effect = requests.Timeout()
        with self.assertRaisesRegex(HikvisionError, 'timed out'):
            self.client.capture_fingerprint()
        self.client.session.request.assert_called_once()

    def test_capture_wrong_slot_is_not_saved(self):
        self.client.session.request.return_value = reply(raw=CAPTURE.replace(b'<fingerNo>1', b'<fingerNo>7'),
                                                        content_type='application/xml')
        capture = self.client.capture_fingerprint()
        self.assertFalse(capture.data)
        self.assertIn('different fingerprint slot', capture.message)

    def test_invalid_slot_never_contacts_terminal(self):
        with self.assertRaisesRegex(HikvisionError, 'between 1 and 10'):
            self.client.capture_fingerprint(11)
        self.client.session.request.assert_not_called()

    def test_failed_fingerprint_reader_is_not_success_even_over_http_200(self):
        self.client.session.request.return_value = reply({'FingerPrintStatus': {
            'StatusList': [{'id': 1, 'cardReaderRecvStatus': '4'}]}})
        result = self.client.apply_fingerprint('301', 'test-template')
        self.assertFalse(result.ok)
        self.assertIn('memory is full', response_message(result))

    def test_xml_status_is_decoded_without_exposing_raw_xml(self):
        self.client.session.request.return_value = xml_error()
        result = self.client._request('POST', '/test')
        self.assertFalse(result.ok)
        self.assertEqual(response_message(result), 'Invalid Content: badXmlContent')

    def test_face_upload_has_ordered_parts_required_metadata_and_exact_lengths(self):
        def terminal(method, url, **kwargs):
            self.assertEqual(method, 'POST')
            self.assertTrue(url.endswith('/FaceDataRecord?format=json'))
            self.assertNotIn('files', kwargs)
            message = BytesParser(policy=policy.default).parsebytes(
                ('Content-Type: ' + kwargs['headers']['Content-Type'] + '\r\n\r\n').encode() + kwargs['data'])
            parts = list(message.iter_parts())
            self.assertEqual([p.get_param('name', header='content-disposition') for p in parts],
                             ['FaceDataRecord', 'FaceImage'])
            for part in parts:
                self.assertEqual(int(part['Content-Length']), len(part.get_payload(decode=True)))
                self.assertIsNone(part.get_filename())
            metadata = json.loads(parts[0].get_payload(decode=True))
            self.assertEqual(metadata['FPID'], '301')
            self.assertEqual(metadata['name'], 'Test Employee')
            self.assertTrue(metadata['saveFacePic'])
            self.assertEqual(parts[1].get_content_type(), 'image/jpeg')
            return reply({'statusCode': 1, 'statusString': 'OK'})
        self.client.session.request.side_effect = terminal
        self.assertTrue(self.client.upload_face('301', photo(), name='Test Employee').ok)

    def test_face_format_rejection_retries_legacy_image_field_only(self):
        self.client.session.request.side_effect = [xml_error(), reply({'statusCode': 1})]
        self.assertTrue(self.client.upload_face('301', photo(), name='Test Employee').ok)
        request = self.client.session.request.call_args
        self.assertIn(b'name="img"', request.kwargs['data'])
        self.assertEqual(self.client.session.request.call_count, 2)

    def test_face_busy_or_quality_failure_is_not_retried(self):
        self.client.session.request.return_value = xml_error('picQuality')
        self.assertFalse(self.client.upload_face('301', photo(), name='Test Employee').ok)
        self.client.session.request.assert_called_once()

    def test_large_photo_becomes_small_valid_jpeg(self):
        output = prepare_face_jpeg(photo((3200, 2400)))
        self.assertLessEqual(len(output), 200 * 1024)
        with Image.open(io.BytesIO(output)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertLessEqual(max(image.size), 640)

    def test_invalid_or_too_small_photo_never_reaches_terminal(self):
        for image in (b'not-a-jpeg', photo((16, 16))):
            with self.assertRaises(HikvisionError):
                self.client.upload_face('301', image)
        self.client.session.request.assert_not_called()

    def test_xml_entities_are_not_expanded(self):
        self.assertEqual(decode_document(b'<!DOCTYPE a [<!ENTITY x "secret">]><a>&x;</a>'), {})

    def test_ui_never_reports_saved_fingerprint_without_readback(self):
        import time
        from employee_management import enroll_employee
        client = MagicMock()
        employee = {'employeeNo': '301', 'name': 'Test Employee'}
        client.get_user.return_value = employee
        client.capture_fingerprint.return_value = CapturedFingerprint(data='test-template')
        client.apply_fingerprint.return_value = ApiResult(True, 200, {'statusCode': 1})
        with patch('employee_management.st') as st, patch('employee_management.client_for', return_value=client), \
                patch('employee_management.read_fingerprints', return_value=[]):
            st.session_state = {'management_account': 'HR', 'management_last_seen': time.time()}
            st.button.side_effect = lambda label, **kwargs: label == 'Capture and save fingerprint'
            with self.assertRaisesRegex(HikvisionError, 'did not confirm'):
                enroll_employee(employee, 'Puma Survey', {}, 'test')
            st.success.assert_not_called()


if __name__ == '__main__':
    unittest.main()
