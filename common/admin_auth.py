"""Same-origin administrative pages with short-lived local browser sessions."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import socket
import struct
from urllib.parse import urlsplit

from flask import g, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from common.local_auth import admin_token, token_matches


def _loopback(address):
    try:
        return ipaddress.ip_address(address or "").is_loopback
    except ValueError:
        return False


def _local_peer(address):
    if _loopback(address):
        return True
    # Compose publishes only to host loopback. Docker forwards that connection
    # through its bridge gateway; never trust arbitrary X-Forwarded-For headers.
    if os.getenv("BINES_ADMIN_LOCAL_PROXY") != "docker":
        return False
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                gateway = socket.inet_ntoa(struct.pack("<I", int(fields[2], 16)))
                if address == gateway:
                    return True
    except (OSError, ValueError):
        pass
    return False


def install_admin_guard(app, name, *, entrypoints=("/",), token_provider=admin_token):
    """Bootstrap only direct local page visits; API calls require a session or token.

    Forwarded headers never grant local access. Remote clients must explicitly
    configure an allowed Host and authenticate with Authorization: Bearer.
    """
    cookie_name = f"bines_{name}_session"

    def serializer():
        return URLSafeTimedSerializer(token_provider(), salt=f"bines-admin:{name}")

    @app.before_request
    def authenticate():
        host = urlsplit(request.host_url).hostname
        allowed = {"localhost", "127.0.0.1", "::1"}
        allowed.update(part.strip().lower() for part in
                       os.getenv("BINES_ADMIN_ALLOWED_HOSTS", "").split(",") if part.strip())
        if not host or host.lower() not in allowed:
            return jsonify(success=False, error="untrusted host"), 403
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify(success=False, error="cross-origin request denied"), 403
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify(success=False, error="cross-site request denied"), 403

        authorization = request.headers.get("Authorization", "")
        if authorization and token_matches(authorization, f"Bearer {token_provider()}"):
            return None
        cookie = request.cookies.get(cookie_name)
        if cookie:
            try:
                if serializer().loads(cookie, max_age=86400) == {"admin": True}:
                    return None
            except (BadSignature, SignatureExpired):
                pass
        if (request.method == "GET" and request.path in entrypoints
                and host.lower() in {"localhost", "127.0.0.1", "::1"} and _local_peer(request.remote_addr)):
            g.bines_admin_bootstrap = True
            return None
        return jsonify(success=False, error="open the local management page or provide an admin token"), 401

    @app.after_request
    def secure_response(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if getattr(g, "bines_admin_bootstrap", False) and response.status_code < 400:
            response.set_cookie(cookie_name, serializer().dumps({"admin": True}), max_age=86400,
                                httponly=True, secure=request.is_secure, samesite="Strict")
        return response
