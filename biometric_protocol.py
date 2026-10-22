"""Wire formats for Hikvision biometric enrollment. Data stays in memory."""
import io
import json
import uuid
import xml.etree.ElementTree as ET
from email import policy
from email.parser import BytesParser

from PIL import Image, ImageOps, UnidentifiedImageError


def decode_payload(response):
    """Read JSON, namespaced XML, or the metadata part of multipart replies."""
    content_type = response.headers.get('Content-Type', '')
    if content_type.lower().startswith('multipart/'):
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + response.content)
        for part in message.walk():
            if part.get_content_type() in ('application/json', 'application/xml', 'text/xml'):
                decoded = decode_document(part.get_payload(decode=True))
                if decoded:
                    return decoded
        return {}
    return decode_document(response.content)


def decode_document(raw):
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {'data': data}
    except (ValueError, TypeError, UnicodeDecodeError):
        pass
    if not isinstance(raw, bytes) or b'<!DOCTYPE' in raw.upper():
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}

    def convert(element):
        if not len(element):
            return (element.text or '').strip()
        result = {}
        for child in element:
            name = child.tag.rsplit('}', 1)[-1]
            value = convert(child)
            if name in result:
                if not isinstance(result[name], list):
                    result[name] = [result[name]]
                result[name].append(value)
            else:
                result[name] = value
        return result
    return {root.tag.rsplit('}', 1)[-1]: convert(root)}


def prepare_face_jpeg(image):
    if not image:
        raise ValueError('Choose a JPEG face photograph before saving.')
    if len(image) > 20 * 1024 * 1024:
        raise ValueError('Choose a face photograph smaller than 20 MB.')
    try:
        with Image.open(io.BytesIO(image)) as original:
            if original.format != 'JPEG':
                raise ValueError('Choose a valid JPEG face photograph.')
            photograph = ImageOps.exif_transpose(original).convert('RGB')
            if min(photograph.size) < 80:
                raise ValueError('Use a clear face photograph at least 80 × 80 pixels.')
            photograph.thumbnail((640, 640), Image.Resampling.LANCZOS)
            if min(photograph.size) < 80:
                raise ValueError('Use a portrait photograph with the face clearly visible.')
            for quality in (90, 80, 70, 60):
                output = io.BytesIO()
                photograph.save(output, 'JPEG', quality=quality, optimize=True)
                if output.tell() <= 200 * 1024:
                    return output.getvalue()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise ValueError('The selected file is not a readable JPEG photograph.') from None
    raise ValueError('Choose a smaller face photograph; the terminal limit is 200 KB.')


def face_multipart(metadata, image, image_field='FaceImage'):
    """Metadata first, binary JPEG second, with the lengths strict firmware needs."""
    boundary = 'hikvision_' + uuid.uuid4().hex
    encoded = json.dumps(metadata, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    chunks = []
    for name, content_type, value in (
        ('FaceDataRecord', 'application/json', encoded), (image_field, 'image/jpeg', image)
    ):
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}";\r\n'
                       f'Content-Type: {content_type}\r\nContent-Length: {len(value)}\r\n\r\n').encode())
        chunks.extend((value, b'\r\n'))
    chunks.append(f'--{boundary}--\r\n'.encode())
    return {'data': b''.join(chunks), 'headers': {'Content-Type': f'multipart/form-data; boundary={boundary}'}}


def format_rejected(result):
    status = result.payload.get('ResponseStatus', result.payload)
    if not isinstance(status, dict):
        return False
    return not result.ok and str(status.get('subStatusCode', '')).lower() in {
        'badxmlcontent', 'badxmlformat', 'badjsoncontent', 'badjsonformat',
        'invalidcontent', 'notsupport', 'methodnotallowed',
    }
