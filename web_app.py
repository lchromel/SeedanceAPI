#!/usr/bin/env python3
import cgi
import json
import base64
import mimetypes
import os
import re
import sys
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
UPLOAD_DIR = os.path.join(ROOT_DIR, "uploads")
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
    file_name = safe_upload_name(original_name, content_type)
    path = os.path.join(UPLOAD_DIR, file_name)
    total = 0
    with open(path, "wb") as output:
        while True:
            chunk = file_obj.read(1024 * 256)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                output.close()
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise ValueError(f"File is too large. Limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
            output.write(chunk)
    if total <= 0:
        raise ValueError("Uploaded file is empty")
    return {
        "fileName": file_name,
        "originalName": original_name,
        "contentType": content_type,
        "size": total,
        "path": path,
    }


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
    if not normalized or normalized.startswith("data:"):
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

        first_frame = str(data.get("firstFrameUrl") or "").strip()
        last_frame = str(data.get("lastFrameUrl") or "").strip()
        seed_image_urls = []
        if first_frame:
            seed_image_urls.append((first_frame, "first_frame"))
        elif image_urls:
            seed_image_urls.append((image_urls[0], "first_frame"))
        if last_frame:
            seed_image_urls.append((last_frame, "last_frame"))

        for image_url, role in seed_image_urls:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": remote_image_as_data_url(image_url)},
                    "role": role,
                }
            )
        for video_url in video_urls[:3]:
            content.append({"type": "video_url", "video_url": {"url": video_url}})
        for audio_url in audio_urls[:3]:
            content.append({"type": "audio_url", "audio_url": {"url": audio_url}})

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
            json_response(self, 200, {"providers": providers})
            return
        if path == "/api/status":
            self.handle_status(params)
            return
        json_response(self, 404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/api/generate":
            self.handle_generate()
            return
        if self.path == "/api/upload-reference":
            self.handle_upload_reference()
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
                url = f"{base}/uploads/{urllib.parse.quote(saved['fileName'])}"
                uploads.append(
                    {
                        "url": url,
                        "fileName": saved["fileName"],
                        "originalName": saved["originalName"],
                        "contentType": saved["contentType"],
                        "size": saved["size"],
                    }
                )
            if not uploads:
                raise ValueError("file is required")
            json_response(self, 200, {"files": uploads})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})
        except OSError as exc:
            json_response(self, 500, {"error": str(exc)})

    def handle_generate(self):
        try:
            data = read_json_body(self)
            provider_id = data.get("provider") or "byteplus"
            provider = provider_config(provider_id)
            api_key = str(data.get("apiKey") or get_secret(provider["token_names"])).strip()
            if not api_key:
                names = ", ".join(provider["token_names"])
                raise PermissionError(f"API key не найден. Добавьте {names} в {TOKEN_FILE} или введите ключ в UI.")
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
                raise PermissionError(f"API key не найден. Добавьте {names} в {TOKEN_FILE} или введите ключ в UI.")
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
            <h1>Seedance 2 Video Studio</h1>
            <p>Async API — submit task, poll status, preview MP4</p>
          </div>
        </div>
        <div class="status-pill" id="keyStatus">Проверка ключа...</div>
      </div>

      <form id="generationForm" class="panel">
        <section class="form-section scene-section">
          <h2>Scene</h2>
          <label>Prompt
            <textarea name="prompt" rows="7" maxlength="4000" placeholder="Describe your video scene...">A cinematic aerial shot over coastline at golden hour, slow push-in, soft natural light</textarea>
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

        <section class="form-section settings-section">
          <h2>Settings</h2>
          <div class="duration-control">
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
        </section>

        <div class="checks">
          <label><input name="generateAudio" type="checkbox"> Generate audio</label>
          <label><input name="returnLastFrame" type="checkbox"> Return last frame</label>
          <label><input name="webSearch" type="checkbox"> Web search tool</label>
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
        <div class="empty">MP4 появится здесь после завершения задачи.</div>
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

