"""Tidal connector (oauth_device).

Uses standard OAuth device flow against Tidal's auth endpoints, then
persists the tokens for the engine to initialize `tidalapi.Session`.
"""

import base64
import os
import requests
from typing import Literal

from .base import ConnStatus, Connector, DeviceCode, Field


def _tidal_client_creds():
    # Extracted from tidalapi (Config class)
    client_id = base64.b64decode(
        base64.b64decode(b"WmxneVNuaGtiVzUw") + base64.b64decode(b"V2xkTE1HbDRWQT09")
    ).decode("utf-8")
    client_secret = base64.b64decode(
        base64.b64decode(b"TVU1dU9VRm1SRUZxZUhKblNrWktZa3RPVjB4bFFY")
        + base64.b64decode(b"bExSMVpIYlVsT2RWaFFVRXhJVmxoQmRuaEJaejA9")
    ).decode("utf-8")
    return client_id, client_secret


class TidalConnector(Connector):
    id = "tidal"
    name = "Tidal"
    auth_kind = "oauth_device"
    config_fields = []  # No manual config required; client IDs are embedded.

    def _auth_file(self):
        return os.getenv("TIDAL_AUTH_FILE") or self._store.get("TIDAL_AUTH_FILE") or "data/tidal_oauth.json"

    def status(self) -> ConnStatus:
        if os.path.exists(self._auth_file()):
            return ConnStatus("connected", "token present")
        return ConnStatus("unconfigured", "not authorized yet")

    def begin_device(self) -> DeviceCode:
        client_id, _ = _tidal_client_creds()
        r = requests.post(
            "https://auth.tidal.com/v1/oauth2/device_authorization",
            data={"client_id": client_id, "scope": "r_usr w_usr w_sub"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        url = data.get("verificationUriComplete") or data.get("verificationUri")
        if url and not url.startswith("http"):
            url = f"https://{url}"
            
        return DeviceCode(
            user_code=data["userCode"],
            verification_url=url,
            device_code=data["deviceCode"],
            interval=data.get("interval", 5),
        )

    def poll_device(self, dc: DeviceCode) -> ConnStatus:
        import json
        
        client_id, client_secret = _tidal_client_creds()
        auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        
        r = requests.post(
            "https://auth.tidal.com/v1/oauth2/token",
            headers={"Authorization": f"Basic {auth_header}"},
            data={
                "device_code": dc.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "scope": "r_usr w_usr w_sub",
            },
            timeout=10,
        )
        
        if r.status_code == 400:
            data = r.json()
            error = data.get("error", "")
            if error == "authorization_pending":
                return ConnStatus("unconfigured", "waiting for authorization (pending)")
            elif error == "expired_token":
                return ConnStatus("unconfigured", "device code expired")
            else:
                return ConnStatus("unconfigured", f"waiting for authorization ({error})")
                
        r.raise_for_status()
        data = r.json()
        
        path = self._auth_file()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
            
        return ConnStatus("connected", "authorized")
