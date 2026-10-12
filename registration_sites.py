"""Site routing. Device credentials are read only from secrets/environment."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_SITES = {'Buguruni': {'url': 'http://217.29.138.10:4376'}, 'Puma Upanga': {'url': 'http://217.29.138.128:4373'}, 'Puma Ocean Road': {'url': 'http://217.29.138.127:4374'}, 'Puma Survey': {'url': 'http://102.205.250.241:4378'}, 'India': {'url': 'http://102.205.251.187:4440'}, 'Kibaha': {'url': 'https://agent-unsavory-scope.ngrok-free.dev'}, 'Livingstone': {'url': 'http://217.29.138.29:4735'}}


@dataclass(frozen=True)
class Site:
    name: str
    url: str
    username: str
    password: str
    enrollment_pin: str
    timeout: int
    verify_tls: bool

    @property
    def ready(self) -> bool:
        return bool(self.url and self.password and self.enrollment_pin)


def load_sites(secrets: Mapping, environ: Mapping) -> dict[str, Site]:
    """Merge per-site secrets and env overrides; never share device passwords."""
    def global_setting(key, default=""):
        return str(secrets.get(key, environ.get(key, default))).strip()

    configured = secrets.get("sites", {})
    names = list(DEFAULT_SITES)
    names.extend(name for name in configured if name not in names)
    result = {}
    for name in names:
        values = configured.get(name, {})
        prefix = "SITE_" + re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")

        def value(key, default=""):
            return str(values.get(key, environ.get(f"{prefix}_{key.upper()}", default))).strip()

        # Existing one-device installations keep working as Buguruni only.
        legacy = name == "Buguruni" and not values and not any(
            key.startswith(prefix + "_") for key in environ
        )
        url = value("url", global_setting("HIKVISION_URL", DEFAULT_SITES.get(name, {}).get("url", ""))
                    if legacy else DEFAULT_SITES.get(name, {}).get("url", ""))
        password = value("password", global_setting("HIKVISION_PASSWORD") if legacy else "")
        try:
            timeout = int(value("timeout", global_setting("HIKVISION_TIMEOUT", "45")))
        except ValueError:
            timeout = 45
        result[name] = Site(
            name=name, url=url,
            username=value("username", global_setting("HIKVISION_USERNAME", "admin")),
            password=password,
            enrollment_pin=value("enrollment_pin", global_setting("ENROLLMENT_PIN")),
            timeout=max(1, timeout),
            verify_tls=value("verify_tls", global_setting("HIKVISION_VERIFY_TLS", "false")).lower() == "true",
        )
    return result

