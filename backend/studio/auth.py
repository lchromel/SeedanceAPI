import hashlib
import json
import time
from datetime import timedelta

import pyotp
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .models import LoginBucket, MfaDevice


@ensure_csrf_cookie
def session(request):
    return JsonResponse(
        {
            "user": {"id": request.user.id, "name": request.user.username}
            if request.user.is_authenticated
            else None,
            "csrfToken": get_token(request),
        }
    )


@csrf_protect
@require_POST
def sign_in(request):
    if len(request.body) > 4096:
        return JsonResponse({"detail": "Invalid credentials."}, status=400)
    try:
        data = json.loads(request.body)
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        code = str(data.get("code", ""))
    except (ValueError, AttributeError):
        return JsonResponse({"detail": "Invalid credentials."}, status=400)
    now = timezone.now()
    # Rate-limit usernames, plus direct peer IP. Do not trust arbitrary X-Forwarded-For.
    for value, limit in [
        ("user:" + username.casefold(), 8),
        ("ip:" + request.META.get("REMOTE_ADDR", ""), 60),
    ]:
        key = hashlib.sha256(value.encode()).hexdigest()
        with transaction.atomic():
            LoginBucket.objects.get_or_create(key=key, defaults={"window": now})
            bucket = LoginBucket.objects.select_for_update().get(key=key)
            if now - bucket.window > timedelta(minutes=15):
                bucket.count, bucket.window = 0, now
            if bucket.count >= limit:
                return JsonResponse(
                    {"detail": "Too many attempts. Try again in 15 minutes."},
                    status=429,
                )
            bucket.count += 1
            bucket.save()
    user = (
        authenticate(request, username=username, password=password)
        if len(password) <= 1024
        else None
    )
    if user:
        with transaction.atomic():
            device = MfaDevice.objects.select_for_update().filter(user=user).first()
            if device:
                secret = (
                    Fernet(settings.MFA_ENCRYPTION_KEY.encode())
                    .decrypt(device.encrypted_secret.encode())
                    .decode()
                )
                totp = pyotp.TOTP(secret)
                counter = int(time.time()) // 30
                matched = next(
                    (
                        c
                        for c in (counter - 1, counter, counter + 1)
                        if c > device.last_counter
                        and pyotp.utils.strings_equal(totp.generate_otp(c), code)
                    ),
                    None,
                )
                if matched is None:
                    user = None
                else:
                    device.last_counter = matched
                    device.save(update_fields=["last_counter"])
            elif settings.MFA_REQUIRED:
                user = None
    if not user:
        return JsonResponse({"detail": "Invalid credentials or verification code."}, status=401)
    login(request, user)
    return JsonResponse(
        {
            "user": {"id": user.id, "name": user.username},
            "csrfToken": get_token(request),
        }
    )


@csrf_protect
@require_POST
def sign_out(request):
    logout(request)
    return JsonResponse({"ok": True})