form { display: grid; gap: 16px; }
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
  min-height: 126px;
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
  .upload-grid, .image-reference-list, .reference-preview { grid-template-columns: 1fr; }
  .topbar, .result-head { display: grid; }
  .actions { display: grid; }
}
"""


JS = """
const state = {
  config: null,
  taskId: "",
  provider: "byteplus",
  providerConfig: null,
  baseUrl: "",
  pollTimer: null
};

const $ = (selector) => document.querySelector(selector);
const form = $("#generationForm");
const durationEl = $("#duration");
const durationValue = $("#durationValue");
const ratioEl = $("#aspectRatio");
const resolutionEl = $("#resolution");
const rawOutput = $("#rawOutput");
const taskLine = $("#taskLine");
const taskStatus = $("#taskStatus");
const videoWrap = $("#videoWrap");
const pollBtn = $("#pollBtn");
const submitBtn = $("#submitBtn");
const keyStatus = $("#keyStatus");
const uploadStatus = $("#uploadStatus");
const promptEl = form.elements.prompt;
const referenceUpload = $("#referenceUpload");
const imageReferenceList = $("#imageReferenceList");
const referencePreview = $("#referencePreview");
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
  updateDurationSlider();
}

function collectPayload() {
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
  if (resolutionEl.disabled) delete data.resolution;
  return data;
}

function setUploadStatus(message, kind = "") {
  uploadStatus.textContent = message;
  uploadStatus.className = `upload-status ${kind}`;
}

function updateDurationSlider() {
  const min = Number(durationEl.min || 4);
  const max = Number(durationEl.max || 15);
  const value = Number(durationEl.value || 5);
  const pct = ((value - min) / (max - min)) * 100;
  durationEl.style.background = `linear-gradient(to right, var(--accent) 0%, var(--accent) ${pct}%, var(--field) ${pct}%, var(--field) 100%)`;
  durationValue.textContent = `${value}s`;
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

function moveImageRef(fromIndex, toIndex) {
  if (fromIndex === toIndex || fromIndex < 0 || toIndex < 0 || fromIndex >= imageRefs.length || toIndex >= imageRefs.length) {
    return;
  }
  const [item] = imageRefs.splice(fromIndex, 1);
  imageRefs.splice(toIndex, 0, item);
  syncImageUrlsField();
  renderImageReferences();
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
      const toIndex = imageRefs.findIndex((entry) => entry.id === ref.id);
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
  return first.url;
}

async function uploadImageReferences(onProgress) {
  let uploaded = 0;
  for (const ref of imageRefs) {
    if (!ref.file || ref.url) continue;
    const previousPreview = ref.previewUrl;
    ref.url = await uploadSingleReferenceFile(ref.file);
    ref.previewUrl = ref.url;
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
    ref.url = await uploadSingleReferenceFile(ref.file);
    ref.previewUrl = ref.url;
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

async function submitGeneration(event) {
  event.preventDefault();
  submitBtn.disabled = true;
  submitBtn.textContent = "Submitting...";
  setStatus("submitting");
  videoWrap.innerHTML = `<div class="empty">Задача отправляется...</div>`;
  try {
    await uploadReferenceFiles();
    const payload = collectPayload();
    const data = await apiFetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    pretty(data);
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
    submitBtn.textContent = "Generate video";
  }
}

async function boot() {
  state.config = await apiFetch("/api/config");
  state.provider = state.config.providers.byteplus ? "byteplus" : Object.keys(state.config.providers)[0];
  state.providerConfig = state.config.providers[state.provider];
  form.addEventListener("submit", submitGeneration);
  pollBtn.addEventListener("click", () => pollStatus(true));
  if (referenceUpload) {
    referenceUpload.addEventListener("change", () => {
      addImageFiles(referenceUpload.files);
      addMediaFiles(referenceUpload.files);
      referenceUpload.value = "";
    });
  }
  if (promptEl) {
    promptEl.addEventListener("input", syncPromptImageUrls);
    syncPromptImageUrls();
  }
  durationEl.addEventListener("input", updateDurationSlider);
  refreshProviderFields();
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
