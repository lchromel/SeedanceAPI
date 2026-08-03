#!/usr/bin/env python3
import cgi
import json
import base64
import binascii
import datetime
import hashlib
import hmac
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


APP_HOST = os.environ.get("HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("PORT", "8080"))
TOKEN_FILE = os.path.expanduser(os.environ.get("TOKEN_FILE", "~/Desktop/tokens.txt"))
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.abspath(
    os.environ.get("SEEDANCE_DATA_DIR")
    or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or ROOT_DIR
)
UPLOAD_DIR = os.path.join(DATA_ROOT, "uploads")
MATERIAL_LIBRARY_FILE = os.path.join(DATA_ROOT, "material_library.json")
MATERIAL_LIBRARY_LOCK = threading.Lock()
AIGC_GROUP_LOCK = threading.Lock()
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
ALLOWED_UPLOAD_MIME_PREFIXES = ("image/", "video/", "audio/")


PROVIDERS = {
    "byteplus": {
        "name": "BytePlus Ark / Seedance 2.0",
        "base_url": "https://ark.ap-southeast.bytepluses.com/api/v3",
        "submit_path": "/contents/generations/tasks",
        "status_path": "/contents/generations/tasks/{task_id}",
        "models": ["dreamina-seedance-2-0-260128"],
        "durations": list(range(4, 16)),
        "ratios": ["auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
        "resolutions": ["480p", "720p", "1080p"],
        "token_names": ["ARK_API_KEY", "BYTEPLUS_ARK_API_KEY", "BYTEPLUS_API_KEY", "SEEDANCE_API_KEY"],
        "endpoint_names": ["SEEDANCE_ENDPOINT_ID", "BYTEPLUS_ARK_ENDPOINT_ID", "ARK_ENDPOINT_ID"],
    },
    "seedanceapi": {
        "name": "SD 2.0 API",
        "base_url": "https://seedanceapi.org",
        "submit_path": "/v2/generate",
        "status_path": "/v2/status",
        "models": ["seedance-2.0", "seedance-2.0-fast"],
        "durations": [5, 10, 15],
        "ratios": ["16:9", "9:16", "4:3", "3:4"],
        "resolutions": [],
        "token_names": ["SEEDANCE_API_KEY", "SD20_API_KEY"],
    },
    "reapi": {
        "name": "reAPI / doubao-seedance-2.0",
        "base_url": "https://reapi.ai",
        "submit_path": "/api/v1/videos/generations",
        "status_path": "/api/v1/tasks/{task_id}",
        "models": [
            "doubao-seedance-2.0",
            "doubao-seedance-2.0-fast",
            "doubao-seedance-2.0-face",
            "doubao-seedance-2.0-fast-face",
        ],
        "durations": list(range(4, 16)),
        "ratios": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"],
        "resolutions": ["480p", "720p", "1080p"],
        "token_names": ["REAPI_API_KEY", "SEEDANCE_API_KEY"],
    },
}

BYTEPLUS_IMAGE_SUBMIT_PATH = "/images/generations"
BYTEPLUS_IMAGE_MODELS = ["seedream-5-0-260128"]
BYTEPLUS_IMAGE_ENDPOINT_NAMES = ["SEEDREAM_ENDPOINT_ID", "BYTEPLUS_SEEDREAM_ENDPOINT_ID", "ARK_IMAGE_ENDPOINT_ID"]
BYTEPLUS_ASSET_API_BASE_URL = os.environ.get(
    "BYTEPLUS_ASSET_API_BASE_URL",
    "https://ark.ap-southeast-1.byteplusapi.com",
).rstrip("/")
BYTEPLUS_ASSET_REGION = os.environ.get("BYTEPLUS_ASSET_REGION", "ap-southeast-1")
BYTEPLUS_ASSET_PROJECT = os.environ.get("BYTEPLUS_ASSET_PROJECT", "default")
BYTEPLUS_AIGC_GROUP_NAME = os.environ.get(
    "BYTEPLUS_AIGC_GROUP_NAME", "seedance-generated-heroes"
)
BYTEPLUS_ACCESS_KEY_NAMES = [
    "BYTEPLUS_ACCESS_KEY_ID",
    "BYTEPLUS_ACCESS_KEY",
    "BYTEPLUS_AK",
    "ARK_ACCESS_KEY_ID",
]
BYTEPLUS_SECRET_KEY_NAMES = [
    "BYTEPLUS_SECRET_ACCESS_KEY",
    "BYTEPLUS_SECRET_KEY",
    "BYTEPLUS_SK",
    "ARK_SECRET_ACCESS_KEY",
]


def read_token_file():
    values = {}
    if not os.path.exists(TOKEN_FILE):
        return values
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return values
    return values


TOKEN_VALUES = read_token_file()


def get_secret(names):
    for name in names:
        value = os.environ.get(name) or TOKEN_VALUES.get(name)
        if value:
            return value
    return ""


def default_model_for_provider(provider):
    endpoint_id = get_secret(provider.get("endpoint_names", []))
    if endpoint_id:
        return endpoint_id
    return provider["models"][0]


def default_byteplus_image_model():
    endpoint_id = get_secret(BYTEPLUS_IMAGE_ENDPOINT_NAMES)
    if endpoint_id:
        return endpoint_id
    return BYTEPLUS_IMAGE_MODELS[0]


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler, status, body, content_type="text/html; charset=utf-8"):
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def file_response(handler, path):
    if not os.path.isfile(path):
        json_response(handler, 404, {"error": "File not found"})
        return
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    size = os.path.getsize(path)
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
    handler.send_header("Content-Length", str(size))
    handler.end_headers()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 256)
            if not chunk:
                break
            handler.wfile.write(chunk)


def public_base_url(handler):
    proto = handler.headers.get("X-Forwarded-Proto", "")
    host = handler.headers.get("X-Forwarded-Host", "") or handler.headers.get("Host", "")
    if not proto:
        proto = "https" if handler.headers.get("X-Forwarded-Ssl", "").lower() == "on" else "http"
    if not host:
        host = f"127.0.0.1:{APP_PORT}"
    return f"{proto}://{host}"


def safe_upload_name(original_name, content_type):
    stem = os.path.basename(str(original_name or "")).strip()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")
    guessed_ext = mimetypes.guess_extension(content_type or "") or ""
    if not stem:
        stem = f"reference{guessed_ext}"
    if "." not in stem and guessed_ext:
        stem += guessed_ext
    return f"{int(time.time())}-{uuid.uuid4().hex[:12]}-{stem}"


