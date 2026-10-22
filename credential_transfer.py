"""Copy one person's credentials in memory using Hikvision's ISAPI APIs.

No photographs, card numbers or fingerprint templates are saved to disk/session.
Standalone station terminals use face library 1 and destination reader 1.
"""
import hashlib
import json
import uuid
from io import BytesIO
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from urllib.parse import urlsplit
from PIL import Image, ImageChops, ImageOps, ImageStat, UnidentifiedImageError

from hikvision import ApiResult, HikvisionError, response_message


@dataclass(repr=False)
class Credentials:
    cards: list = field(default_factory=list)
    fingerprints: list = field(default_factory=list)
    face: bytes = b''

    def counts(self):
        return {'cards': len(self.cards), 'fingerprints': len(self.fingerprints),
                'faces': int(bool(self.face))}

    def manifest(self):
        # Only digests and counts may enter session state.
        digest = lambda value: hashlib.sha256(value).hexdigest()
        encoded = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        return {'cards': digest(encoded(sorted(self.cards, key=lambda c: c['cardNo']))),
                'fingerprints': digest(encoded(sorted(self.fingerprints, key=lambda f: f['fingerPrintID']))),
                'faces': digest(self.face), 'counts': self.counts()}


def checked(result, action):
    if not result.ok:
        # Never fall back to a raw response containing biometric data or URLs.
        status = result.payload.get('ResponseStatus', result.payload)
        safe = {key: status[key] for key in ('statusString', 'subStatusCode')
                if isinstance(status, dict) and key in status}
        if 'FingerPrintStatus' in result.payload:
            safe['FingerPrintStatus'] = result.payload['FingerPrintStatus']
        reason = response_message(ApiResult(False, result.status_code, safe))
        raise HikvisionError(f'{action}: {reason} Source employee remains active.')
    return result.payload


def records(value):
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(row, dict) for row in value):
        return value
    raise HikvisionError('The terminal returned an invalid credential list. Source employee remains active.')


def search_cards(client, employee_no=None, card_no=None):
    condition = {'searchID': uuid.uuid4().hex, 'searchResultPosition': 0, 'maxResults': 30}
    if employee_no is not None:
        condition['EmployeeNoList'] = [{'employeeNo': str(employee_no)}]
    elif card_no is not None:
        condition['CardNoList'] = [{'cardNo': str(card_no)}]
    else:
        raise ValueError('An employee ID or card number is required.')
    cards = []
    while True:
        payload = checked(client._request('POST', '/ISAPI/AccessControl/CardInfo/Search?format=json',
                          json={'CardInfoSearchCond': condition}), 'Could not read cards')
        search = payload.get('CardInfoSearch')
        if not isinstance(search, dict) or 'totalMatches' not in search:
            raise HikvisionError('The terminal did not confirm its card search results.')
        batch = records(search.get('CardInfo', []))
        cards.extend(batch)
        condition['searchResultPosition'] += len(batch)
        total = int(search['totalMatches'])
        if len(cards) >= total:
            break
        if not batch or len(cards) > 10000:
            raise HikvisionError('The terminal returned an incomplete card list.')
    for card in cards:
        if not card.get('cardNo') or not card.get('employeeNo'):
            raise HikvisionError('The terminal returned an invalid card record.')
        if employee_no is not None and str(card['employeeNo']) != str(employee_no):
            raise HikvisionError('The terminal returned cards for a different employee.')
        if card_no is not None and str(card['cardNo']) != str(card_no):
            raise HikvisionError('The terminal returned a different card than requested.')
    return cards


def read_fingerprints(client, employee_no):
    payload = checked(client._request('POST', '/ISAPI/AccessControl/FingerPrintUpload?format=json',
        json={'FingerPrintCond': {'searchID': uuid.uuid4().hex, 'employeeNo': str(employee_no)}}),
        'Could not export fingerprints')
    info = payload.get('FingerPrintInfo')
    if not isinstance(info, dict) or info.get('status') not in ('OK', 'NoFP'):
        raise HikvisionError('The terminal did not confirm its fingerprint export results.')
    items = records(info.get('FingerPrintList', []))
    if info['status'] == 'OK' and not items:
        raise HikvisionError('The terminal returned an incomplete fingerprint export.')
    if info['status'] == 'NoFP' and items:
        raise HikvisionError('The terminal returned inconsistent fingerprint results.')
    found = {}
    for item in items:
        if 'employeeNo' in item and str(item['employeeNo']) != str(employee_no):
            raise HikvisionError('The terminal returned a fingerprint for a different employee.')
        finger_id = int(item.get('fingerPrintID', 0))
        if not 1 <= finger_id <= 10 or not item.get('fingerData'):
            raise HikvisionError('A fingerprint template could not be exported. Source employee remains active.')
        finger = {key: item[key] for key in ('fingerPrintID', 'fingerType', 'fingerData', 'leaderFP') if key in item}
        finger['fingerPrintID'] = finger_id
        finger.setdefault('fingerType', 'normalFP')
        if finger_id in found and found[finger_id] != finger:
            raise HikvisionError('Different reader templates use the same finger slot; automatic transfer cannot verify them.')
        found[finger_id] = finger
    return list(found.values())


