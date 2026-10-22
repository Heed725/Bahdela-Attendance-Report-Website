from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests
from requests import Response
from requests.auth import HTTPDigestAuth
from biometric_protocol import decode_payload, face_multipart, format_rejected, prepare_face_jpeg


class HikvisionError(RuntimeError):
    """Raised when the Hikvision terminal cannot be contacted or decoded."""


@dataclass
class ApiResult:
    ok: bool
    status_code: int
    payload: dict[str, Any]
    text: str = ""


@dataclass
class CapturedFingerprint:
    data: str = ""
    quality: int | None = None
    message: str = ""


def response_message(result: ApiResult) -> str:
    payload = result.payload or {}
    fingerprint = payload.get('FingerPrintStatus')
    if isinstance(fingerprint, dict):
        entries = fingerprint.get('StatusList', [])
        if isinstance(entries, dict):
            entries = entries.get('Status', entries)
            if isinstance(entries, dict):
                entries = [entries]
        if isinstance(entries, list):
            meanings = {'0': 'failed', '2': 'reader is offline', '3': 'poor quality; try again',
                        '4': 'reader memory is full', '5': 'fingerprint already exists',
                        '7': 'invalid fingerprint slot', '8': 'reader does not need this template',
                        '10': 'reader firmware is too old'}
            for entry in entries:
                code = str(entry.get('cardReaderRecvStatus', '')) if isinstance(entry, dict) else ''
                if code != '1':
                    return 'Fingerprint was rejected: ' + meanings.get(code, 'unconfirmed reader status')
        if fingerprint.get('status') == 'failed':
            return 'Fingerprint was rejected by the terminal.'
    status = payload.get("ResponseStatus", payload)
    details = []
    for key in ("statusString", "subStatusCode", "errorMsg", "message"):
        value = status.get(key) if isinstance(status, dict) else None
        if value and str(value) not in details:
            details.append(str(value))
    if details:
        return ": ".join(details)
    return result.text.strip() or f"Hikvision returned HTTP {result.status_code}."


class HikvisionClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: int = 45,
        verify_tls: bool = False,
    ) -> None:
        if not base_url.strip():
            raise HikvisionError("HIKVISION_URL is not configured.")
        if "://" not in base_url:
            base_url = f"http://{base_url}"
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HikvisionError("HIKVISION_URL must be a valid HTTP or HTTPS address.")

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.session = requests.Session()
        self.session.auth = HTTPDigestAuth(username, password)
        self.session.headers.update({"Accept": "application/json"})

    def _send(self, method: str, path: str, **kwargs: Any) -> Response:
        kwargs.setdefault('timeout', self.timeout)
        kwargs.setdefault('verify', self.verify_tls)
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                **kwargs,
            )
        except requests.Timeout as exc:
            raise HikvisionError("The Hikvision terminal timed out. Check its address and network connection.") from exc
        except requests.RequestException as exc:
            raise HikvisionError(f"Could not connect to the Hikvision terminal: {exc}") from exc

        return response

    def _request(self, method: str, path: str, **kwargs: Any) -> ApiResult:
        response = self._send(method, path, **kwargs)
        payload = self._json(response)
        ok = response.ok and self._is_success(payload)
        return ApiResult(ok=ok, status_code=response.status_code, payload=payload, text=response.text)

    @staticmethod
    def _json(response: Response) -> dict[str, Any]:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {"data": data}
        except ValueError:
            return decode_payload(response)

    @staticmethod
    def _is_success(payload: dict[str, Any]) -> bool:
        status = payload.get("ResponseStatus", payload)
        if not isinstance(status, dict) or not payload:
            return False
        fingerprint = payload.get('FingerPrintStatus')
        if isinstance(fingerprint, dict):
            if str(fingerprint.get('status', 'success')).lower() != 'success':
                return False
            entries = fingerprint.get('StatusList', [])
            if isinstance(entries, dict):
                entries = entries.get('Status', entries)
                if isinstance(entries, dict):
                    entries = [entries]
            if not isinstance(entries, list) or any(
                    not isinstance(item, dict) or str(item.get('cardReaderRecvStatus')) != '1' for item in entries):
                return False
            if 'totalStatus' in fingerprint and str(fingerprint['totalStatus']) != '1':
                return False
        if "statusCode" not in status:
            return True
        code = str(status.get("statusCode", "1"))
        text = str(status.get("statusString", "OK")).lower()
        return code in {"0", "1"} and text not in {"error", "failed", "failure"}

    def user_exists(self, employee_no: str) -> bool:
        body = {
            "UserInfoSearchCond": {
                "searchID": str(uuid.uuid4()),
                "searchResultPosition": 0,
                "maxResults": 1,
                "EmployeeNoList": [{"employeeNo": employee_no}],
            }
        }
        result = self._request(
            "POST",
            "/ISAPI/AccessControl/UserInfo/Search?format=json",
            json=body,
        )
        if not result.ok:
            raise HikvisionError(f"Could not check the employee ID: {response_message(result)}")
        search = result.payload.get("UserInfoSearch", {})
        return int(search.get("numOfMatches", search.get("totalMatches", 0)) or 0) > 0

    def upsert_user(self, record: dict[str, Any]) -> tuple[ApiResult, str]:
        exists = self.user_exists(record["employee_no"])
        body = self.user_payload(record)
        endpoint = "/ISAPI/AccessControl/UserInfo/Modify?format=json" if exists else "/ISAPI/AccessControl/UserInfo/Record?format=json"
        result = self._request("PUT" if exists else "POST", endpoint, json=body)
        return result, "updated" if exists else "created"

    @staticmethod
    def user_payload(record):
        gender = record.get("gender", "unspecified")
        body = {
            "UserInfo": {
                "employeeNo": record["employee_no"],
                "name": record["name"],
                "userType": "normal",
                "gender": "unknown" if gender == "unspecified" else gender,
                "Valid": {
                    "enable": True,
                    "beginTime": f"{record['valid_from']}T00:00:00",
                    "endTime": f"{record['valid_until']}T23:59:59",
                    "timeType": "local",
                },
                "doorRight": "1",
                "RightPlan": [
                    {"doorNo": 1, "planTemplateNo": str(record["access_plan"])}
                ],
            }
        }
        return body

    def upload_face(self, employee_no: str, image: bytes, filename: str = "face.jpg",
                    name: str = '', method: str = 'POST', path: str | None = None) -> ApiResult:
        try:
            image = prepare_face_jpeg(image)
        except ValueError as exc:
            raise HikvisionError(str(exc)) from None
        metadata = {"faceLibType": "blackFD", "FDID": "1", "FPID": str(employee_no),
                    "name": name, "saveFacePic": True}
        path = path or '/ISAPI/Intelligent/FDLib/FaceDataRecord?format=json'
        request = face_multipart(metadata, image)
        request['headers']['Connection'] = 'close'
        result = self._request(method, path, **request)
        # Some older terminal web interfaces name the binary form part "img".
        # Retry only after an explicit format rejection, never on timeout/busy.
        if format_rejected(result):
            request = face_multipart(metadata, image, 'img')
            request['headers']['Connection'] = 'close'
            result = self._request(method, path, **request)
        return result

    def capture_profile(self):
        """Read the station's own namespace/version; never trigger capture here."""
        result = self._request('GET', '/ISAPI/AccessControl/CaptureFingerPrint/capabilities', timeout=(5, 10))
        if result.status_code in (401, 403):
            raise HikvisionError('The terminal denied access to fingerprint capabilities. Check this station’s connection account.')
        profile = {'namespace': 'http://www.isapi.org/ver20/XMLSchema', 'version': '2.0',
                   'available': result.ok, 'status': response_message(result) if not result.ok else 'Available'}
        if result.ok and '<!DOCTYPE' not in result.text.upper():
            try:
                root = ET.fromstring(result.text)
                if root.tag.rsplit('}', 1)[-1] == 'CaptureFingerPrint':
                    profile['namespace'] = root.tag[1:].split('}', 1)[0] if root.tag.startswith('{') else ''
                    profile['version'] = root.attrib.get('version', '2.0')
                    for node in root.iter():
                        if node.tag.rsplit('}', 1)[-1] == 'fingerNo':
                            profile['finger_min'] = node.attrib.get('min', '1')
                            profile['finger_max'] = node.attrib.get('max', '10')
                            break
            except ET.ParseError:
                pass
        return profile

    def terminal_diagnostics(self):
        """A small, read-only support report without secrets or employee data."""
        report = {}
        for label, path in (('Device', '/ISAPI/System/deviceInfo'),
                            ('Access control', '/ISAPI/AccessControl/capabilities')):
            try:
                result = self._request('GET', path, timeout=(5, 10))
                safe = {'HTTP': result.status_code}
                if result.ok:
                    def visit(value):
                        if not isinstance(value, dict):
                            return
                        for key, item in value.items():
                            if key in ('model', 'firmwareVersion', 'isSupportCaptureFingerPrint',
                                       'isSupportFingerPrintCfg', 'isSupportFace') and not isinstance(item, (dict, list)):
                                safe[key] = item
                            elif isinstance(item, dict):
                                visit(item)
                    visit(result.payload)
                else:
                    safe['status'] = response_message(result)
                report[label] = safe
            except HikvisionError:
                report[label] = {'status': 'Could not connect for this read-only check.'}
        try:
            report['Fingerprint capture'] = self.capture_profile()
        except HikvisionError as exc:
            report['Fingerprint capture'] = {'status': str(exc)}
        return report

    def capture_fingerprint(self, finger_no: int = 1) -> CapturedFingerprint:
        if not 1 <= finger_no <= 10:
            raise HikvisionError('Choose a fingerprint slot between 1 and 10.')
        try:
            profile = self.capture_profile()
        except HikvisionError as exc:
            return CapturedFingerprint(message=str(exc))
        # Use the namespace/version actually advertised by this terminal.
        namespace = ' xmlns=' + quoteattr(profile['namespace']) if profile['namespace'] else ''
        body = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<CaptureFingerPrintCond' + namespace + ' version=' + quoteattr(profile['version']) + '>'
                f'<fingerNo>{finger_no}</fingerNo></CaptureFingerPrintCond>').encode()
        result = self._request(
            "POST",
            "/ISAPI/AccessControl/CaptureFingerPrint",
            data=body,
            headers={'Content-Type': 'application/xml; charset=UTF-8',
                     'Accept': 'application/xml, application/json, multipart/form-data', 'Connection': 'close'},
        )
        attempts = ['XML capture: ' + (response_message(result) if not result.ok else 'Accepted')]
        if format_rejected(result):
            result = self._request('POST', '/ISAPI/AccessControl/CaptureFingerPrint?format=json',
                                   json={'CaptureFingerPrintCond': {'fingerNo': finger_no}},
                                   headers={'Connection': 'close'})
            attempts.append('JSON capture: ' + (response_message(result) if not result.ok else 'Accepted'))
        if not result.ok:
            return CapturedFingerprint(message='; '.join(attempts) + '. Use Check terminal compatibility for this station.')
        capture = result.payload.get("CaptureFingerPrint", result.payload.get("FingerPrintCapture", {}))
        if not isinstance(capture, dict):
            return CapturedFingerprint(message='The terminal returned an invalid fingerprint capture response.')
        if str(capture.get('fingerNo', finger_no)) != str(finger_no):
            return CapturedFingerprint(message='The terminal returned a different fingerprint slot; nothing was saved.')
        quality = capture.get("fingerPrintQuality", capture.get("quality"))
        try:
            quality = int(quality) if quality is not None else None
        except (TypeError, ValueError):
            quality = None
        return CapturedFingerprint(
            data=str(capture.get("fingerData", capture.get("fingerPrintData", "")) or ""),
            quality=quality,
            message=str(capture.get("statusString", "") or ""),
        )

    def apply_fingerprint(self, employee_no: str, finger_data: str, finger_no: int = 1) -> ApiResult:
        body = {
            "FingerPrintCfg": {
                "employeeNo": employee_no,
                "fingerPrintID": finger_no,
                "enableCardReader": [1],
                "checkEmployeeNo": True,
                "fingerType": "normalFP",
                "fingerData": finger_data,
            }
        }
        return self._request(
            "POST",
            "/ISAPI/AccessControl/FingerPrint/SetUp?format=json",
            json=body,
        )


    def search_users(self, employee_no=None):
        """Fetch all pages, or exactly one person, without inferring IDs from names."""
        search_id = str(uuid.uuid4())
        users = []
        seen_ids = set()
        position = 0
        while True:
            condition = {"searchID": search_id, "searchResultPosition": position, "maxResults": 30}
            if employee_no:
                condition["EmployeeNoList"] = [{"employeeNo": str(employee_no)}]
            result = self._request("POST", "/ISAPI/AccessControl/UserInfo/Search?format=json",
                                   json={"UserInfoSearchCond": condition})
            if not result.ok:
                raise HikvisionError("Could not read employees: " + response_message(result))
            search = result.payload.get("UserInfoSearch")
            if not isinstance(search, dict):
                raise HikvisionError("The terminal returned an invalid employee list.")
            batch = search.get("UserInfo", [])
            if isinstance(batch, dict):
                batch = [batch]
            if not isinstance(batch, list):
                raise HikvisionError("The terminal returned an invalid employee list.")
            batch_ids = [str(user.get('employeeNo') or '').strip()
                         for user in batch if isinstance(user, dict)]
            if len(batch_ids) != len(batch) or not all(batch_ids):
                raise HikvisionError("The terminal returned an invalid employee list.")
            if len(set(batch_ids)) != len(batch_ids) or seen_ids.intersection(batch_ids):
                raise HikvisionError("The terminal repeated an employee page. Please retry.")
            seen_ids.update(batch_ids)
            users.extend(batch)
            position += len(batch)
            try:
                total = int(search['totalMatches'])
            except (KeyError, TypeError, ValueError) as exc:
                raise HikvisionError("The terminal did not confirm the complete employee count.") from exc
            if total < position:
                raise HikvisionError("The terminal employee list changed during loading. Please retry.")
            if not batch and position < total:
                raise HikvisionError("The terminal returned an incomplete employee list. Please retry.")
            if position >= total:
                return users
            if position > 100000:
                raise HikvisionError("The terminal returned too many employee records.")

    def get_user(self, employee_no):
        for user in self.search_users(employee_no):
            if str(user.get("employeeNo")) == str(employee_no):
                return user
        return None

    def create_user(self, record):
        # Add must never overwrite a terminal's existing employee ID.
        if self.get_user(record["employee_no"]):
            raise HikvisionError("This employee ID already exists at the selected location.")
        result = self._request("POST", "/ISAPI/AccessControl/UserInfo/Record?format=json",
                               json=self.user_payload(record))
        if not result.ok:
            raise HikvisionError("Employee was not saved: " + response_message(result))
        saved = self.get_user(record["employee_no"])
        if not saved or saved.get("name") != record["name"]:
            raise HikvisionError("The terminal did not confirm the saved employee. Refresh its user list.")
        return saved

    def delete_user(self, employee_no):
        """Only explicit single-person deletion; confirm completion by re-querying."""
        import time
        employee_no = str(employee_no).strip()
        if not employee_no or not employee_no.isalnum():
            raise HikvisionError("A valid employee ID is required for deletion.")
        result = self._request("PUT", "/ISAPI/AccessControl/UserInfoDetail/Delete?format=json",
            json={"UserInfoDetail": {"mode": "byEmployeeNo",
                "EmployeeNoList": [{"employeeNo": employee_no}]}})
        if not result.ok:
            raise HikvisionError("Deletion was rejected: " + response_message(result))
        for attempt in range(10):
            if self.get_user(employee_no) is None:
                return
            if attempt < 9:
                time.sleep(0.5)
        raise HikvisionError("Deletion is not confirmed. Refresh the list before trying again.")
