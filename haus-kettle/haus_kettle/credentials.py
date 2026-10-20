"""Reuse an explicitly selected HA Xiaomi account or a local token; never log secrets."""

import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from .model import MODEL


class CredentialsError(Exception):
    pass


def api(path, data=None):
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise CredentialsError("Home Assistant access is unavailable.")
    request = urllib.request.Request(
        "http://supervisor" + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
            return result.get("data", result) if isinstance(result, dict) else result
    except (OSError, ValueError, urllib.error.HTTPError):
        raise CredentialsError(
            "Check the connection in Home Assistant Integrations; Xiaomi may need sign-in or verification."
        ) from None


def validate(credentials):
    try:
        address = ipaddress.ip_address(credentials.get("host", ""))
    except ValueError:
        raise CredentialsError("Enter the kettle's local IP address.") from None
    if (
        not address.is_private
        or address.is_loopback
        or address.is_multicast
        or address.is_unspecified
    ):
        raise CredentialsError("The kettle must have a private network address.")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", credentials.get("token", "")):
        raise CredentialsError("Enter the kettle's 32-character local token.")
    if (
        not re.fullmatch(r"[0-9]{1,10}", str(credentials.get("did", "")))
        or not 0 < int(credentials["did"]) <= 0xFFFFFFFF
    ):
        raise CredentialsError("Enter a valid Xiaomi device ID.")
    if credentials.get("model", MODEL) != MODEL:
        raise CredentialsError("This model is not supported by Haus Kettle yet.")
    return {
        "host": str(address),
        "token": credentials["token"],
        "did": str(credentials["did"]),
        "model": MODEL,
    }


def save(path, credentials):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(credentials, handle)
    temporary.replace(path)
    path.chmod(0o600)


def load(options, path):
    if options.get("source", "home_assistant") == "local":
        return validate(
            {
                "host": options.get("host", ""),
                "token": options.get("token", ""),
                "did": options.get("device_id", ""),
                "model": MODEL,
            }
        )
    try:
        credentials = validate(json.loads(Path(path).read_text()))
        if not options.get("device_id") or str(credentials["did"]) == str(options["device_id"]):
            return credentials
    except (OSError, ValueError, CredentialsError):
        pass
    return refresh(options, path)


def refresh(options, path, *, renew=False):
    if options.get("source", "home_assistant") == "local":
        return load(options, path)
    if renew:
        # Existing integration owns regional accounts, expired sessions and 2FA UI.
        api("/core/api/services/xiaomi_miot/renew_devices", {})
    result = api("/core/api/services/xiaomi_miot/get_token?return_response", {"name": MODEL})
    response = result.get("service_response", result) if isinstance(result, dict) else None
    if not isinstance(response, dict) or not isinstance(response.get("list"), list):
        raise CredentialsError(
            "Reconnect the Xiaomi integration in Home Assistant to import this kettle."
        )
    rows = response["list"]
    selected = [
        r
        for r in rows
        if isinstance(r, dict)
        and r.get("model") == MODEL
        and (not options.get("device_id") or str(r.get("did")) == str(options["device_id"]))
    ]
    if len(selected) != 1:
        raise CredentialsError(
            "Select a device ID, or reconnect the Xiaomi integration in Home Assistant."
        )
    credentials = validate(selected[0])
    save(path, credentials)
    return credentials


def mqtt_options(options):
    configured = options.get("mqtt", {})
    if configured.get("host"):
        return configured
    service = api("/services/mqtt")
    return {
        "host": service["host"],
        "port": service.get("port", 1883),
        "username": service.get("username", ""),
        "password": service.get("password", ""),
        "ssl": bool(service.get("ssl")),
    }