def face_response(response):
    """Decode plain JSON and JSON+JPEG multipart responses without logging bytes."""
    content_type = response.headers.get('Content-Type', '')
    pictures = []
    if content_type.lower().startswith('multipart/'):
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + response.content)
        payload = None
        for part in message.walk():
            if part.get_content_type() == 'application/json':
                try:
                    payload = json.loads(part.get_payload(decode=True))
                except (ValueError, TypeError):
                    raise HikvisionError('The terminal returned invalid face metadata.') from None
            elif part.get_content_type().startswith('image/'):
                pictures.append(part.get_payload(decode=True))
        if not isinstance(payload, dict):
            raise HikvisionError('The terminal did not return face metadata.')
    else:
        try:
            payload = response.json()
        except ValueError:
            raise HikvisionError('The terminal does not provide a supported face export response.') from None
        if not isinstance(payload, dict):
            raise HikvisionError('The terminal returned invalid face metadata.')
    return payload, pictures


def read_face(client, employee_no):
    response = client._send('POST', '/ISAPI/Intelligent/FDLib/FDSearch?format=json',
        json={'searchID': uuid.uuid4().hex, 'searchResultPosition': 0, 'maxResults': 1,
              'faceLibType': 'blackFD', 'FDID': '1', 'FPID': str(employee_no)})
    payload, pictures = face_response(response)
    checked(ApiResult(response.ok and client._is_success(payload), response.status_code, payload),
            'Could not export the face photograph')
    result = payload.get('FDSearch', payload)
    if not isinstance(result, dict) or 'numOfMatches' not in result:
        raise HikvisionError('The terminal did not confirm its face search results.')
    if int(result['numOfMatches']) == 0 and int(result.get('totalMatches', 0)) == 0:
        return b''
    matches = records(result.get('MatchList', []))
    if len(matches) != 1 or int(result.get('totalMatches', 1)) != 1:
        raise HikvisionError('Automatic transfer requires one face photograph per employee in library 1.')
    match = matches[0]
    if str(match.get('FPID', '')) != str(employee_no):
        raise HikvisionError('The terminal returned a face photograph for a different employee.')
    if match.get('modelStatus') in ('unmodeled', 'modelingFailed'):
        raise HikvisionError('The terminal has not built a working face model. Source employee remains active.')
    # A model alone is not a portable photograph; never silently omit the photo.
    if len(pictures) == 1:
        image = pictures[0]
    elif not pictures and (match.get('faceURL') or match.get('picURL')):
        location = urlsplit(match.get('faceURL') or match['picURL'])
        # Terminals often return a private LAN address. Keep the configured public
        # station origin and use only the returned path/query, with no redirects.
        if not location.path.startswith('/') or location.path.startswith('//'):
            raise HikvisionError('The terminal returned an invalid face photograph address.')
        path = location.path + ('?' + location.query if location.query else '')
        download = client._send('GET', path, allow_redirects=False)
        if download.status_code != 200:
            raise HikvisionError('Could not download the enrolled face photograph. Source employee remains active.')
        image = download.content
    else:
        raise HikvisionError('The terminal did not export the enrolled face photograph. Source employee remains active.')
    if not isinstance(image, bytes) or not image.startswith(b'\xff\xd8\xff') or len(image) > 5 * 1024 * 1024:
        raise HikvisionError('The terminal did not export a valid JPEG face photograph.')
    try:
        with Image.open(BytesIO(image)) as photograph:
            photograph.verify()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise HikvisionError('The terminal exported an unreadable face photograph.') from None
    return image