def save_upload(field):
    file_obj = getattr(field, "file", None)
    if file_obj is None:
        raise ValueError("file is required")
    original_name = str(getattr(field, "filename", "") or "").strip()
    content_type = str(field.type or mimetypes.guess_type(original_name)[0] or "application/octet-stream")
    if not any(content_type.startswith(prefix) for prefix in ALLOWED_UPLOAD_MIME_PREFIXES):
        raise ValueError("Only image, video, and audio uploads are supported")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    temp_name = f".upload-{uuid.uuid4().hex}.tmp"
    temp_path = os.path.join(UPLOAD_DIR, temp_name)
    total = 0
    digest = hashlib.sha256()
    with open(temp_path, "wb") as output:
        while True:
            chunk = file_obj.read(1024 * 256)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                output.close()
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                raise ValueError(f"File is too large. Limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
            digest.update(chunk)
            output.write(chunk)
    if total <= 0:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise ValueError("Uploaded file is empty")
    sha256 = digest.hexdigest()
    original_safe_name = safe_upload_name(original_name, content_type).split("-", 2)[-1]
    file_name = f"{sha256[:20]}-{original_safe_name}"
    path = os.path.join(UPLOAD_DIR, file_name)
    if os.path.exists(path):
        os.remove(temp_path)
    else:
        os.replace(temp_path, path)
    return {
        "fileName": file_name,
        "originalName": original_name,
        "contentType": content_type,
        "size": total,
        "path": path,
        "sha256": sha256,
    }


def save_generated_data_url(data_url, original_name="generated-hero.png"):
    match = re.fullmatch(
        r"data:(image/[A-Za-z0-9.+-]+);base64,(.+)",
        str(data_url or "").strip(),
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError("Generated result must be a public HTTP(S) image URL")
    content_type, encoded = match.groups()
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Generated image data is not valid base64") from exc
    if not content:
        raise ValueError("Generated image is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"Generated image is too large. Limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    safe_name = safe_upload_name(original_name, content_type).split("-", 2)[-1]
    file_name = f"{digest[:20]}-{safe_name}"
    path = os.path.join(UPLOAD_DIR, file_name)
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(content)
    return file_name


def utc_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load_material_records_unlocked():
    if not os.path.exists(MATERIAL_LIBRARY_FILE):
        return []
    try:
        with open(MATERIAL_LIBRARY_FILE, "r", encoding="utf-8") as handle:
            records = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(records, list):
        return []
    return [item for item in records if isinstance(item, dict)]


def _save_material_records_unlocked(records):
    os.makedirs(DATA_ROOT, exist_ok=True)
    temp_path = MATERIAL_LIBRARY_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(records, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, MATERIAL_LIBRARY_FILE)


def public_material_record(record, base_url=""):
    file_name = str(record.get("fileName") or "").strip()
    relative_url = f"/uploads/{urllib.parse.quote(file_name)}" if file_name else ""
    return {
        "id": str(record.get("id") or "").strip(),
        "url": (str(base_url).rstrip("/") + relative_url) if base_url and relative_url else relative_url,
        "fileName": file_name,
        "originalName": str(record.get("originalName") or "").strip(),
        "contentType": str(record.get("contentType") or "").strip(),
        "size": int(record.get("size") or 0),
        "sha256": str(record.get("sha256") or "").strip(),
        "createdAt": str(record.get("createdAt") or "").strip(),
        "updatedAt": str(record.get("updatedAt") or "").strip(),
        "assetId": str(record.get("assetId") or "").strip(),
        "assetUri": str(record.get("assetUri") or "").strip(),
        "assetStatus": str(record.get("assetStatus") or "Local").strip() or "Local",
        "assetGroupId": str(record.get("assetGroupId") or "").strip(),
        "assetProject": str(record.get("assetProject") or "").strip(),
        "assetError": str(record.get("assetError") or "").strip(),
        "submittedAt": str(record.get("submittedAt") or "").strip(),
    }


def list_material_records(base_url=""):
    with MATERIAL_LIBRARY_LOCK:
        records = _load_material_records_unlocked()
        visible = []
        changed = False
        for record in records:
            file_name = str(record.get("fileName") or "").strip()
            if not file_name or not os.path.isfile(os.path.join(UPLOAD_DIR, file_name)):
                changed = True
                continue
            visible.append(record)
        visible.sort(key=lambda item: str(item.get("createdAt") or ""), reverse=True)
        if changed:
            _save_material_records_unlocked(visible)
        return [public_material_record(item, base_url) for item in visible]


def upsert_material_record(saved, base_url=""):
    now = utc_timestamp()
    with MATERIAL_LIBRARY_LOCK:
        records = _load_material_records_unlocked()
        record = None
        for item in records:
            if str(item.get("sha256") or "") == str(saved.get("sha256") or ""):
                record = item
                break
        if record is None:
            record = {
                "id": uuid.uuid4().hex,
                "createdAt": now,
                "assetStatus": "Local",
            }
            records.append(record)
        record.update(
            {
                "fileName": saved["fileName"],
                "originalName": saved["originalName"],
                "contentType": saved["contentType"],
                "size": saved["size"],
                "sha256": saved["sha256"],
                "updatedAt": now,
            }
        )
        records.sort(key=lambda item: str(item.get("createdAt") or ""), reverse=True)
        _save_material_records_unlocked(records)
        return public_material_record(record, base_url)


def get_material_record(material_id):
    normalized_id = str(material_id or "").strip()
    with MATERIAL_LIBRARY_LOCK:
        for record in _load_material_records_unlocked():
            if str(record.get("id") or "").strip() == normalized_id:
                return dict(record)
    return None


def update_material_record(material_id, values, base_url=""):
    normalized_id = str(material_id or "").strip()
    with MATERIAL_LIBRARY_LOCK:
        records = _load_material_records_unlocked()
        for record in records:
            if str(record.get("id") or "").strip() != normalized_id:
                continue
            record.update(values)
            record["updatedAt"] = utc_timestamp()
            _save_material_records_unlocked(records)
            return public_material_record(record, base_url)
    return None


def read_json_body(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def provider_config(provider_id):
    provider = PROVIDERS.get(provider_id)
    if not provider:
        raise ValueError("Unknown provider")
    return provider


def endpoint_url(provider, path, task_id=None, base_url=None):
    root = (base_url or provider["base_url"]).rstrip("/")
    if task_id:
        path = path.replace("{task_id}", urllib.parse.quote(task_id, safe=""))
    return root + path


def request_json(method, url, api_key, payload=None, timeout=45):
    body = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            error_payload = json.loads(raw)
        except json.JSONDecodeError:
            error_payload = {"message": raw or exc.reason}
        return exc.code, error_payload
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def _sha256_hex(value):
    return hashlib.sha256(value).hexdigest()


def _hmac_sha256(key, value):
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def request_byteplus_asset_api(action, payload=None, timeout=45):
    access_key = get_secret(BYTEPLUS_ACCESS_KEY_NAMES)
    secret_key = get_secret(BYTEPLUS_SECRET_KEY_NAMES)
    if not access_key or not secret_key:
        raise PermissionError(
            "Assets API требует AK/SK. Добавьте BYTEPLUS_ACCESS_KEY_ID и "
            f"BYTEPLUS_SECRET_ACCESS_KEY в Railway env или в {TOKEN_FILE}."
        )

    body = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    parsed = urllib.parse.urlparse(BYTEPLUS_ASSET_API_BASE_URL)
    host = parsed.netloc
    query = urllib.parse.urlencode({"Action": action, "Version": "2024-01-01"})
    request_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short_date = request_date[:8]
    payload_hash = _sha256_hex(body)
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_headers = (
        "content-type:application/json\n"
        f"host:{host}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{request_date}\n"
    )
    canonical_request = "\n".join(
        ["POST", "/", query, canonical_headers, signed_headers, payload_hash]
    )
    credential_scope = f"{short_date}/{BYTEPLUS_ASSET_REGION}/ark/request"
    string_to_sign = "\n".join(
        [
            "HMAC-SHA256",
            request_date,
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )
    key_date = _hmac_sha256(secret_key.encode("utf-8"), short_date)
    key_region = _hmac_sha256(key_date, BYTEPLUS_ASSET_REGION)
    key_service = _hmac_sha256(key_region, "ark")
    signing_key = _hmac_sha256(key_service, "request")
    signature = hmac.new(
        signing_key,
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    authorization = (
        f"HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    url = f"{BYTEPLUS_ASSET_API_BASE_URL}/?{query}"
    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json",
        "Host": host,
        "X-Content-Sha256": payload_hash,
        "X-Date": request_date,
    }
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            response_payload = json.loads(raw) if raw else {}
            return response.status, response_payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            response_payload = json.loads(raw)
        except json.JSONDecodeError:
            response_payload = {"message": raw or exc.reason}
        return exc.code, response_payload
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Assets API network error: {exc.reason}") from exc


def unwrap_byteplus_asset_response(status_code, payload):
    metadata = payload.get("ResponseMetadata") if isinstance(payload, dict) else None
    metadata_error = metadata.get("Error") if isinstance(metadata, dict) else None
    if status_code >= 400 or metadata_error:
        message = provider_error_message(payload) or f"Assets API returned HTTP {status_code}"
        raise RuntimeError(message)
    result = payload.get("Result") if isinstance(payload, dict) else None
    return result if isinstance(result, dict) else payload


def call_byteplus_asset_api(action, payload=None, timeout=45):
    status_code, response_payload = request_byteplus_asset_api(action, payload, timeout)
    return unwrap_byteplus_asset_response(status_code, response_payload)


def ensure_aigc_asset_group(project_name, group_name=BYTEPLUS_AIGC_GROUP_NAME):
    normalized_project = str(project_name or BYTEPLUS_ASSET_PROJECT).strip()
    normalized_name = str(group_name or BYTEPLUS_AIGC_GROUP_NAME).strip()
    with AIGC_GROUP_LOCK:
        groups = call_byteplus_asset_api(
            "ListAssetGroups",
            {
                "Filter": {"GroupType": "AIGC", "Name": normalized_name},
                "PageNumber": 1,
                "PageSize": 100,
                "ProjectName": normalized_project,
            },
        )
        items = groups.get("Items") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("Name") or "").strip() != normalized_name:
                continue
            group_id = str(item.get("Id") or item.get("GroupId") or "").strip()
            if group_id:
                return group_id
        created = call_byteplus_asset_api(
            "CreateAssetGroup",
            {
                "GroupType": "AIGC",
                "Name": normalized_name,
                "Title": "Generated heroes",
                "Description": "Heroes generated in Seedance Studio",
                "ProjectName": normalized_project,
            },
        )
        group_id = str(created.get("Id") or created.get("GroupId") or "").strip()
        if not group_id:
            raise RuntimeError("BytePlus did not return an AIGC asset group ID")
        return group_id


def split_urls(value):
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[\n,]+", str(value or ""))
    return [item.strip() for item in items if item and item.strip()]


def walk_json_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from walk_json_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_json_values(item)


def remote_image_as_data_url(url):
    normalized = str(url or "").strip()
    if not normalized or normalized.startswith(("data:", "asset://")):
        return normalized
    request = urllib.request.Request(
        normalized,
        headers={
            "User-Agent": "SeedanceWeb/1.0",
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*,*/*;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
    if not content_type or not content_type.startswith("image/"):
        guessed, _ = mimetypes.guess_type(urllib.parse.urlparse(normalized).path)
        content_type = guessed if guessed and guessed.startswith("image/") else "image/png"
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def redact_large_values(value):
    if isinstance(value, dict):
        return {key: redact_large_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_large_values(item) for item in value]
    if isinstance(value, str) and value.startswith("data:") and len(value) > 120:
        return value[:80] + "...[redacted]"
    return value


def provider_error_message(payload):
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error") or payload.get("Error")
    if isinstance(error, dict):
        code = str(error.get("code") or error.get("Code") or "").strip()
        message = str(error.get("message") or error.get("Message") or "").strip()
        if code == "InputImageSensitiveContentDetected.PrivacyInformation":
            return (
                "BytePlus rejected the reference image because it may contain a real person "
                "or privacy-sensitive biometric information. Use a non-identifiable, synthetic, "
                "licensed model image, or generate without an image reference."
            )
        return f"{code}: {message}".strip(": ") or message or code
    if error:
        return str(error).strip()
    for key, value in walk_json_values(payload):
        if str(key).lower() in {"error", "message", "error_message"} and value:
            return str(value).strip()
    return ""


def build_submit_payload(provider_id, data):
    prompt = str(data.get("prompt", "")).strip()
    if not prompt and not any(data.get(name) for name in ("imageUrls", "firstFrameUrl", "lastFrameUrl", "videoUrls", "audioUrls")):
        raise ValueError("Введите prompt или добавьте хотя бы один reference URL.")

    model = data.get("endpoint") or data.get("model") or default_model_for_provider(PROVIDERS[provider_id])
    duration = int(data.get("duration") or 5)
    ratio = data.get("aspectRatio") or "16:9"
    image_urls = split_urls(data.get("imageUrls"))
    video_urls = split_urls(data.get("videoUrls"))
    audio_urls = split_urls(data.get("audioUrls"))

    if provider_id == "byteplus":
        if not prompt:
            raise ValueError("Prompt is required for BytePlus Seedance generation.")
        content = []
        if prompt:
            content.append({"type": "text", "text": prompt})
        max_reference_items = max(0, 5 - len(content))
        reference_items_used = 0

        first_frame = str(data.get("firstFrameUrl") or "").strip()
        last_frame = str(data.get("lastFrameUrl") or "").strip()
        uses_reference_media = bool(image_urls or video_urls or audio_urls)
        if (first_frame or last_frame) and not uses_reference_media:
            for image_url, role in ((first_frame, "first_frame"), (last_frame, "last_frame")):
                if not image_url:
                    continue
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": remote_image_as_data_url(image_url)},
                        "role": role,
                    }
                )
        else:
            for image_url in image_urls[:9]:
                if reference_items_used >= max_reference_items:
                    break
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": remote_image_as_data_url(image_url)},
                        "role": "reference_image",
                    }
                )
                reference_items_used += 1
        for video_url in video_urls[:3]:
            if reference_items_used >= max_reference_items:
                break
            content.append({"type": "video_url", "video_url": {"url": video_url}, "role": "reference_video"})
            reference_items_used += 1
        for audio_url in audio_urls[:3]:
            if reference_items_used >= max_reference_items:
                break
            content.append({"type": "audio_url", "audio_url": {"url": audio_url}, "role": "reference_audio"})
            reference_items_used += 1

        payload = {
            "model": model,
            "content": content,
            "ratio": ratio or "auto",
            "duration": max(4, min(15, duration)),
            "resolution": data.get("resolution") or "720p",
            "generate_audio": bool(data.get("generateAudio")),
        }
        if data.get("seed") not in (None, ""):
            payload["seed"] = int(data["seed"])
        return payload

    if provider_id == "seedanceapi":
        payload = {
            "prompt": prompt,
            "duration": duration,
            "model": model,
        }
        if image_urls:
            payload["images"] = image_urls[:4]
        else:
            payload["aspect_ratio"] = ratio
        if data.get("callbackUrl"):
            payload["callback_url"] = str(data["callbackUrl"]).strip()
        return payload

    payload = {
        "model": model,
        "duration": duration,
        "size": ratio,
    }
    if prompt:
        payload["prompt"] = prompt
    if data.get("resolution"):
        payload["resolution"] = data["resolution"]
    if data.get("seed") not in (None, ""):
        payload["seed"] = int(data["seed"])
    if data.get("generateAudio"):
        payload["generate_audio"] = True
    if data.get("returnLastFrame"):
        payload["return_last_frame"] = True

    first_frame = str(data.get("firstFrameUrl") or "").strip()
    last_frame = str(data.get("lastFrameUrl") or "").strip()
    if first_frame or last_frame:
        frames = []
        if first_frame:
            frames.append({"url": first_frame, "role": "first_frame"})
        if last_frame:
            frames.append({"url": last_frame, "role": "last_frame"})
        payload["image_with_roles"] = frames
    elif image_urls:
        payload["image_urls"] = image_urls[:9]

    if video_urls:
        payload["video_urls"] = video_urls[:3]
    if audio_urls:
        payload["audio_urls"] = audio_urls[:3]
    if data.get("webSearch"):
        payload["tools"] = [{"type": "web_search"}]
    return payload


def build_image_payload(provider_id, data):
    if provider_id != "byteplus":
        raise ValueError("Seedream image generation is supported only through BytePlus Ark.")
    prompt = str(data.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("Введите prompt для генерации изображения.")

    image_urls = split_urls(data.get("imageUrls"))
    payload = {
        "model": str(data.get("imageModel") or default_byteplus_image_model()).strip(),
        "prompt": prompt,
        "size": str(data.get("imageSize") or "2K").strip(),
        "response_format": "url",
        "watermark": bool(data.get("imageWatermark")),
    }
    output_format = str(data.get("imageOutputFormat") or "png").strip().lower()
    if output_format in {"png", "jpeg"}:
        payload["output_format"] = output_format
    if image_urls:
        payload["image"] = [remote_image_as_data_url(url) for url in image_urls[:10]]
    return payload


def normalize_image_generation(status_code, payload):
    data = payload.get("Result") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else payload if isinstance(payload, dict) else {}
    image_urls = []
    images = []
    candidates = []
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        candidates.append(item)
        for nested in item.get("imagecontent") or item.get("image_content") or []:
            if isinstance(nested, dict):
                candidates.append(nested)
    for item in candidates:
        url = item.get("url")
        b64_json = item.get("b64_json")
        if isinstance(url, str) and url:
            image_urls.append(url)
            images.append({"url": url, "size": item.get("size")})
        elif isinstance(b64_json, str) and b64_json:
            data_url = b64_json if b64_json.startswith("data:") else f"data:image/png;base64,{b64_json}"
            image_urls.append(data_url)
            images.append({"url": data_url, "size": item.get("size")})
    error = data.get("error") or data.get("Error")
    return {
        "imageUrls": image_urls,
        "images": images,
        "model": data.get("model"),
        "created": data.get("created"),
        "usage": data.get("usage"),
        "error": error,
        "raw": payload,
        "ok": 200 <= status_code < 300 and bool(image_urls) and not error,
    }


def normalize_submit(provider_id, status_code, payload):
    if provider_id == "byteplus":
        result = payload.get("Result") if isinstance(payload, dict) else None
        data = result if isinstance(result, dict) else payload
        task_id = data.get("TaskId") or data.get("task_id") or data.get("id")
        return {
            "taskId": task_id,
            "status": data.get("TaskStatus") or data.get("status") or "IN_PROGRESS",
            "raw": payload,
            "ok": 200 <= status_code < 300 and bool(task_id),
        }
    if provider_id == "seedanceapi":
        data = payload.get("data") or payload
        return {
            "taskId": data.get("task_id") or data.get("id"),
            "status": data.get("status", "IN_PROGRESS"),
            "raw": payload,
            "ok": 200 <= status_code < 300 and bool(data.get("task_id") or data.get("id")),
        }
    return {
        "taskId": payload.get("id") or payload.get("task_id"),
        "status": payload.get("status", "processing"),
        "raw": payload,
        "ok": 200 <= status_code < 300 and bool(payload.get("id") or payload.get("task_id")),
    }


def normalize_status(provider_id, payload):
    if provider_id == "byteplus":
        result = payload.get("Result") if isinstance(payload, dict) else None
        data = result if isinstance(result, dict) else payload
        video_urls = []
        for key, value in walk_json_values(data):
            key_name = str(key).lower()
            if key_name in {"video_url", "videourl", "url", "video"} and isinstance(value, str):
                if value.startswith(("http://", "https://")) and value not in video_urls:
                    video_urls.append(value)
            if key_name in {"video_urls", "videourls", "videos"} and isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.startswith(("http://", "https://")) and item not in video_urls:
                        video_urls.append(item)
        error = payload.get("Error") or payload.get("error") if isinstance(payload, dict) else None
        return {
            "taskId": data.get("TaskId") or data.get("task_id") or data.get("id"),
            "status": data.get("TaskStatus") or data.get("status") or "UNKNOWN",
            "videoUrls": video_urls,
            "lastFrameUrl": data.get("LastFrameUrl") or data.get("last_frame_url"),
            "error": error,
            "raw": payload,
        }
    if provider_id == "seedanceapi":
        data = payload.get("data") or payload
        urls = data.get("response") or data.get("video_urls") or []
        return {
            "taskId": data.get("task_id") or data.get("id"),
            "status": data.get("status") or "UNKNOWN",
            "videoUrls": urls if isinstance(urls, list) else [urls],
            "lastFrameUrl": data.get("last_frame_url"),
            "error": data.get("error_message") or data.get("error"),
            "raw": payload,
        }
    output = payload.get("output") or {}
    return {
        "taskId": payload.get("id") or payload.get("task_id"),
        "status": payload.get("status") or "UNKNOWN",
        "videoUrls": output.get("video_urls") or payload.get("video_urls") or [],
        "lastFrameUrl": output.get("last_frame_url"),
        "error": payload.get("error"),
        "raw": payload,
    }


class SeedanceHandler(BaseHTTPRequestHandler):
    server_version = "SeedanceWeb/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        if path == "/":
            text_response(self, 200, HTML)
            return
        if path == "/health":
            json_response(self, 200, {"status": "ok"})
            return
        if path == "/app.css":
            text_response(self, 200, CSS, "text/css; charset=utf-8")
            return
        if path == "/app.js":
            text_response(self, 200, JS, "application/javascript; charset=utf-8")
            return
        if path == "/assets/callback":
            text_response(self, 200, ASSET_CALLBACK_HTML)
            return
        if path.startswith("/uploads/"):
            file_name = urllib.parse.unquote(path.removeprefix("/uploads/"))
            if "/" in file_name or "\\" in file_name or not file_name:
                json_response(self, 400, {"error": "Invalid file name"})
                return
            file_response(self, os.path.join(UPLOAD_DIR, file_name))
            return
        if path == "/api/config":
            providers = {}
            for provider_id, provider in PROVIDERS.items():
                providers[provider_id] = {
                    "name": provider["name"],
                    "models": provider["models"],
                    "durations": provider["durations"],
                    "ratios": provider["ratios"],
                    "resolutions": provider["resolutions"],
                    "hasServerKey": bool(get_secret(provider["token_names"])),
                    "endpointId": default_model_for_provider(provider),
                    "baseUrl": provider["base_url"],
                }
            json_response(
                self,
                200,
                {
                    "providers": providers,
                    "image": {
                        "models": BYTEPLUS_IMAGE_MODELS,
                        "model": default_byteplus_image_model(),
                        "sizes": ["1K", "2K", "4K", "1024x1024", "1536x1024", "1024x1536", "2048x2048"],
                        "outputFormats": ["png", "jpeg"],
                    },
                    "assets": {
                        "enabled": bool(
                            get_secret(BYTEPLUS_ACCESS_KEY_NAMES)
                            and get_secret(BYTEPLUS_SECRET_KEY_NAMES)
                        ),
                        "projectName": BYTEPLUS_ASSET_PROJECT,
                        "region": BYTEPLUS_ASSET_REGION,
                    },
                },
            )
            return
        if path == "/api/status":
            self.handle_status(params)
            return
        if path == "/api/materials":
            json_response(self, 200, {"materials": list_material_records(public_base_url(self))})
            return
        if path == "/api/materials/status":
            self.handle_material_status(params)
            return
        if path == "/api/assets":
            self.handle_list_assets(params)
            return
        if path == "/api/assets/status":
            self.handle_asset_status(params)
            return
        json_response(self, 404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/api/generate":
            self.handle_generate()
            return
        if self.path == "/api/generate-image":
            self.handle_generate_image()
            return
        if self.path == "/api/upload-reference":
            self.handle_upload_reference()
            return
        if self.path == "/api/materials/submit":
            self.handle_submit_material()
            return
        if self.path == "/api/assets/verification-session":
            self.handle_asset_verification_session()
            return
        if self.path == "/api/assets/verification-result":
            self.handle_asset_verification_result()
            return
        if self.path == "/api/assets/check-generated":
            self.handle_check_generated_asset()
            return
        if self.path == "/api/assets/create":
            self.handle_create_asset()
            return
        json_response(self, 404, {"error": "Not found"})

    def handle_upload_reference(self):
        try:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("multipart/form-data"):
                raise ValueError("multipart/form-data is required")
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                },
            )
            fields = form["file"] if "file" in form else []
            if not isinstance(fields, list):
                fields = [fields]
            uploads = []
            base = public_base_url(self)
            for field in fields:
                saved = save_upload(field)
                material = upsert_material_record(saved, base)
                uploads.append(
                    {
                        "url": material["url"],
                        "fileName": saved["fileName"],
                        "originalName": saved["originalName"],
                        "contentType": saved["contentType"],
                        "size": saved["size"],
                        "material": material,
                    }
                )
            if not uploads:
                raise ValueError("file is required")
            json_response(self, 200, {"files": uploads})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except OSError as exc:
            json_response(self, 500, {"error": str(exc)})

    def handle_submit_material(self):
        try:
            data = read_json_body(self)
            material_id = str(data.get("materialId") or data.get("id") or "").strip()
            group_id = str(data.get("groupId") or "").strip()
            group_type = str(data.get("groupType") or "AIGC").strip()
            project_name = str(data.get("projectName") or BYTEPLUS_ASSET_PROJECT).strip()
            if not material_id:
                raise ValueError("materialId is required")
            if not group_id:
                if group_type != "AIGC":
                    raise ValueError("groupId is required for non-AIGC assets")
                group_id = ensure_aigc_asset_group(
                    project_name,
                    str(data.get("groupName") or BYTEPLUS_AIGC_GROUP_NAME).strip(),
                )
            record = get_material_record(material_id)
            if not record:
                raise ValueError("material not found")
            existing_status = str(record.get("assetStatus") or "").strip()
            existing_asset_id = str(record.get("assetId") or "").strip()
            if existing_asset_id and existing_status in {"Processing", "Active"}:
                json_response(self, 200, public_material_record(record, public_base_url(self)))
                return
            content_type = str(record.get("contentType") or "").lower()
            asset_type = (
                "Video"
                if content_type.startswith("video/")
                else "Audio"
                if content_type.startswith("audio/")
                else "Image"
            )
            file_name = str(record.get("fileName") or "").strip()
            asset_url = f"{public_base_url(self)}/uploads/{urllib.parse.quote(file_name)}"
            payload = {
                "GroupId": group_id,
                "URL": asset_url,
                "AssetType": asset_type,
                "ProjectName": project_name,
            }
            name = str(data.get("name") or record.get("originalName") or "").strip()
            if name:
                payload["Name"] = name
            result = call_byteplus_asset_api("CreateAsset", payload, timeout=120)
            asset_id = str(result.get("Id") or result.get("id") or "").strip()
            if not asset_id:
                raise RuntimeError("BytePlus did not return an Asset ID")
            material = update_material_record(
                material_id,
                {
                    "assetId": asset_id,
                    "assetUri": f"asset://{asset_id}",
                    "assetStatus": "Processing",
                    "assetGroupId": group_id,
                    "assetProject": project_name,
                    "assetError": "",
                    "submittedAt": utc_timestamp(),
                },
                public_base_url(self),
            )
            json_response(self, 200, material)
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 502, {"error": str(exc)})

    def handle_material_status(self, params):
        try:
            material_id = (params.get("materialId") or params.get("id") or [""])[0].strip()
            if not material_id:
                raise ValueError("materialId is required")
            record = get_material_record(material_id)
            if not record:
                raise ValueError("material not found")
            asset_id = str(record.get("assetId") or "").strip()
            if not asset_id:
                json_response(self, 200, public_material_record(record, public_base_url(self)))
                return
            project_name = str(
                (params.get("projectName") or [record.get("assetProject") or BYTEPLUS_ASSET_PROJECT])[0]
            ).strip()
            result = call_byteplus_asset_api(
                "GetAsset",
                {"Id": asset_id, "ProjectName": project_name},
            )
            status = str(result.get("Status") or "Processing").strip()
            error = provider_error_message(result) if status == "Failed" else ""
            material = update_material_record(
                material_id,
                {
                    "assetStatus": status,
                    "assetUri": f"asset://{asset_id}" if status == "Active" else str(record.get("assetUri") or ""),
                    "assetProject": project_name,
                    "assetError": error,
                },
                public_base_url(self),
            )
            json_response(self, 200, material)
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 502, {"error": str(exc)})

    def handle_asset_verification_session(self):
        try:
            data = read_json_body(self)
            callback_url = str(data.get("callbackUrl") or "").strip()
            if not callback_url.startswith(("http://", "https://")):
                raise ValueError("callbackUrl must be an absolute HTTP(S) URL")
            result = call_byteplus_asset_api(
                "CreateVisualValidateSession",
                {
                    "CallbackURL": callback_url,
                    "ProjectName": str(
                        data.get("projectName") or BYTEPLUS_ASSET_PROJECT
                    ).strip(),
                },
            )
            json_response(self, 200, result)
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 502, {"error": str(exc)})

    def handle_asset_verification_result(self):
        try:
            data = read_json_body(self)
            byted_token = str(data.get("bytedToken") or data.get("BytedToken") or "").strip()
            if not byted_token:
                raise ValueError("bytedToken is required")
            result = call_byteplus_asset_api(
                "GetVisualValidateResult",
                {
                    "BytedToken": byted_token,
                    "ProjectName": str(
                        data.get("projectName") or BYTEPLUS_ASSET_PROJECT
                    ).strip(),
                },
            )
            json_response(self, 200, result)
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 502, {"error": str(exc)})

    def handle_create_asset(self):
        try:
            data = read_json_body(self)
            group_id = str(data.get("groupId") or "").strip()
            asset_url = str(data.get("url") or "").strip()
            asset_type = str(data.get("assetType") or "Image").strip().title()
            if not group_id:
                raise ValueError("groupId is required")
            if not asset_url.startswith(("http://", "https://")):
                raise ValueError("url must be an absolute public HTTP(S) URL")
            if asset_type not in {"Image", "Video", "Audio"}:
                raise ValueError("assetType must be Image, Video, or Audio")
            payload = {
                "GroupId": group_id,
                "URL": asset_url,
                "AssetType": asset_type,
                "ProjectName": str(
                    data.get("projectName") or BYTEPLUS_ASSET_PROJECT
                ).strip(),
            }
            name = str(data.get("name") or "").strip()
            if name:
                payload["Name"] = name
            result = call_byteplus_asset_api("CreateAsset", payload, timeout=120)
            json_response(self, 200, result)
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 502, {"error": str(exc)})

    def handle_check_generated_asset(self):
        try:
            data = read_json_body(self)
            generated_url = str(data.get("url") or "").strip()
            project_name = str(
                data.get("projectName") or BYTEPLUS_ASSET_PROJECT
            ).strip()
            if generated_url.startswith("data:image/"):
                file_name = save_generated_data_url(generated_url)
                asset_url = (
                    f"{public_base_url(self)}/uploads/{urllib.parse.quote(file_name)}"
                )
            elif generated_url.startswith(("http://", "https://")):
                asset_url = generated_url
            else:
                raise ValueError(
                    "url must be a generated image HTTP(S) URL or image data URL"
                )
            group_id = ensure_aigc_asset_group(
                project_name,
                str(data.get("groupName") or BYTEPLUS_AIGC_GROUP_NAME).strip(),
            )
            payload = {
                "GroupId": group_id,
                "URL": asset_url,
                "AssetType": "Image",
                "ProjectName": project_name,
            }
            name = str(data.get("name") or "generated-hero").strip()
            if name:
                payload["Name"] = name
            result = call_byteplus_asset_api("CreateAsset", payload, timeout=120)
            asset_id = str(result.get("Id") or result.get("id") or "").strip()
            if not asset_id:
                raise RuntimeError("BytePlus did not return an Asset ID")
            json_response(
                self,
                200,
                {
                    "assetId": asset_id,
                    "assetUri": f"asset://{asset_id}",
                    "status": "Processing",
                    "groupId": group_id,
                    "groupType": "AIGC",
                    "projectName": project_name,
                },
            )
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 502, {"error": str(exc)})

    def handle_list_assets(self, params):
        try:
            project_name = (
                params.get("projectName") or [BYTEPLUS_ASSET_PROJECT]
            )[0].strip()
            group_id = (params.get("groupId") or [""])[0].strip()
            groups_payload = {
                "Filter": {"GroupType": "LivenessFace"},
                "PageNumber": 1,
                "PageSize": 100,
                "ProjectName": project_name,
            }
            groups = call_byteplus_asset_api("ListAssetGroups", groups_payload)
            assets_filter = {"GroupType": "LivenessFace"}
            if group_id:
                assets_filter["GroupIds"] = [group_id]
            assets = call_byteplus_asset_api(
                "ListAssets",
                {
                    "Filter": assets_filter,
                    "PageNumber": 1,
                    "PageSize": 100,
                    "SortBy": "CreateTime",
                    "SortOrder": "Desc",
                    "ProjectName": project_name,
                },
            )
            json_response(
                self,
                200,
                {
                    "groups": groups.get("Items") or [],
                    "assets": assets.get("Items") or [],
                    "groupCount": groups.get("TotalCount") or 0,
                    "assetCount": assets.get("TotalCount") or 0,
                },
            )
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 502, {"error": str(exc)})

    def handle_asset_status(self, params):
        try:
            asset_id = (params.get("assetId") or params.get("id") or [""])[0].strip()
            project_name = (
                params.get("projectName") or [BYTEPLUS_ASSET_PROJECT]
            )[0].strip()
            if not asset_id:
                raise ValueError("assetId is required")
            result = call_byteplus_asset_api(
                "GetAsset",
                {"Id": asset_id, "ProjectName": project_name},
            )
            json_response(self, 200, result)
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 502, {"error": str(exc)})

    def handle_generate(self):
        try:
            data = read_json_body(self)
            provider_id = data.get("provider") or "byteplus"
            provider = provider_config(provider_id)
            api_key = str(data.get("apiKey") or get_secret(provider["token_names"])).strip()
            if not api_key:
                names = ", ".join(provider["token_names"])
                raise PermissionError(f"API key не найден. Добавьте {names} в Railway env или в {TOKEN_FILE}.")
            payload = build_submit_payload(provider_id, data)
            base_url = str(data.get("baseUrl") or provider["base_url"]).strip()
            url = endpoint_url(provider, provider["submit_path"], base_url=base_url)
            status_code, response_payload = request_json("POST", url, api_key, payload)
            normalized = normalize_submit(provider_id, status_code, response_payload)
            normalized["request"] = redact_large_values(payload)
            if not normalized["ok"]:
                normalized["error"] = provider_error_message(response_payload) or f"Provider returned HTTP {status_code}"
                sys.stderr.write(
                    "Generate failed: "
                    + json.dumps(
                        {
                            "provider": provider_id,
                            "status_code": status_code,
                            "response": response_payload,
                            "request": redact_large_values(payload),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            json_response(self, 200 if normalized["ok"] else status_code, normalized)
        except PermissionError as exc:
            sys.stderr.write(f"Generate permission error: {exc}\n")
            json_response(self, 401, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"Generate bad request: {exc}\n")
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            sys.stderr.write(f"Generate runtime error: {exc}\n")
            json_response(self, 502, {"error": str(exc)})

    def handle_generate_image(self):
        try:
            data = read_json_body(self)
            provider_id = data.get("provider") or "byteplus"
            provider = provider_config(provider_id)
            api_key = str(data.get("apiKey") or get_secret(provider["token_names"])).strip()
            if not api_key:
                names = ", ".join(provider["token_names"])
                raise PermissionError(f"API key не найден. Добавьте {names} в Railway env или в {TOKEN_FILE}.")
            payload = build_image_payload(provider_id, data)
            base_url = str(data.get("baseUrl") or provider["base_url"]).strip()
            url = endpoint_url(provider, BYTEPLUS_IMAGE_SUBMIT_PATH, base_url=base_url)
            status_code, response_payload = request_json("POST", url, api_key, payload, timeout=120)
            normalized = normalize_image_generation(status_code, response_payload)
            normalized["request"] = redact_large_values(payload)
            if not normalized["ok"]:
                normalized["error"] = provider_error_message(response_payload) or normalized.get("error") or f"Provider returned HTTP {status_code}"
                sys.stderr.write(
                    "Image generate failed: "
                    + json.dumps(
                        {
                            "provider": provider_id,
                            "status_code": status_code,
                            "response": response_payload,
                            "request": redact_large_values(payload),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            json_response(self, 200 if normalized["ok"] else status_code, normalized)
        except PermissionError as exc:
            sys.stderr.write(f"Image generate permission error: {exc}\n")
            json_response(self, 401, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"Image generate bad request: {exc}\n")
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            sys.stderr.write(f"Image generate runtime error: {exc}\n")
            json_response(self, 502, {"error": str(exc)})

    def handle_status(self, params):
        try:
            provider_id = (params.get("provider") or ["byteplus"])[0]
            task_id = (params.get("taskId") or params.get("id") or [""])[0].strip()
            if not task_id:
                raise ValueError("taskId is required")
            provider = provider_config(provider_id)
            api_key = (params.get("apiKey") or [get_secret(provider["token_names"])])[0].strip()
            if not api_key:
                names = ", ".join(provider["token_names"])
                raise PermissionError(f"API key не найден. Добавьте {names} в Railway env или в {TOKEN_FILE}.")
            base_url = (params.get("baseUrl") or [provider["base_url"]])[0].strip()
            if "{task_id}" in provider["status_path"]:
                url = endpoint_url(provider, provider["status_path"], task_id=task_id, base_url=base_url)
            else:
                url = endpoint_url(provider, provider["status_path"], base_url=base_url)
                url += "?" + urllib.parse.urlencode({"task_id": task_id})
            status_code, payload = request_json("GET", url, api_key)
            json_response(self, status_code if status_code >= 400 else 200, normalize_status(provider_id, payload))
        except PermissionError as exc:
            json_response(self, 401, {"error": str(exc)})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except RuntimeError as exc:
            json_response(self, 502, {"error": str(exc)})

ASSET_CALLBACK_HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BytePlus verification</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #07070f; color: #e8e8f0; font: 16px system-ui, sans-serif; }
    main { max-width: 520px; margin: 24px; padding: 28px; border: 1px solid rgba(255,255,255,.1); border-radius: 14px; background: #0f0f1a; }
    p { color: #a1a1b5; line-height: 1.55; }
  </style>
</head>
<body>
  <main>
    <h1 id="title">Проверка завершена</h1>
    <p id="message">Результат передан в Seedance Studio. Это окно можно закрыть.</p>
  </main>
  <script>
    const params = Object.fromEntries(new URLSearchParams(location.search).entries());
    const ok = params.resultCode === "10000";
    document.querySelector("#title").textContent = ok ? "Личность подтверждена" : "Проверка не пройдена";
    document.querySelector("#message").textContent = ok
      ? "Получаем приватную группу ассетов. Вернитесь в Seedance Studio."
      : `BytePlus вернул код ${params.resultCode || "unknown"}. Запустите новую проверку.`;
    if (window.opener) {
      window.opener.postMessage({ type: "byteplus-asset-verification", params }, location.origin);
      if (ok) setTimeout(() => window.close(), 1400);
    }
  </script>
</body>
</html>
"""


HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Seedance 2 Video Studio</title>
  <link rel="stylesheet" href="/app.css">
</head>
<body>
  <main class="shell">
    <section class="workspace">
      <div class="topbar">
        <div class="brand">
          <div class="brand-mark">S2</div>
          <div>
            <h1>Seedance / Seedream Studio</h1>
            <p>BytePlus Ark — video tasks and image generations</p>
          </div>
        </div>
        <div class="status-pill" id="keyStatus">Проверка ключа...</div>
      </div>

      <form id="generationForm" class="panel">
        <div class="mode-switch" role="group" aria-label="Generation mode">
          <button type="button" class="mode-btn active" data-mode="video">Video</button>
          <button type="button" class="mode-btn" data-mode="image">Image</button>
        </div>

        <section class="form-section scene-section">
          <h2>Scene</h2>
          <label>Prompt
            <div class="prompt-editor">
              <div id="promptEditor" class="prompt-input" contenteditable="true" role="textbox" aria-multiline="true" data-placeholder="Describe your video scene..."></div>
              <textarea name="prompt" hidden>A cinematic aerial shot over coastline at golden hour, slow push-in, soft natural light</textarea>
            </div>
          </label>

          <div class="upload-grid">
            <label class="upload-tile upload-tile-wide"><span class="upload-plus">+</span><span>Files</span>
              <input id="referenceUpload" type="file" accept="image/*,video/*,audio/*" multiple>
            </label>
          </div>
          <div class="image-reference-list" id="imageReferenceList" aria-label="Ordered image references"></div>
          <div class="reference-preview" id="referencePreview"></div>
          <div class="upload-status" id="uploadStatus">Файлы будут загружены перед отправкой задачи.</div>
          <input name="imageUrls" type="hidden">
          <input name="videoUrls" type="hidden">
          <input name="audioUrls" type="hidden">
        </section>

        <section class="form-section asset-library video-setting">
          <div class="section-heading">
            <div>
              <h2>Materials & private assets</h2>
              <p>Check virtual heroes directly; use H5 only for real people</p>
            </div>
            <span class="status-pill idle" id="assetApiStatus">checking</span>
          </div>
          <div class="grid two">
            <label>BytePlus project
              <input id="assetProject" value="default" autocomplete="off">
            </label>
            <label>Real-person group (optional)
              <select id="assetGroupSelect">
                <option value="">No groups loaded</option>
              </select>
            </label>
          </div>
          <div class="asset-actions">
            <button type="button" class="secondary" id="startVerificationBtn">Verify a person</button>
            <button type="button" class="secondary" id="refreshAssetsBtn">Refresh library</button>
          </div>
          <p class="asset-help" id="assetHelp">Real-person verification creates one private group per person.</p>
          <div class="asset-upload-row material-upload-row">
            <label>Upload photo or video
              <input id="assetUpload" type="file" accept="image/*,video/*,audio/*">
            </label>
            <button type="button" id="createAssetBtn">Save material</button>
          </div>
          <div class="asset-id-row">
            <label>Reuse by Asset ID
              <input id="manualAssetInput" placeholder="asset-... or asset://asset-..." autocomplete="off" spellcheck="false">
            </label>
            <label>Type
              <select id="manualAssetType">
                <option value="Image">Image</option>
                <option value="Video">Video</option>
                <option value="Audio">Audio</option>
              </select>
            </label>
            <button type="button" class="secondary" id="addAssetByIdBtn">Use ID</button>
          </div>
          <h3 class="asset-subheading">Uploaded materials</h3>
          <div class="material-list" id="materialList">
            <div class="empty asset-empty">No uploaded materials yet.</div>
          </div>
          <h3 class="asset-subheading">BytePlus private library</h3>
          <div class="private-asset-list" id="privateAssetList">
            <div class="empty asset-empty">No private assets loaded.</div>
          </div>
        </section>

        <section class="form-section settings-section">
          <h2>Settings</h2>
          <div class="duration-control video-setting">
            <div class="duration-head">
              <label for="duration">Duration</label>
              <span id="durationValue">5s</span>
            </div>
            <input name="duration" id="duration" type="range" min="4" max="15" value="5">
            <div class="duration-scale"><span>4s</span><span>15s</span></div>
          </div>
          <div class="grid two">
            <label>Aspect Ratio
              <select name="aspectRatio" id="aspectRatio"></select>
            </label>
            <label>Resolution
              <select name="resolution" id="resolution"></select>
            </label>
          </div>
          <div class="grid two image-settings" hidden>
            <label>Image Size
              <select name="imageSize" id="imageSize"></select>
            </label>
            <label>Output Format
              <select name="imageOutputFormat" id="imageOutputFormat"></select>
            </label>
          </div>
        </section>

        <div class="checks video-options">
          <label><input name="generateAudio" type="checkbox"> Generate audio</label>
          <label><input name="returnLastFrame" type="checkbox"> Return last frame</label>
          <label><input name="webSearch" type="checkbox"> Web search tool</label>
        </div>
        <div class="checks image-options" hidden>
          <label><input name="imageWatermark" type="checkbox"> Watermark</label>
        </div>

        <div class="actions">
          <button type="submit" id="submitBtn">Generate video</button>
          <button type="button" class="secondary" id="pollBtn" disabled>Poll status</button>
        </div>
      </form>
    </section>

    <aside class="result">
      <div class="result-head">
        <div>
          <h2>Result</h2>
          <p id="taskLine">Задача еще не отправлена.</p>
        </div>
        <div class="status-pill idle" id="taskStatus">idle</div>
      </div>
      <div id="videoWrap" class="video-wrap">
        <div class="empty">MP4 или изображение появится здесь после генерации.</div>
      </div>
      <pre id="rawOutput">{}</pre>
    </aside>
  </main>
  <script src="/app.js"></script>
</body>
</html>
"""


CSS = """
:root {
  color-scheme: dark;
  --bg: #07070f;
  --ink: #e8e8f0;
  --muted: #7f8197;
  --line: rgba(255, 255, 255, .08);
  --panel: #0f0f1a;
  --field: #131320;
  --field-2: #171727;
  --accent: #7c3aed;
  --accent-2: #0ea5e9;
  --good: #34d399;
  --warn: #fbbf24;
  --bad: #f87171;
}

* { box-sizing: border-box; }
[hidden] { display: none !important; }
body {
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--ink);
}

.shell {
  min-height: 100vh;
  display: grid;
  grid-template-columns: minmax(380px, 520px) minmax(0, 1fr);
  gap: 0;
}

.workspace, .result {
  min-width: 0;
}

.workspace {
  border-right: 1px solid var(--line);
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 22px;
  border-bottom: 1px solid var(--line);
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

.brand-mark {
  width: 34px;
  height: 34px;
  border-radius: 8px;
  border: 1px solid rgba(124, 58, 237, .34);
  background: rgba(124, 58, 237, .16);
  color: #c4b5fd;
  display: grid;
  place-items: center;
  font-size: 12px;
  font-weight: 850;
}

h1, h2, p { margin: 0; }
h1 { font-size: 16px; line-height: 1.15; font-weight: 760; }
h2 { font-size: 12px; text-transform: uppercase; color: var(--muted); font-weight: 800; }
p { color: var(--muted); margin-top: 5px; line-height: 1.35; font-size: 12px; }

.panel, .result {
  background: transparent;
  border: 0;
  border-radius: 0;
  box-shadow: none;
}

.panel {
  padding: 22px;
}

.grid {
  display: grid;
  gap: 12px;
}
.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.four { grid-template-columns: repeat(4, minmax(0, 1fr)); }

label {
  display: grid;
  gap: 7px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 780;
  text-transform: uppercase;
}

input, select, textarea {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--field);
  color: var(--ink);
  font: inherit;
  font-size: 14px;
  padding: 10px 12px;
  outline: none;
}

input[type="file"] {
  min-height: 40px;
  padding: 8px;
  font-size: 11px;
  color: var(--muted);
}

textarea { resize: vertical; line-height: 1.45; }
input:focus, select:focus, textarea:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(124, 58, 237, .18);
}

.prompt-input {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--field);
  color: var(--ink);
  font: inherit;
  font-size: 14px;
  line-height: 1.45;
  padding: 10px 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  outline: none;
  min-height: 170px;
  max-height: 420px;
  overflow: auto;
  text-transform: none;
  font-weight: 500;
}

.prompt-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(124, 58, 237, .18);
}

.prompt-input:empty::before {
  content: attr(data-placeholder);
  color: rgba(127, 129, 151, .75);
  pointer-events: none;
}

.prompt-token {
  color: #bef264;
  font-weight: 900;
}

form { display: grid; gap: 16px; }
.mode-switch {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--field);
  padding: 4px;
}
.mode-btn {
  min-height: 34px;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  border: 0;
}
.mode-btn.active {
  background: var(--accent);
  color: #fff;
}
.form-section {
  display: grid;
  gap: 14px;
}
.form-section + .form-section {
  border-top: 1px solid var(--line);
  padding-top: 16px;
}
.form-section h2 {
  font-size: 12px;
  line-height: 1.2;
  margin: 0;
  color: var(--muted);
}
.checks {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  padding-top: 2px;
}
.checks label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  text-transform: none;
  font-weight: 650;
}
.checks input {
  width: 16px;
  height: 16px;
  accent-color: var(--accent);
}

.upload-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 10px;
}

.upload-tile {
  position: relative;
  border: 1px dashed var(--line);
  border-radius: 8px;
  background: var(--field);
  padding: 10px;
  min-height: 96px;
  align-content: center;
  text-align: center;
  cursor: pointer;
  transition: border-color .16s, background .16s, transform .16s;
  display: grid;
  place-items: center;
  gap: 7px;
}

.upload-tile-wide {
  min-height: 63px;
}

.duration-control {
  display: grid;
  gap: 8px;
}

.duration-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.duration-head label {
  display: block;
}

#durationValue {
  color: var(--ink);
  font-size: 14px;
  font-weight: 800;
}

input[type="range"] {
  appearance: none;
  height: 6px;
  padding: 0;
  border: 0;
  border-radius: 999px;
  background: linear-gradient(to right, var(--accent) 0%, var(--accent) 9.09%, var(--field) 9.09%, var(--field) 100%);
  cursor: pointer;
}

input[type="range"]::-webkit-slider-thumb {
  appearance: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--accent);
  border: 2px solid rgba(255, 255, 255, .14);
  box-shadow: 0 0 0 3px rgba(124, 58, 237, .22);
}

input[type="range"]::-moz-range-thumb {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--accent);
  border: 2px solid rgba(255, 255, 255, .14);
  box-shadow: 0 0 0 3px rgba(124, 58, 237, .22);
}

.duration-scale {
  display: flex;
  justify-content: space-between;
  color: rgba(127, 129, 151, .65);
  font-size: 10px;
}

.upload-tile:hover {
  border-color: rgba(124, 58, 237, .55);
  background: rgba(124, 58, 237, .07);
  transform: translateY(-1px);
}

.upload-tile.is-dragging-over {
  border-color: var(--accent);
  background: rgba(124, 58, 237, .12);
  box-shadow: 0 0 0 3px rgba(124, 58, 237, .16);
  transform: translateY(-1px);
}

.upload-tile.is-dragging-over .upload-plus {
  border-color: rgba(196, 181, 253, .72);
  background: rgba(124, 58, 237, .18);
}

.upload-tile input {
  position: absolute;
  inline-size: 1px;
  block-size: 1px;
  opacity: 0;
  pointer-events: none;
}

.upload-plus {
  width: 24px;
  height: 24px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: rgba(255, 255, 255, .04);
  color: #c4b5fd;
  display: grid;
  place-items: center;
  font-size: 18px;
  line-height: 1;
}

.image-reference-list {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.image-reference-list:empty {
  display: none;
}

.image-card {
  position: relative;
  aspect-ratio: 4 / 5;
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  background: var(--field);
  cursor: grab;
}

.image-card:active {
  cursor: grabbing;
}

.image-card.dragging {
  opacity: .45;
}

.image-card img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.image-card-badge {
  position: absolute;
  z-index: 2;
}

.image-card-badge {
  top: 6px;
  left: 6px;
  min-width: 26px;
  height: 24px;
  padding: 0 7px;
  border-radius: 999px;
  background: rgba(13, 13, 19, .76);
  border: 1px solid rgba(255, 255, 255, .14);
  color: var(--ink);
  display: grid;
  place-items: center;
  font-size: 11px;
  font-weight: 800;
}

.reference-remove {
  position: absolute;
  z-index: 3;
  top: 6px;
  right: 6px;
  width: 26px;
  height: 24px;
  padding: 0;
  border-radius: 999px;
  border: 1px solid rgba(255, 255, 255, .14);
  background: rgba(13, 13, 19, .78);
  color: var(--ink);
  font-size: 14px;
  line-height: 1;
  display: grid;
  place-items: center;
}

.reference-remove:hover {
  border-color: rgba(248, 113, 113, .55);
  background: rgba(248, 113, 113, .18);
}

.image-card-check {
  position: absolute;
  z-index: 3;
  left: 6px;
  right: 6px;
  bottom: 6px;
  width: calc(100% - 12px);
  min-height: 30px;
  padding: 6px 8px;
  border: 1px solid rgba(255, 255, 255, .18);
  border-radius: 7px;
  background: rgba(13, 13, 19, .88);
  color: var(--ink);
  font-size: 10px;
  line-height: 1.2;
  backdrop-filter: blur(8px);
}

.image-card-check:disabled {
  opacity: .7;
}

.reference-preview {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
}

.reference-thumb {
  position: relative;
  aspect-ratio: 1;
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  background: var(--field);
  display: grid;
  place-items: center;
  color: var(--muted);
  font-size: 10px;
  padding: 8px;
  text-align: center;
}

.reference-thumb img,
.reference-thumb video {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.reference-thumb span {
  max-width: 100%;
  overflow-wrap: anywhere;
}

.upload-status {
  min-height: 32px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--field);
  color: var(--muted);
  display: flex;
  align-items: center;
  padding: 7px 10px;
  font-size: 13px;
  line-height: 1.35;
}

.upload-status.busy {
  color: #c4b5fd;
  border-color: rgba(124, 58, 237, .3);
  background: rgba(124, 58, 237, .1);
}

.upload-status.error {
  color: var(--bad);
  border-color: rgba(180, 35, 24, .25);
  background: rgba(180, 35, 24, .08);
}

.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.asset-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.asset-actions button {
  min-height: 36px;
  padding: 0 12px;
  font-size: 12px;
}

.asset-help {
  min-height: 18px;
  color: var(--muted);
}

.asset-help.error { color: var(--bad); }
.asset-help.done { color: var(--good); }

.asset-upload-row {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
  gap: 10px;
  align-items: end;
}

.asset-upload-row button {
  grid-column: 1 / -1;
}

.material-upload-row {
  grid-template-columns: minmax(0, 1fr) auto;
}

.material-upload-row button {
  grid-column: auto;
}

.asset-id-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 110px auto;
  gap: 10px;
  align-items: end;
}

.asset-subheading {
  margin: 2px 0 0;
  color: var(--muted);
  font-size: 10px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: .06em;
}

.private-asset-list,
.material-list {
  display: grid;
  gap: 8px;
  max-height: 320px;
  overflow: auto;
}

.private-asset-card {
  display: grid;
  grid-template-columns: 54px minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  min-height: 70px;
  padding: 8px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--field);
}

.private-asset-preview {
  width: 54px;
  height: 54px;
  border-radius: 6px;
  overflow: hidden;
  display: grid;
  place-items: center;
  background: var(--field-2);
  color: var(--muted);
  font-size: 10px;
  text-transform: uppercase;
}

.private-asset-preview img,
.private-asset-preview video {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.private-asset-meta {
  min-width: 0;
}

.private-asset-meta strong,
.private-asset-meta small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.private-asset-meta strong { font-size: 12px; }
.private-asset-meta small {
  margin-top: 5px;
  color: var(--muted);
  font-size: 10px;
}

.private-asset-card button {
  min-height: 34px;
  padding: 0 11px;
  font-size: 11px;
}

.asset-card-actions {
  display: grid;
  gap: 6px;
  min-width: 96px;
}

.asset-card-actions button {
  min-height: 30px;
}

.private-asset-card button.added {
  color: var(--good);
  border-color: rgba(52, 211, 153, .3);
  background: rgba(52, 211, 153, .08);
}

.asset-empty {
  min-height: 70px;
}

.actions {
  display: flex;
  gap: 12px;
  border-top: 1px solid var(--line);
  padding-top: 16px;
}
button {
  min-height: 42px;
  border: 1px solid transparent;
  border-radius: 8px;
  padding: 0 18px;
  background: var(--accent);
  color: white;
  font: inherit;
  font-size: 14px;
  font-weight: 760;
  cursor: pointer;
  transition: transform .16s, filter .16s, border-color .16s, background .16s;
}
button:hover { filter: brightness(1.05); transform: translateY(-1px); }
button:disabled { opacity: .5; cursor: not-allowed; }
.secondary {
  background: var(--field-2);
  color: #d1d5db;
  border-color: var(--line);
}

.status-pill {
  flex: 0 0 auto;
  border: 1px solid rgba(31, 122, 140, .28);
  border-radius: 999px;
  padding: 7px 10px;
  color: #fcd34d;
  background: rgba(251, 191, 36, .1);
  border-color: rgba(251, 191, 36, .28);
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
}
.status-pill.idle {
  color: var(--muted);
  border-color: var(--line);
  background: var(--field);
}
.status-pill.done {
  color: var(--good);
  border-color: rgba(52, 211, 153, .25);
  background: rgba(52, 211, 153, .08);
}
.status-pill.error {
  color: var(--bad);
  border-color: rgba(248, 113, 113, .25);
  background: rgba(248, 113, 113, .08);
}

.result {
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 22px;
}

.result-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.video-wrap {
  flex: 1;
  min-height: 300px;
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  display: grid;
  place-items: center;
  overflow: hidden;
}
video {
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #000;
}
.image-result-grid {
  width: 100%;
  height: 100%;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
  padding: 12px;
  align-content: start;
  overflow: auto;
}
.image-result-card {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--panel);
}
.image-result-card > a {
  display: block;
  min-width: 0;
}
.image-result-grid img {
  width: 100%;
  height: auto;
  display: block;
  background: #000;
}
.hero-check-panel {
  display: grid;
  gap: 8px;
  padding: 10px;
}
.hero-check-panel button {
  min-height: 36px;
}
.hero-check-actions {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
  gap: 8px;
}
.hero-check-status {
  min-height: 18px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}
.hero-check-status.done { color: var(--good); }
.hero-check-status.error { color: var(--bad); }
.empty {
  color: var(--muted);
  padding: 24px;
  text-align: center;
  line-height: 1.4;
  font-size: 14px;
}
pre {
  margin: 0;
  max-height: 340px;
  overflow: auto;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: var(--panel);
  color: #a7f3d0;
  padding: 13px;
  font-size: 12px;
  line-height: 1.5;
}

@media (max-width: 980px) {
  .shell { grid-template-columns: 1fr; }
  .workspace { border-right: 0; border-bottom: 1px solid var(--line); }
  .four { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .upload-grid, .image-reference-list, .reference-preview { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 620px) {
  .two, .four { grid-template-columns: 1fr; }
  .material-upload-row, .asset-id-row { grid-template-columns: 1fr; }
  .private-asset-card { grid-template-columns: 54px minmax(0, 1fr); }
  .asset-card-actions { grid-column: 1 / -1; grid-template-columns: repeat(auto-fit, minmax(90px, 1fr)); }
  .upload-grid, .image-reference-list, .reference-preview { grid-template-columns: 1fr; }
  .topbar, .result-head { display: grid; }
  .actions { display: grid; }
}
"""


JS = """
const state = {
  config: null,
  taskId: "",
  mode: "video",
  provider: "byteplus",
  providerConfig: null,
  baseUrl: "",
  pollTimer: null,
  privateAssets: [],
  materials: [],
  generatedAssets: {}
};

const $ = (selector) => document.querySelector(selector);
const form = $("#generationForm");
const durationEl = $("#duration");
const durationValue = $("#durationValue");
const ratioEl = $("#aspectRatio");
const resolutionEl = $("#resolution");
const imageSizeEl = $("#imageSize");
const imageOutputFormatEl = $("#imageOutputFormat");
const rawOutput = $("#rawOutput");
const taskLine = $("#taskLine");
const taskStatus = $("#taskStatus");
const videoWrap = $("#videoWrap");
const pollBtn = $("#pollBtn");
const submitBtn = $("#submitBtn");
const keyStatus = $("#keyStatus");
const uploadStatus = $("#uploadStatus");
const promptEl = form.elements.prompt;
const promptEditor = $("#promptEditor");
const referenceUpload = $("#referenceUpload");
const imageReferenceList = $("#imageReferenceList");
const referencePreview = $("#referencePreview");
const assetApiStatus = $("#assetApiStatus");
const assetProject = $("#assetProject");
const assetGroupSelect = $("#assetGroupSelect");
const startVerificationBtn = $("#startVerificationBtn");
const refreshAssetsBtn = $("#refreshAssetsBtn");
const assetHelp = $("#assetHelp");
const assetUpload = $("#assetUpload");
const createAssetBtn = $("#createAssetBtn");
const manualAssetInput = $("#manualAssetInput");
const manualAssetType = $("#manualAssetType");
const addAssetByIdBtn = $("#addAssetByIdBtn");
const materialList = $("#materialList");
const privateAssetList = $("#privateAssetList");
const modeButtons = document.querySelectorAll(".mode-btn");
const videoSettings = document.querySelectorAll(".video-setting");
const imageSettings = document.querySelectorAll(".image-settings");
const videoOptions = document.querySelectorAll(".video-options");
const imageOptions = document.querySelectorAll(".image-options");
let imageRefCounter = 0;
let mediaRefCounter = 0;
let draggedImageRefId = null;
const imageRefs = [];
const mediaRefs = [];

function pretty(data) {
  rawOutput.textContent = JSON.stringify(data, null, 2);
}

function optionList(el, values, current) {
  el.innerHTML = "";
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    if (String(value) === String(current)) option.selected = true;
    el.appendChild(option);
  });
}

function setStatus(value, kind = "idle") {
  taskStatus.textContent = value;
  taskStatus.className = `status-pill ${kind}`;
}

function currentProvider() {
  return state.providerConfig;
}

function setMode(mode) {
  state.mode = mode === "image" ? "image" : "video";
  modeButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === state.mode);
  });
  videoSettings.forEach((el) => {
    el.hidden = state.mode !== "video";
  });
  videoOptions.forEach((el) => {
    el.hidden = state.mode !== "video";
  });
  imageSettings.forEach((el) => {
    el.hidden = state.mode !== "image";
  });
  imageOptions.forEach((el) => {
    el.hidden = state.mode !== "image";
  });
  pollBtn.hidden = state.mode === "image";
  pollBtn.disabled = state.mode === "image" || !state.taskId;
  submitBtn.textContent = state.mode === "image" ? "Generate image" : "Generate video";
}

function refreshProviderFields() {
  const provider = currentProvider();
  optionList(ratioEl, provider.ratios, "16:9");
  if (provider.resolutions.length) {
    optionList(resolutionEl, provider.resolutions, "720p");
    resolutionEl.disabled = false;
  } else {
    optionList(resolutionEl, ["provider default"], "provider default");
    resolutionEl.disabled = true;
  }
  keyStatus.textContent = provider.hasServerKey ? "server key ready" : "key required";
  keyStatus.className = provider.hasServerKey ? "status-pill done" : "status-pill error";
  if (state.config && state.config.image) {
    optionList(imageSizeEl, state.config.image.sizes || ["2K"], "2K");
    optionList(imageOutputFormatEl, state.config.image.outputFormats || ["png", "jpeg"], "png");
  }
  updateDurationSlider();
  setMode(state.mode);
}

function collectPayload() {
  syncPromptField();
  syncPromptImageUrls();
  syncImageUrlsField();
  syncMediaUrlsFields();
  const data = Object.fromEntries(new FormData(form).entries());
  const provider = currentProvider();
  data.provider = state.provider;
  data.endpoint = provider.endpointId || provider.models[0];
  data.model = data.endpoint;
  data.baseUrl = provider.baseUrl;
  data.generateAudio = form.generateAudio.checked;
  data.returnLastFrame = form.returnLastFrame.checked;
  data.webSearch = form.webSearch.checked;
  data.imageModel = state.config && state.config.image ? state.config.image.model : "";
  data.imageWatermark = form.imageWatermark ? form.imageWatermark.checked : false;
  if (resolutionEl.disabled) delete data.resolution;
  return data;
}

function setUploadStatus(message, kind = "") {
  uploadStatus.textContent = message;
  uploadStatus.className = `upload-status ${kind}`;
}

function setAssetStatus(message, kind = "idle") {
  assetApiStatus.textContent = message;
  assetApiStatus.className = `status-pill ${kind}`;
}

function setAssetHelp(message, kind = "") {
  assetHelp.textContent = message;
  assetHelp.className = `asset-help ${kind}`;
}

function privateAssetUri(asset) {
  const raw = asset && (asset.assetUri || asset.AssetUri || asset.Id || asset.assetId);
  if (!raw) return "";
  return String(raw).startsWith("asset://") ? String(raw) : `asset://${raw}`;
}

function privateAssetIsAdded(asset) {
  const uri = privateAssetUri(asset);
  return imageRefs.some((ref) => ref.url === uri) || mediaRefs.some((ref) => ref.url === uri);
}

function addPrivateAssetReference(asset) {
  const uri = privateAssetUri(asset);
  if (!uri || privateAssetIsAdded(asset)) return;
  const type = String(asset.AssetType || "Image").toLowerCase();
  let referenceIndex = 1;
  const common = {
    url: uri,
    previewUrl: asset.URL || "",
    name: asset.Name || asset.Id,
    source: "private-asset"
  };
  if (type === "image") {
    addImageRef(common);
    referenceIndex = imageRefs.length;
  } else {
    mediaRefs.push({
      id: `media-${++mediaRefCounter}`,
      kind: type === "audio" ? "audio" : "video",
      file: null,
      ...common
    });
    syncMediaUrlsFields();
    renderReferencePreview();
    referenceIndex = mediaRefs.filter((ref) => ref.kind === (type === "audio" ? "audio" : "video")).length;
  }
  setUploadStatus(`${asset.AssetType || "Asset"} добавлен как ${uri}. Укажите его в prompt как ${type} ${referenceIndex}.`);
  renderPrivateAssets();
  renderMaterials();
}

function normalizeAssetUri(value) {
  const normalized = String(value || "").trim();
  if (!normalized) throw new Error("Введите BytePlus Asset ID.");
  const assetId = normalized.toLowerCase().startsWith("asset://") ? normalized.slice(8).trim() : normalized;
  if (!assetId || !/^[A-Za-z0-9._:-]+$/.test(assetId)) {
    throw new Error("Некорректный Asset ID.");
  }
  return `asset://${assetId}`;
}

async function copyAssetId(assetId) {
  const normalized = String(assetId || "").replace(/^asset:\/\//i, "").trim();
  if (!normalized) return;
  try {
    await navigator.clipboard.writeText(normalized);
    setAssetHelp(`Asset ID скопирован: ${normalized}`, "done");
  } catch {
    manualAssetInput.value = normalized;
    manualAssetInput.focus();
    manualAssetInput.select();
    setAssetHelp("ID помещён в поле. Скопируйте его вручную.");
  }
}

function addManualAssetReference() {
  try {
    const uri = normalizeAssetUri(manualAssetInput.value);
    const assetId = uri.slice(8);
    const knownAsset = state.privateAssets.find((item) => String(item.Id || "") === assetId);
    addPrivateAssetReference(knownAsset || {
      Id: assetId,
      AssetType: manualAssetType.value || "Image",
      Name: assetId,
      Status: "Active"
    });
    manualAssetInput.value = "";
  } catch (error) {
    setAssetHelp(error.message, "error");
  }
}

function materialType(material) {
  const contentType = String(material.contentType || "").toLowerCase();
  if (contentType.startsWith("video/")) return "Video";
  if (contentType.startsWith("audio/")) return "Audio";
  return "Image";
}

function upsertMaterial(material) {
  if (!material || !material.id) return;
  const index = state.materials.findIndex((item) => item.id === material.id);
  if (index >= 0) state.materials[index] = material;
  else state.materials.unshift(material);
}

function useMaterialFile(material) {
  const type = materialType(material).toLowerCase();
  const common = {
    url: material.url,
    previewUrl: material.url,
    name: material.originalName || material.fileName,
    materialId: material.id,
    source: "material-library"
  };
  if (type === "image") {
    addImageRef(common);
  } else if (!mediaRefs.some((item) => item.url === material.url)) {
    mediaRefs.push({
      id: `media-${++mediaRefCounter}`,
      kind: type === "audio" ? "audio" : "video",
      file: null,
      ...common
    });
    syncMediaUrlsFields();
    renderReferencePreview();
  }
  setUploadStatus(`${material.originalName || "Material"} добавлен из сохранённой библиотеки.`);
}

function useMaterialAsset(material) {
  if (String(material.assetStatus || "").toLowerCase() !== "active" || !material.assetId) return;
  addPrivateAssetReference({
    Id: material.assetId,
    AssetType: materialType(material),
    Name: material.originalName || material.assetId,
    URL: material.url,
    Status: "Active"
  });
}

async function loadMaterials() {
  try {
    const data = await apiFetch("/api/materials");
    state.materials = Array.isArray(data.materials) ? data.materials : [];
    renderMaterials();
  } catch (error) {
    setAssetHelp(error.message, "error");
  }
}

async function refreshMaterialStatus(material, poll = false) {
  const attempts = poll ? 36 : 1;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const params = new URLSearchParams({
      materialId: material.id,
      projectName: assetProject.value.trim() || "default"
    });
    const updated = await apiFetch(`/api/materials/status?${params.toString()}`);
    upsertMaterial(updated);
    renderMaterials();
    const status = String(updated.assetStatus || "Local");
    setAssetStatus(status.toLowerCase(), status === "Active" ? "done" : status === "Failed" ? "error" : "idle");
    setAssetHelp(
      status === "Active"
        ? `Материал прошёл проверку. Asset ID: ${updated.assetId}`
        : status === "Failed"
        ? updated.assetError || "BytePlus отклонил материал."
        : `Материал ${updated.originalName}: ${status}.`,
      status === "Active" ? "done" : status === "Failed" ? "error" : ""
    );
    if (!poll || status === "Active" || status === "Failed") return updated;
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  throw new Error("Материал всё ещё обрабатывается. Нажмите Refresh позже.");
}

async function submitMaterialForReview(material) {
  if (!state.config.assets.enabled) {
    setAssetHelp("Для отправки на проверку добавьте BytePlus AK/SK.", "error");
    return null;
  }
  setAssetStatus("submitting");
  setAssetHelp(`Отправляю ${material.originalName} на проверку BytePlus...`);
  try {
    return await reviewMaterialAsAigc(material);
  } catch (error) {
    setAssetStatus("error", "error");
    setAssetHelp(error.message, "error");
    return null;
  }
}

async function reviewMaterialAsAigc(material) {
    const submitted = await apiFetch("/api/materials/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        materialId: material.id,
        groupType: "AIGC",
        projectName: assetProject.value.trim() || "default",
        name: material.originalName
      })
    });
    upsertMaterial(submitted);
    renderMaterials();
    return await refreshMaterialStatus(submitted, true);
}

async function submitMaterialForRealPersonReview(material) {
  const groupId = assetGroupSelect.value;
  if (!groupId) {
    setAssetHelp("Сначала завершите H5-проверку и выберите real-person group.", "error");
    return null;
  }
  setAssetStatus("submitting");
  setAssetHelp(`Проверяю ${material.originalName} для выбранного реального человека...`);
  try {
    const submitted = await apiFetch("/api/materials/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        materialId: material.id,
        groupId,
        groupType: "LivenessFace",
        projectName: assetProject.value.trim() || "default",
        name: material.originalName
      })
    });
    upsertMaterial(submitted);
    renderMaterials();
    const checked = await refreshMaterialStatus(submitted, true);
    await loadPrivateAssets(true);
    return checked;
  } catch (error) {
    setAssetStatus("error", "error");
    setAssetHelp(error.message, "error");
    return null;
  }
}

function renderMaterials() {
  materialList.replaceChildren();
  if (!state.materials.length) {
    const empty = document.createElement("div");
    empty.className = "empty asset-empty";
    empty.textContent = "Загруженных материалов пока нет.";
    materialList.appendChild(empty);
    return;
  }
  state.materials.forEach((material) => {
    const card = document.createElement("article");
    card.className = "private-asset-card material-card";
    const preview = document.createElement("div");
    preview.className = "private-asset-preview";
    const type = materialType(material);
    if (type === "Image") {
      const img = document.createElement("img");
      img.src = material.url;
      img.alt = material.originalName || "Material";
      preview.appendChild(img);
    } else if (type === "Video") {
      const video = document.createElement("video");
      video.src = material.url;
      video.muted = true;
      video.playsInline = true;
      preview.appendChild(video);
    } else {
      preview.textContent = type;
    }
    const meta = document.createElement("div");
    meta.className = "private-asset-meta";
    const title = document.createElement("strong");
    title.textContent = material.originalName || material.fileName || "Material";
    const detail = document.createElement("small");
    detail.textContent = `${type} · ${material.assetStatus || "Local"}${material.assetId ? ` · ${material.assetId}` : ""}`;
    meta.append(title, detail);
    const actions = document.createElement("div");
    actions.className = "asset-card-actions";
    const useFileButton = document.createElement("button");
    useFileButton.type = "button";
    useFileButton.className = "secondary";
    useFileButton.textContent = "Use file";
    useFileButton.addEventListener("click", () => useMaterialFile(material));
    actions.appendChild(useFileButton);
    const status = String(material.assetStatus || "Local");
    if (status === "Active" && material.assetId) {
      const useAssetButton = document.createElement("button");
      useAssetButton.type = "button";
      useAssetButton.textContent = "Use asset";
      useAssetButton.addEventListener("click", () => useMaterialAsset(material));
      const copyButton = document.createElement("button");
      copyButton.type = "button";
      copyButton.className = "secondary";
      copyButton.textContent = "Copy ID";
      copyButton.addEventListener("click", () => copyAssetId(material.assetId));
      actions.append(useAssetButton, copyButton);
    } else if (status === "Processing") {
      const refreshButton = document.createElement("button");
      refreshButton.type = "button";
      refreshButton.className = "secondary";
      refreshButton.textContent = "Refresh check";
      refreshButton.addEventListener("click", () => refreshMaterialStatus(material).catch((error) => setAssetHelp(error.message, "error")));
      actions.appendChild(refreshButton);
    } else {
      const reviewButton = document.createElement("button");
      reviewButton.type = "button";
      reviewButton.textContent = status === "Failed"
        ? "Проверить снова"
        : materialType(material) === "Image"
        ? "Проверить героя"
        : "Send to check";
      reviewButton.disabled = !state.config.assets.enabled;
      reviewButton.addEventListener("click", () => submitMaterialForReview(material));
      actions.appendChild(reviewButton);
      if (materialType(material) === "Image" && assetGroupSelect.value) {
        const realPersonButton = document.createElement("button");
        realPersonButton.type = "button";
        realPersonButton.className = "secondary";
        realPersonButton.textContent = "Check as real person";
        realPersonButton.addEventListener("click", () => submitMaterialForRealPersonReview(material));
        actions.appendChild(realPersonButton);
      }
    }
    card.append(preview, meta, actions);
    materialList.appendChild(card);
  });
}

function renderPrivateAssets() {
  privateAssetList.replaceChildren();
  const assets = state.privateAssets || [];
  if (!assets.length) {
    const empty = document.createElement("div");
    empty.className = "empty asset-empty";
    empty.textContent = "В выбранной группе пока нет ассетов.";
    privateAssetList.appendChild(empty);
    return;
  }
  assets.forEach((asset) => {
    const card = document.createElement("article");
    card.className = "private-asset-card";
    const preview = document.createElement("div");
    preview.className = "private-asset-preview";
    const type = String(asset.AssetType || "Asset");
    if (asset.URL && type.toLowerCase() === "image") {
      const img = document.createElement("img");
      img.src = asset.URL;
      img.alt = asset.Name || asset.Id;
      preview.appendChild(img);
    } else if (asset.URL && type.toLowerCase() === "video") {
      const video = document.createElement("video");
      video.src = asset.URL;
      video.muted = true;
      preview.appendChild(video);
    } else {
      preview.textContent = type;
    }
    const meta = document.createElement("div");
    meta.className = "private-asset-meta";
    const title = document.createElement("strong");
    title.textContent = asset.Name || asset.Id || "Unnamed asset";
    const detail = document.createElement("small");
    detail.textContent = `${type} · ${asset.Status || "Unknown"} · ${asset.Id || ""}`;
    meta.append(title, detail);
    const actions = document.createElement("div");
    actions.className = "asset-card-actions";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    const active = String(asset.Status || "").toLowerCase() === "active";
    const added = privateAssetIsAdded(asset);
    button.disabled = !active || added;
    button.textContent = added ? "Added" : active ? "Use" : asset.Status || "Processing";
    if (added) button.classList.add("added");
    button.addEventListener("click", () => addPrivateAssetReference(asset));
    actions.appendChild(button);
    if (active && asset.Id) {
      const copyButton = document.createElement("button");
      copyButton.type = "button";
      copyButton.className = "secondary";
      copyButton.textContent = "Copy ID";
      copyButton.addEventListener("click", () => copyAssetId(asset.Id));
      actions.appendChild(copyButton);
    }
    card.append(preview, meta, actions);
    privateAssetList.appendChild(card);
  });
}

function renderAssetGroups(groups) {
  const current = assetGroupSelect.value;
  assetGroupSelect.replaceChildren();
  if (!groups.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No verified groups";
    assetGroupSelect.appendChild(option);
    return;
  }
  groups.forEach((group) => {
    const option = document.createElement("option");
    option.value = group.Id;
    option.textContent = group.Name || group.Title || group.Id;
    assetGroupSelect.appendChild(option);
  });
  if (groups.some((group) => group.Id === current)) {
    assetGroupSelect.value = current;
  }
}

async function loadPrivateAssets(filterSelectedGroup = false) {
  if (!state.config.assets.enabled) return;
  setAssetStatus("loading");
  const params = new URLSearchParams({
    projectName: assetProject.value.trim() || "default"
  });
  if (filterSelectedGroup && assetGroupSelect.value) {
    params.set("groupId", assetGroupSelect.value);
  }
  try {
    const data = await apiFetch(`/api/assets?${params.toString()}`);
    renderAssetGroups(Array.isArray(data.groups) ? data.groups : []);
    state.privateAssets = Array.isArray(data.assets) ? data.assets : [];
    renderPrivateAssets();
    setAssetStatus("ready", "done");
    setAssetHelp(`${data.groupCount || 0} group(s), ${data.assetCount || 0} asset(s). Only Active assets can be used.`);
  } catch (error) {
    setAssetStatus("error", "error");
    setAssetHelp(error.message, "error");
  }
}

async function startAssetVerification() {
  const popup = window.open("", "byteplus-person-verification", "popup,width=520,height=760");
  startVerificationBtn.disabled = true;
  setAssetStatus("starting");
  setAssetHelp("Создаю защищённую H5-сессию BytePlus...");
  try {
    const data = await apiFetch("/api/assets/verification-session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        callbackUrl: `${location.origin}/assets/callback`,
        projectName: assetProject.value.trim() || "default"
      })
    });
    const h5Link = data.H5Link || data.h5Link;
    if (!h5Link) throw new Error("BytePlus did not return H5Link");
    const verificationUrl = new URL(h5Link);
    verificationUrl.searchParams.set("lng", "en");
    if (popup) {
      popup.location.href = verificationUrl.toString();
      popup.focus();
    } else {
      window.open(verificationUrl.toString(), "_blank", "noopener");
    }
    setAssetStatus("verify");
    setAssetHelp("Завершите проверку личности в открывшемся окне.");
  } catch (error) {
    if (popup) popup.close();
    setAssetStatus("error", "error");
    setAssetHelp(error.message, "error");
  } finally {
    startVerificationBtn.disabled = false;
  }
}

async function finishAssetVerification(params) {
  if (params.resultCode !== "10000" || !params.bytedToken) {
    setAssetStatus("failed", "error");
    setAssetHelp(`Проверка не пройдена: resultCode=${params.resultCode || "unknown"}.`, "error");
    return;
  }
  setAssetStatus("saving");
  setAssetHelp("Проверка пройдена. Получаю ID приватной группы...");
  try {
    const data = await apiFetch("/api/assets/verification-result", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        bytedToken: params.bytedToken,
        projectName: assetProject.value.trim() || "default"
      })
    });
    setAssetStatus("verified", "done");
    setAssetHelp(`Группа ${data.GroupId || "создана"} готова.`, "done");
    await loadPrivateAssets();
    if (data.GroupId) assetGroupSelect.value = data.GroupId;
  } catch (error) {
    setAssetStatus("error", "error");
    setAssetHelp(error.message, "error");
  }
}

async function waitForPrivateAsset(assetId) {
  const params = new URLSearchParams({
    assetId,
    projectName: assetProject.value.trim() || "default"
  });
  for (let attempt = 0; attempt < 36; attempt += 1) {
    const asset = await apiFetch(`/api/assets/status?${params.toString()}`);
    const status = String(asset.Status || "Processing");
    setAssetStatus(status.toLowerCase(), status === "Active" ? "done" : status === "Failed" ? "error" : "idle");
    setAssetHelp(`Asset ${assetId}: ${status}${status === "Processing" ? ". Следующая проверка через 5 секунд." : ""}`);
    if (status === "Active" || status === "Failed") return asset;
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  throw new Error("Asset is still processing. Use Refresh library later.");
}

async function createPrivateAsset() {
  const file = assetUpload.files && assetUpload.files[0];
  if (!file) {
    setAssetHelp("Выберите image, video или audio файл.", "error");
    return;
  }
  createAssetBtn.disabled = true;
  setAssetStatus("uploading");
  setAssetHelp("Сохраняю материал в библиотеке...");
  try {
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch("/api/upload-reference", {
      method: "POST",
      body: formData
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Upload failed: HTTP ${response.status}`);
    const uploaded = Array.isArray(payload.files) ? payload.files[0] : null;
    if (!uploaded || !uploaded.material) throw new Error("Material record missing from upload response");
    upsertMaterial(uploaded.material);
    renderMaterials();
    assetUpload.value = "";
    setAssetStatus("saved", "done");
    setAssetHelp("Материал сохранён. Теперь можно использовать файл или отправить его на проверку BytePlus.", "done");
  } catch (error) {
    setAssetStatus("error", "error");
    setAssetHelp(error.message, "error");
  } finally {
    createAssetBtn.disabled = false;
  }
}

function updateDurationSlider() {
  const min = Number(durationEl.min || 4);
  const max = Number(durationEl.max || 15);
  const value = Number(durationEl.value || 5);
  const pct = ((value - min) / (max - min)) * 100;
  durationEl.style.background = `linear-gradient(to right, var(--accent) 0%, var(--accent) ${pct}%, var(--field) ${pct}%, var(--field) 100%)`;
  durationValue.textContent = `${value}s`;
}

function promptText() {
  return (promptEditor ? promptEditor.innerText : promptEl.value || "").replace(/\u00a0/g, " ");
}

function syncPromptField() {
  if (promptEl) promptEl.value = promptText();
}

function caretOffset(root) {
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount) return 0;
  const range = selection.getRangeAt(0);
  const before = range.cloneRange();
  before.selectNodeContents(root);
  before.setEnd(range.endContainer, range.endOffset);
  return before.toString().length;
}

function restoreCaret(root, offset) {
  const selection = window.getSelection();
  if (!selection) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let remaining = offset;
  let node = walker.nextNode();
  while (node) {
    const length = node.nodeValue.length;
    if (remaining <= length) {
      const range = document.createRange();
      range.setStart(node, remaining);
      range.collapse(true);
      selection.removeAllRanges();
      selection.addRange(range);
      return;
    }
    remaining -= length;
    node = walker.nextNode();
  }
  const range = document.createRange();
  range.selectNodeContents(root);
  range.collapse(false);
  selection.removeAllRanges();
  selection.addRange(range);
}

function renderPromptEditor(restore = true) {
  if (!promptEditor) return;
  const text = promptText();
  const offset = restore ? caretOffset(promptEditor) : text.length;
  promptEditor.replaceChildren();
  const parts = text.split(/(@image\d+)/gi);
  parts.forEach((part) => {
    if (!part) return;
    if (/^@image\d+$/i.test(part)) {
      const token = document.createElement("span");
      token.className = "prompt-token";
      token.textContent = part;
      promptEditor.appendChild(token);
    } else {
      promptEditor.appendChild(document.createTextNode(part));
    }
  });
  syncPromptField();
  if (restore) restoreCaret(promptEditor, offset);
}

function syncImageUrlsField() {
  const field = form.elements.imageUrls;
  if (!field) return;
  field.value = imageRefs.map((ref) => ref.url).filter(Boolean).join("\\n");
}

function syncMediaUrlsFields() {
  const videoField = form.elements.videoUrls;
  const audioField = form.elements.audioUrls;
  if (videoField) {
    videoField.value = mediaRefs.filter((ref) => ref.kind === "video").map((ref) => ref.url).filter(Boolean).join("\\n");
  }
  if (audioField) {
    audioField.value = mediaRefs.filter((ref) => ref.kind === "audio").map((ref) => ref.url).filter(Boolean).join("\\n");
  }
}

function imageRefLabel(ref) {
  if (ref.name) return ref.name;
  try {
    return new URL(ref.url).hostname;
  } catch {
    return ref.url || "Image";
  }
}

function addImageRef(ref) {
  if (ref.url && imageRefs.some((item) => item.url === ref.url)) return;
  imageRefs.push({
    id: `img-${++imageRefCounter}`,
    url: "",
    previewUrl: "",
    file: null,
    name: "",
    materialId: "",
    ...ref
  });
  syncImageUrlsField();
  renderImageReferences();
}

function promptImageUrls() {
  const prompt = String(promptEl.value || "");
  const matches = prompt.match(/https?:\/\/[^\s"'<>]+/g) || [];
  return matches
    .map((url) => url.replace(/[),.;]+$/, ""))
    .filter((url) => /\.(png|jpe?g|webp|gif|avif)(\?.*)?$/i.test(url));
}

function syncPromptImageUrls() {
  const urls = promptImageUrls();
  let changed = false;
  for (let index = imageRefs.length - 1; index >= 0; index -= 1) {
    const ref = imageRefs[index];
    if (ref.source === "prompt" && !urls.includes(ref.url)) {
      disposeImageRef(ref);
      imageRefs.splice(index, 1);
      changed = true;
    }
  }
  urls.forEach((url) => {
    if (!imageRefs.some((ref) => ref.url === url)) {
      imageRefs.push({
        id: `img-${++imageRefCounter}`,
        url,
        previewUrl: url,
        file: null,
        name: url,
        source: "prompt"
      });
      changed = true;
    }
  });
  if (changed) {
    syncImageUrlsField();
    renderImageReferences();
  }
}

function addImageFiles(files) {
  Array.from(files || [])
    .filter((file) => file.type.startsWith("image/"))
    .forEach((file) => {
      addImageRef({
        file,
        previewUrl: URL.createObjectURL(file),
        name: file.name
      });
    });
}

function disposeImageRef(ref) {
  if (ref.previewUrl && ref.previewUrl.startsWith("blob:")) {
    URL.revokeObjectURL(ref.previewUrl);
  }
}

function disposeMediaRef(ref) {
  if (ref.previewUrl && ref.previewUrl.startsWith("blob:")) {
    URL.revokeObjectURL(ref.previewUrl);
  }
}

function addMediaFiles(files) {
  Array.from(files || [])
    .filter((file) => file.type.startsWith("video/") || file.type.startsWith("audio/"))
    .forEach((file) => {
      mediaRefs.push({
        id: `media-${++mediaRefCounter}`,
        kind: file.type.startsWith("video/") ? "video" : "audio",
        file,
        url: "",
        previewUrl: URL.createObjectURL(file),
        name: file.name
      });
    });
  renderReferencePreview();
}

function handleReferenceFiles(files) {
  const list = Array.from(files || []);
  if (!list.length) return;
  addImageFiles(list);
  addMediaFiles(list);
  setUploadStatus(`Добавлено файлов: ${list.length}.`);
}

function removeImageRef(id) {
  const index = imageRefs.findIndex((ref) => ref.id === id);
  if (index < 0) return;
  disposeImageRef(imageRefs[index]);
  imageRefs.splice(index, 1);
  syncImageUrlsField();
  renderImageReferences();
  renderPrivateAssets();
  renderMaterials();
  setUploadStatus("Изображение удалено.");
}

function removeMediaRef(id) {
  const index = mediaRefs.findIndex((ref) => ref.id === id);
  if (index < 0) return;
  disposeMediaRef(mediaRefs[index]);
  mediaRefs.splice(index, 1);
  syncMediaUrlsFields();
  renderReferencePreview();
  renderPrivateAssets();
  renderMaterials();
  setUploadStatus("Файл удален.");
}

function moveImageRef(fromIndex, toIndex) {
  if (fromIndex < 0 || toIndex < 0 || fromIndex >= imageRefs.length || toIndex > imageRefs.length) {
    return;
  }
  if (fromIndex < toIndex) toIndex -= 1;
  if (fromIndex === toIndex) return;
  const [item] = imageRefs.splice(fromIndex, 1);
  imageRefs.splice(toIndex, 0, item);
  syncImageUrlsField();
  renderImageReferences();
}

async function checkAttachedHero(ref, button) {
  if (!state.config.assets.enabled) return;
  button.disabled = true;
  button.textContent = "Отправляю…";
  try {
    if (ref.file) {
      const previousPreview = ref.previewUrl;
      const uploaded = await uploadSingleReferenceFile(ref.file);
      ref.url = uploaded.url;
      ref.previewUrl = uploaded.url;
      ref.materialId = uploaded.material ? uploaded.material.id : "";
      ref.file = null;
      if (previousPreview && previousPreview.startsWith("blob:")) {
        URL.revokeObjectURL(previousPreview);
      }
      syncImageUrlsField();
    }

    const material = state.materials.find((item) =>
      (ref.materialId && item.id === ref.materialId) || item.url === ref.url
    );
    let assetId = "";
    let projectName = assetProject.value.trim() || "default";
    if (material) {
      button.textContent = "Проверяется…";
      const checked = String(material.assetStatus || "") === "Active"
        ? material
        : await reviewMaterialAsAigc(material);
      if (!checked || String(checked.assetStatus || "") !== "Active") {
        throw new Error(checked?.assetError || "BytePlus не подтвердил героя.");
      }
      assetId = checked.assetId;
      projectName = checked.assetProject || projectName;
      ref.previewUrl = checked.url || ref.previewUrl;
      ref.materialId = checked.id;
    } else {
      if (
        !String(ref.url || "").startsWith("http://") &&
        !String(ref.url || "").startsWith("https://")
      ) {
        throw new Error("Сначала загрузите изображение героя.");
      }
      const created = await apiFetch("/api/assets/check-generated", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: ref.url,
          name: ref.name || "attached-hero",
          projectName
        })
      });
      assetId = created.assetId;
      projectName = created.projectName || projectName;
      button.textContent = "Проверяется…";
      const checked = await waitForGeneratedHeroAsset(assetId, projectName);
      if (String(checked.Status || "") !== "Active") {
        throw new Error(
          checked.FailedReason || checked.Error?.Message || "BytePlus не подтвердил героя."
        );
      }
    }

    ref.url = `asset://${assetId}`;
    ref.source = "private-asset";
    ref.assetId = assetId;
    syncImageUrlsField();
    renderImageReferences();
    setUploadStatus(`Герой проверен и добавлен как asset://${assetId}.`);
  } catch (error) {
    button.disabled = false;
    button.textContent = "Повторить проверку";
    button.title = error.message;
    setUploadStatus(error.message, "error");
  }
}

function renderImageReferences() {
  imageReferenceList.innerHTML = "";
  imageRefs.forEach((ref, index) => {
    const item = document.createElement("article");
    item.className = "image-card";
    item.draggable = true;
    item.dataset.id = ref.id;

    const img = document.createElement("img");
    img.src = ref.previewUrl || ref.url;
    img.alt = imageRefLabel(ref);
    item.appendChild(img);

    const badge = document.createElement("div");
    badge.className = "image-card-badge";
    badge.textContent = String(index + 1);
    item.appendChild(badge);

    const checkButton = document.createElement("button");
    checkButton.className = "image-card-check";
    checkButton.type = "button";
    const alreadyChecked = String(ref.url || "").startsWith("asset://");
    checkButton.textContent = alreadyChecked ? "Герой проверен" : "Проверить героя";
    checkButton.disabled = alreadyChecked || !(state.config && state.config.assets && state.config.assets.enabled);
    checkButton.title = checkButton.disabled && !alreadyChecked ? "Добавьте BytePlus AK/SK" : "";
    checkButton.draggable = false;
    checkButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      checkAttachedHero(ref, checkButton);
    });
    item.appendChild(checkButton);

    const remove = document.createElement("button");
    remove.className = "reference-remove";
    remove.type = "button";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `Remove image ${index + 1}`);
    remove.draggable = false;
    remove.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      removeImageRef(ref.id);
    });
    item.appendChild(remove);

    item.addEventListener("dragstart", () => {
      draggedImageRefId = ref.id;
      item.classList.add("dragging");
    });
    item.addEventListener("dragend", () => {
      draggedImageRefId = null;
      item.classList.remove("dragging");
    });
    item.addEventListener("dragover", (event) => event.preventDefault());
    item.addEventListener("drop", (event) => {
      event.preventDefault();
      const fromIndex = imageRefs.findIndex((entry) => entry.id === draggedImageRefId);
      const targetIndex = imageRefs.findIndex((entry) => entry.id === ref.id);
      const rect = item.getBoundingClientRect();
      const insertAfter = event.clientX > rect.left + rect.width / 2;
      const toIndex = targetIndex + (insertAfter ? 1 : 0);
      moveImageRef(fromIndex, toIndex);
    });

    imageReferenceList.appendChild(item);
  });
}

function renderReferencePreview() {
  referencePreview.innerHTML = "";
  if (!mediaRefs.length) {
    referencePreview.hidden = true;
    return;
  }
  referencePreview.hidden = false;
  mediaRefs.slice(0, 10).forEach((ref) => {
    const item = document.createElement("div");
    item.className = "reference-thumb";
    if (ref.kind === "video") {
      const video = document.createElement("video");
      video.src = ref.previewUrl || ref.url;
      video.muted = true;
      video.playsInline = true;
      item.appendChild(video);
    } else {
      const label = document.createElement("span");
      label.textContent = ref.name || "Audio";
      item.appendChild(label);
    }
    const remove = document.createElement("button");
    remove.className = "reference-remove";
    remove.type = "button";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `Remove ${ref.kind} file`);
    remove.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      removeMediaRef(ref.id);
    });
    item.appendChild(remove);
    referencePreview.appendChild(item);
  });
  if (mediaRefs.length > 10) {
    const more = document.createElement("div");
    more.className = "reference-thumb";
    more.innerHTML = `<span>+${mediaRefs.length - 10} more</span>`;
    referencePreview.appendChild(more);
  }
}

async function uploadSingleReferenceFile(file) {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch("/api/upload-reference", {
    method: "POST",
    body: formData
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `Upload failed: HTTP ${response.status}`);
  }
  const first = Array.isArray(payload.files) ? payload.files[0] : null;
  if (!first || !first.url) {
    throw new Error("Upload failed: no URL returned");
  }
  if (first.material) {
    upsertMaterial(first.material);
    renderMaterials();
  }
  return first;
}

async function uploadImageReferences(onProgress) {
  let uploaded = 0;
  for (const ref of imageRefs) {
    if (!ref.file || ref.url) continue;
    const previousPreview = ref.previewUrl;
    const uploadResult = await uploadSingleReferenceFile(ref.file);
    ref.url = uploadResult.url;
    ref.previewUrl = ref.url;
    ref.materialId = uploadResult.material ? uploadResult.material.id : "";
    ref.file = null;
    if (previousPreview && previousPreview.startsWith("blob:")) {
      URL.revokeObjectURL(previousPreview);
    }
    uploaded += 1;
    syncImageUrlsField();
    renderImageReferences();
    if (onProgress) onProgress(uploaded);
  }
  syncImageUrlsField();
  return uploaded;
}

async function uploadMediaReferences(onProgress) {
  let uploaded = 0;
  for (const ref of mediaRefs) {
    if (!ref.file || ref.url) continue;
    const previousPreview = ref.previewUrl;
    const uploadResult = await uploadSingleReferenceFile(ref.file);
    ref.url = uploadResult.url;
    ref.previewUrl = ref.url;
    ref.materialId = uploadResult.material ? uploadResult.material.id : "";
    ref.file = null;
    if (previousPreview && previousPreview.startsWith("blob:")) {
      URL.revokeObjectURL(previousPreview);
    }
    uploaded += 1;
    syncMediaUrlsFields();
    renderReferencePreview();
    if (onProgress) onProgress(uploaded);
  }
  syncMediaUrlsFields();
  return uploaded;
}

async function uploadReferenceFiles() {
  syncPromptImageUrls();
  const pendingImages = imageRefs.filter((ref) => ref.file && !ref.url).length;
  const pendingMedia = mediaRefs.filter((ref) => ref.file && !ref.url).length;
  const totalFiles = pendingImages + pendingMedia;
  syncImageUrlsField();
  syncMediaUrlsFields();
  if (!totalFiles) return;
  setUploadStatus(`Загружаю файлов: ${totalFiles}...`, "busy");
  let uploaded = 0;
  await uploadImageReferences((count) => {
    uploaded = count;
    setUploadStatus(`Загружено файлов: ${uploaded} из ${totalFiles}.`, "busy");
  });
  await uploadMediaReferences((count) => {
    uploaded = pendingImages + count;
    setUploadStatus(`Загружено файлов: ${uploaded} из ${totalFiles}.`, "busy");
  });
  setUploadStatus(`Загружено файлов: ${uploaded}. References готовы.`);
  renderReferencePreview();
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || data.message || `HTTP ${response.status}`);
  }
  return data;
}

async function pollStatus(manual = false) {
  if (!state.taskId) return;
  const params = new URLSearchParams({
    provider: state.provider,
    taskId: state.taskId,
    baseUrl: currentProvider().baseUrl
  });
  try {
    const data = await apiFetch(`/api/status?${params.toString()}`);
    pretty(data);
    const status = String(data.status || "UNKNOWN");
    const lower = status.toLowerCase();
    setStatus(status, lower.includes("success") || lower.includes("completed") ? "done" : lower.includes("fail") ? "error" : "");
    if (data.videoUrls && data.videoUrls.length) {
      videoWrap.innerHTML = `<video src="${data.videoUrls[0]}" controls playsinline></video>`;
      taskLine.innerHTML = `Task <strong>${state.taskId}</strong>`;
      clearInterval(state.pollTimer);
    } else if (data.error) {
      clearInterval(state.pollTimer);
    } else if (manual) {
      taskLine.innerHTML = `Task <strong>${state.taskId}</strong> обновлена.`;
    }
  } catch (error) {
    setStatus("error", "error");
    pretty({ error: error.message });
  }
}

function setHeroCheckStatus(element, message, kind = "") {
  element.textContent = message;
  element.className = `hero-check-status ${kind}`;
}

async function waitForGeneratedHeroAsset(assetId, projectName, onStatus) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const params = new URLSearchParams({ assetId, projectName });
    const asset = await apiFetch(`/api/assets/status?${params.toString()}`);
    const status = String(asset.Status || "Processing");
    if (onStatus) onStatus(status, asset);
    if (status === "Active" || status === "Failed") return asset;
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  throw new Error("Проверка BytePlus не завершилась за 10 минут.");
}

function renderGeneratedHeroReady(actions, statusElement, entry) {
  actions.replaceChildren();
  const copyButton = document.createElement("button");
  copyButton.type = "button";
  copyButton.className = "secondary";
  copyButton.textContent = "Copy Asset ID";
  copyButton.addEventListener("click", () => copyAssetId(entry.assetId));

  const useButton = document.createElement("button");
  useButton.type = "button";
  useButton.textContent = "Use in video";
  useButton.addEventListener("click", () => {
    addPrivateAssetReference({
      Id: entry.assetId,
      AssetType: "Image",
      Name: "Generated hero",
      URL: entry.url,
      Status: "Active"
    });
    setHeroCheckStatus(statusElement, `Герой добавлен как asset://${entry.assetId}.`, "done");
  });
  actions.append(copyButton, useButton);
}

async function checkGeneratedHero(url, index, checkButton, actions, statusElement) {
  checkButton.disabled = true;
  setHeroCheckStatus(statusElement, "Создаю виртуальный ассет и запускаю проверку…");
  try {
    const created = await apiFetch("/api/assets/check-generated", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        name: `generated-hero-${index + 1}`,
        projectName: assetProject.value.trim() || "default"
      })
    });
    const entry = {
      assetId: created.assetId,
      assetUri: created.assetUri,
      groupId: created.groupId,
      projectName: created.projectName,
      status: created.status || "Processing",
      url
    };
    state.generatedAssets[index] = entry;
    setHeroCheckStatus(statusElement, `BytePlus: ${entry.status} · ${entry.assetId}`);
    const asset = await waitForGeneratedHeroAsset(
      entry.assetId,
      entry.projectName,
      (status) => {
        entry.status = status;
        setHeroCheckStatus(statusElement, `BytePlus: ${status} · ${entry.assetId}`);
      }
    );
    entry.status = String(asset.Status || entry.status);
    if (entry.status === "Failed") {
      throw new Error(
        asset.FailedReason || asset.Error?.Message || asset.Message || "BytePlus отклонил героя."
      );
    }
    setHeroCheckStatus(statusElement, `Проверка пройдена · ${entry.assetId}`, "done");
    renderGeneratedHeroReady(actions, statusElement, entry);
  } catch (error) {
    setHeroCheckStatus(statusElement, error.message, "error");
    checkButton.disabled = false;
    checkButton.textContent = "Повторить проверку";
  }
}

function renderImageResults(urls) {
  const list = Array.isArray(urls) ? urls.filter(Boolean) : [];
  state.generatedAssets = {};
  if (!list.length) {
    videoWrap.innerHTML = `<div class="empty">Изображения не вернулись в ответе API.</div>`;
    return;
  }
  const grid = document.createElement("div");
  grid.className = "image-result-grid";
  list.forEach((url, index) => {
    const card = document.createElement("article");
    card.className = "image-result-card";
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noreferrer";
    const img = document.createElement("img");
    img.src = url;
    img.alt = `Generated image ${index + 1}`;
    link.appendChild(img);

    const panel = document.createElement("div");
    panel.className = "hero-check-panel";
    const actions = document.createElement("div");
    actions.className = "hero-check-actions";
    const checkButton = document.createElement("button");
    checkButton.type = "button";
    checkButton.textContent = "Проверить героя";
    checkButton.disabled = !(state.config && state.config.assets && state.config.assets.enabled);
    checkButton.title = checkButton.disabled ? "Добавьте BytePlus AK/SK" : "";
    const checkStatus = document.createElement("div");
    setHeroCheckStatus(
      checkStatus,
      checkButton.disabled
        ? "Для проверки нужны BytePlus AK/SK."
        : "Создаст AIGC-ассет и дождётся результата проверки."
    );
    checkButton.addEventListener("click", () => {
      checkGeneratedHero(url, index, checkButton, actions, checkStatus);
    });
    actions.appendChild(checkButton);
    panel.append(actions, checkStatus);
    card.append(link, panel);
    grid.appendChild(card);
  });
  videoWrap.replaceChildren(grid);
}

async function submitGeneration(event) {
  event.preventDefault();
  submitBtn.disabled = true;
  submitBtn.textContent = "Submitting...";
  setStatus("submitting");
  videoWrap.innerHTML = `<div class="empty">${state.mode === "image" ? "Изображение генерируется..." : "Задача отправляется..."}</div>`;
  try {
    await uploadReferenceFiles();
    const payload = collectPayload();
    const endpoint = state.mode === "image" ? "/api/generate-image" : "/api/generate";
    const data = await apiFetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    pretty(data);
    if (state.mode === "image") {
      state.taskId = "";
      clearInterval(state.pollTimer);
      pollBtn.disabled = true;
      renderImageResults(data.imageUrls || []);
      taskLine.textContent = `Generated ${data.imageUrls ? data.imageUrls.length : 0} image(s).`;
      setStatus("succeeded", "done");
      return;
    }
    state.taskId = data.taskId;
    taskLine.innerHTML = `Task <strong>${state.taskId}</strong>`;
    setStatus(data.status || "processing");
    pollBtn.disabled = false;
    clearInterval(state.pollTimer);
    state.pollTimer = setInterval(() => pollStatus(false), 8000);
    setTimeout(() => pollStatus(false), 1200);
  } catch (error) {
    setStatus("error", "error");
    setUploadStatus(error.message, "error");
    taskLine.textContent = error.message;
    pretty({ error: error.message });
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = state.mode === "image" ? "Generate image" : "Generate video";
  }
}

async function boot() {
  state.config = await apiFetch("/api/config");
  state.provider = state.config.providers.byteplus ? "byteplus" : Object.keys(state.config.providers)[0];
  state.providerConfig = state.config.providers[state.provider];
  assetProject.value = state.config.assets.projectName || "default";
  const assetApiEnabled = Boolean(state.config.assets.enabled);
  [assetProject, assetGroupSelect, startVerificationBtn, refreshAssetsBtn].forEach((control) => {
    control.disabled = !assetApiEnabled;
  });
  if (assetApiEnabled) {
    setAssetStatus("ready", "done");
    setAssetHelp("Виртуального героя проверяйте через Files или Result. H5 нужен только для реального человека.");
  } else {
    setAssetStatus("local only", "idle");
    setAssetHelp("Материалы можно сохранять и переиспользовать. Для отправки на проверку добавьте BytePlus AK/SK.");
  }
  form.addEventListener("submit", submitGeneration);
  modeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      setMode(button.dataset.mode);
      if (state.mode === "image") {
        taskLine.textContent = "Seedream image generation is ready.";
        videoWrap.innerHTML = `<div class="empty">Изображение появится здесь после генерации.</div>`;
      } else {
        taskLine.textContent = state.taskId ? `Task ${state.taskId}` : "Задача еще не отправлена.";
        videoWrap.innerHTML = `<div class="empty">MP4 появится здесь после завершения задачи.</div>`;
      }
      setStatus("idle", "idle");
    });
  });
  pollBtn.addEventListener("click", () => pollStatus(true));
  startVerificationBtn.addEventListener("click", startAssetVerification);
  refreshAssetsBtn.addEventListener("click", () => loadPrivateAssets(Boolean(assetGroupSelect.value)));
  createAssetBtn.addEventListener("click", createPrivateAsset);
  addAssetByIdBtn.addEventListener("click", addManualAssetReference);
  manualAssetInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    addManualAssetReference();
  });
  assetGroupSelect.addEventListener("change", () => {
    renderMaterials();
    loadPrivateAssets(true);
  });
  assetProject.addEventListener("change", () => loadPrivateAssets());
  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin) return;
    if (!event.data || event.data.type !== "byteplus-asset-verification") return;
    finishAssetVerification(event.data.params || {});
  });
  if (referenceUpload) {
    const dropZone = referenceUpload.closest(".upload-tile");
    referenceUpload.addEventListener("change", () => {
      handleReferenceFiles(referenceUpload.files);
      referenceUpload.value = "";
    });
    if (dropZone) {
      ["dragenter", "dragover"].forEach((eventName) => {
        dropZone.addEventListener(eventName, (event) => {
          event.preventDefault();
          event.stopPropagation();
          dropZone.classList.add("is-dragging-over");
        });
      });
      ["dragleave", "drop"].forEach((eventName) => {
        dropZone.addEventListener(eventName, (event) => {
          event.preventDefault();
          event.stopPropagation();
          dropZone.classList.remove("is-dragging-over");
        });
      });
      dropZone.addEventListener("drop", (event) => {
        handleReferenceFiles(event.dataTransfer ? event.dataTransfer.files : []);
      });
    }
  }
  if (promptEditor && promptEl) {
    promptEditor.textContent = promptEl.value || "";
    renderPromptEditor(false);
    promptEditor.addEventListener("input", () => {
      renderPromptEditor(true);
      syncPromptImageUrls();
    });
    syncPromptImageUrls();
  }
  durationEl.addEventListener("input", updateDurationSlider);
  refreshProviderFields();
  await loadMaterials();
  if (assetApiEnabled) await loadPrivateAssets();
  pretty({ ready: true, provider: state.provider });
}

boot().catch((error) => {
  keyStatus.textContent = "boot error";
  keyStatus.className = "status-pill error";
  pretty({ error: error.message });
});
"""


def main():
    server = ThreadingHTTPServer((APP_HOST, APP_PORT), SeedanceHandler)
    print(f"Seedance web service: http://{APP_HOST}:{APP_PORT}", flush=True)
    print(f"Token file: {TOKEN_FILE}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.", flush=True)


if __name__ == "__main__":
    main()
