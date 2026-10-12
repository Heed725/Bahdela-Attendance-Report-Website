from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests
from requests import Response
from requests.auth import HTTPDigestAuth


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
    status = payload.get("ResponseStatus", payload)
    for key in ("statusString", "subStatusCode", "errorMsg", "message"):
        value = status.get(key) if isinstance(status, dict) else None
        if value:
            return str(value)
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

    def _request(self, method: str, path: str, **kwargs: Any) -> ApiResult:
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                timeout=self.timeout,
                verify=self.verify_tls,
                **kwargs,
            )
        except requests.Timeout as exc:
            raise HikvisionError("The Hikvision terminal timed out. Check its address and network connection.") from exc
        except requests.RequestException as exc:
            raise HikvisionError(f"Could not connect to the Hikvision terminal: {exc}") from exc

        payload = self._json(response)
        ok = response.ok and self._is_success(payload)
        return ApiResult(ok=ok, status_code=response.status_code, payload=payload, text=response.text)

    @staticmethod
    def _json(response: Response) -> dict[str, Any]:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {"data": data}
        except ValueError:
            return {}

    @staticmethod
    def _is_success(payload: dict[str, Any]) -> bool:
        status = payload.get("ResponseStatus", payload)
        if not isinstance(status, dict) or not payload:
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
        result = self._request("PUT", endpoint, json=body)
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

    def upload_face(self, employee_no: str, image: bytes, filename: str = "face.jpg") -> ApiResult:
        if not image:
            raise HikvisionError("The selected face photograph is empty.")
        metadata = {"faceLibType": "blackFD", "FDID": "1", "FPID": employee_no}
        files = {
            "FaceDataRecord": (None, json.dumps(metadata), "application/json"),
            "FaceImage": (filename, image, "image/jpeg"),
        }
        return self._request("POST", "/ISAPI/Intelligent/FDLib/FaceDataRecord?format=json", files=files)

    def capture_fingerprint(self, finger_no: int = 1) -> CapturedFingerprint:
        body = {"CaptureFingerPrint": {"fingerNo": finger_no}}
        result = self._request(
            "POST",
            "/ISAPI/AccessControl/CaptureFingerPrint?format=json",
            json=body,
        )
        if not result.ok:
            return CapturedFingerprint(message=response_message(result))
        capture = result.payload.get("CaptureFingerPrint", result.payload.get("FingerPrintCapture", {}))
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
            users.extend(batch)
            position += len(batch)
            total = int(search.get("totalMatches", position) or 0)
            if not batch or position >= total:
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
        result = self._request("PUT", "/ISAPI/AccessControl/UserInfo/Record?format=json",
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