def export_credentials(client, user, needed=None):
    employee_no = str(user['employeeNo'])
    bundle = Credentials()
    for kind, count_key, reader in (
        ('cards', 'numOfCard', lambda: search_cards(client, employee_no=employee_no)),
        ('fingerprints', 'numOfFP', lambda: read_fingerprints(client, employee_no)),
        ('faces', 'numOfFace', lambda: read_face(client, employee_no)),
    ):
        expected = int(user[count_key]) if count_key in user else None
        if needed is not None and not needed[kind]:
            continue
        if expected == 0:
            continue
        value = reader()
        if kind == 'cards':
            bundle.cards = [{key: card[key] for key in ('cardNo', 'cardType', 'leaderCard') if key in card}
                            for card in value]
            for card in bundle.cards:
                card['cardNo'] = str(card['cardNo'])
                card.setdefault('cardType', 'normalCard')
        elif kind == 'fingerprints':
            bundle.fingerprints = value
        else:
            bundle.face = value
        count = bundle.counts()[kind]
        if expected is not None and count != expected:
            raise HikvisionError(f'Could not export all {kind} reported by the terminal. Source employee remains active.')
    return bundle


def ensure_cards_available(client, employee_no, bundle):
    for card in bundle.cards:
        matches = search_cards(client, card_no=card['cardNo'])
        if any(str(item['employeeNo']) != str(employee_no) for item in matches):
            raise HikvisionError('A card is assigned to another employee at the destination. Source employee remains active.')


def copy_credentials(client, user, bundle):
    employee_no = str(user['employeeNo'])
    ensure_cards_available(client, employee_no, bundle)
    for card in bundle.cards:
        existing = search_cards(client, card_no=card['cardNo'])
        if existing:
            actual = {key: existing[0][key] for key in card if key in existing[0]}
            if actual != card:
                raise HikvisionError('The destination card settings differ; source employee remains active.')
            continue
        checked(client._request('POST', '/ISAPI/AccessControl/CardInfo/Record?format=json',
                json={'CardInfo': {**card, 'employeeNo': employee_no,
                                  'checkCardNo': True, 'checkEmployeeNo': True}}), 'Could not copy a card')
    current = read_fingerprints(client, employee_no) if bundle.fingerprints else []
    for finger in bundle.fingerprints:
        if finger in current:
            continue
        payload = checked(client._request('POST', '/ISAPI/AccessControl/FingerPrint/SetUp?format=json',
                json={'FingerPrintCfg': {**finger, 'employeeNo': employee_no,
                                        'enableCardReader': [1], 'checkEmployeeNo': True}}),
                          'Could not copy a fingerprint')
        status = payload.get('FingerPrintStatus', {})
        statuses = records(status.get('StatusList', []))
        if status.get('status', 'success') != 'success' or any(
                str(item.get('cardReaderRecvStatus')) != '1' for item in statuses):
            raise HikvisionError('The destination reader rejected a fingerprint. Source employee remains active.')
    if bundle.face:
        # Retrying a partial transfer may already have copied the face.
        existing_face = read_face(client, employee_no)
        method, path = 'POST', '/ISAPI/Intelligent/FDLib/FaceDataRecord?format=json'
        if existing_face:
            from urllib.parse import urlencode
            method = 'PUT'
            path = '/ISAPI/Intelligent/FDLib/FDSearch?' + urlencode(
                {'format': 'json', 'FDID': '1', 'FPID': employee_no, 'faceLibType': 'blackFD'})
        checked(client.upload_face(employee_no, bundle.face, name=user['name'], method=method, path=path),
                'Could not copy the face photograph')


def verify_credentials(client, user, expected):
    actual = export_credentials(client, user, needed=expected.counts())
    source, destination = expected.manifest(), actual.manifest()
    if source['cards'] != destination['cards'] or source['fingerprints'] != destination['fingerprints']:
        raise HikvisionError('Destination cards or fingerprints could not be verified. Source employee remains active.')
    # Terminals may recompress the photograph when building their own face model.
    if bool(expected.face) != bool(actual.face):
        raise HikvisionError('Destination face photograph could not be verified. Source employee remains active.')
    if expected.face:
        with Image.open(BytesIO(expected.face)) as before, Image.open(BytesIO(actual.face)) as after:
            before = ImageOps.exif_transpose(before).convert('RGB').resize((32, 32))
            after = ImageOps.exif_transpose(after).convert('RGB').resize((32, 32))
            # Compare picture content, allowing normal JPEG recompression. This
            # is image-copy validation, not a biometric identity comparison.
            difference = ImageStat.Stat(ImageChops.difference(before, after))
            if max(difference.mean) > 12:
                raise HikvisionError('Destination face photograph differs from the source. Source employee remains active.')
    return destination
