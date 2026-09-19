from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time
from typing import Any, Optional

from curl_cffi import requests

from .base import BaseEmailClient, MessageSummary


class BoomlifyComClient(BaseEmailClient):
    """Boomlify temporary mailbox client."""

    supports_random_address = True
    supports_custom_localpart = True
    supports_custom_domain = True
    supports_custom_address = True

    BASE_URL = "https://v1.boomlify.com"
    DEFAULT_TRANSPORT_KEY = "7a9b3c8d2e1f4g5h6i9j0k8l2m4n6o8p"
    TRANSPORT_KEY_RING: dict[str, str] = {
        "hgjfh": "rk4kA9fQm8v7W4d2TzX1Y",
        "hgjfhg": "t2PzKd9sQw1Lm3XyVbN6R",
        "hihji": "bV7nL2cMzR6eJ8QaHp39T",
        "guyg": "oP6yT1xHaE9qD4KsLi82M",
        "ojigh": "mQ3wN8sRcK5tY2VhUe74Z",
        "igug": "Za1sX9qWe3rT7yUiPl56K",
        "fyv": "Hv4kM2nBq8sR1tJcLz93F",
        "vy": "Qs7nF3bLk1pV8xTdRm64G",
        "gyvg": "Nc5wZ1tQe9yH2rLaKs78D",
        "gjbjb": "Lf8pC6sWd3vX1qTuMz40S",
        "zqplk": "Tx9vK3dRm5nP2sLaQw71E",
        "nmxas": "Rj6mV4qTe8yN1bLcPw53C",
        "rtuwq": "Uw2nZ7sQa4tK9pLeMr86B",
        "bchdk": "Ky3pT5nWv7rQ1mLaZx68A",
        "czmop": "De9fR2sXq5tM1nLbVw84P",
        "kqvtd": "Gk1nP8rTe3yL6mQaZw59J",
        "prxnl": "Bn7qL4tWe2rP9mXsVd61H",
        "svyud": "Hp5mN2qTs8yR1lKaVw73U",
        "tjbqw": "Lm6tQ3nWp9rV2sXeYk45I",
        "wmzlk": "Vb8rP4tQe1mS7nKxZa62O",
        "ydnfc": "Cf2mH7vQp6tN9sLxRw83Y",
        "aejru": "Jq4nT6zWe5rM8vPaLs71X",
        "bpvhs": "Rd3pK9sTe2yN7mQwVb64Z",
        "cltqg": "Wu5sL2nQe8rT1yPaMx93C",
        "pqlmn": "Ep7mV1qRs6tN4xLbYz82D",
        "vtycx": "Ha9tQ2mWe5rP8nXsLv61F",
        "wzufr": "Nk8rS3pTe1yM6wQvZa75G",
        "kdjsh": "Zt4mP7nQw3rS6xLeVy82H",
        "qwert": "Oy6nR5mTe2pL9qXsWa34J",
        "yuiop": "Px1vK8tQe4mN7sLaRw53K",
        "asdfg": "Sm2nL9qTe5rV8pXaZw61M",
        "hklop": "Yd3pM6tQw7nR2sLeVk84N",
    }

    # Static fallback catalog copied from live GET /domains/public (2026-08-09).
    # The client still refreshes/merges through GET /domains/public when possible.
    DOMAIN_ID_TO_DOMAIN: dict[str, str] = {
        "0ae03374-2feb-4860-be68-43d4fd1771c3": "nilufa.kuromee.com",
        "52fda2ac-632c-4e5e-92c7-29f9192ef3cc": "kuromee.com",
        "36db087e-a581-4e86-aad3-e048a1234494": "hello.kuromee.store",
        "4d6afd1d-0031-47ae-a69c-469eb87d2577": "kuromee.store",
        "89646534-838a-499a-a307-888ba8ac2bce": "starlight.store",
        "6001c00a-f14c-4aeb-9a00-bd3f7120573a": "fan.starlight.store",
        "2e41ad18-4e7e-47de-b6b0-8e7acb541e6c": "theboys.cyou",
        "aa7d3b4d-ee6e-4a9a-a44c-bec175ce6357": "vought.theboys.cyou",
        "cc808ba8-91d7-42df-a381-071a0a056572": "bscse.okcx.edu.rs",
        "9f2c8af0-504c-4407-aa78-27a1eef3beb7": "bseee.okcx.edu.rs",
        "7575be28-0b9b-465c-92ba-6dce7b13d43d": "usa.priyo.edu.pl",
    }

    def __init__(
        self,
        *,
        token: str = "",
        domain_id: str = "",
        domain: str = "",
        email: str = "",
        temp_email_id: str = "",
        proxy: Optional[str] = None,
        proxies: Any = None,
        impersonate: Optional[str] = None,
        user_agent: str = "",
        language: str = "zh",
        timeout: int = 15,
        debug: bool = False,
    ) -> None:
        resolved_impersonate = (impersonate or os.getenv("BOOMLIFY_IMPERSONATE") or "chrome136").strip()
        super().__init__(
            timeout=timeout,
            proxy=proxy,
            proxies=proxies,
            impersonate=resolved_impersonate or None,
        )

        self._debug = bool(debug) or (os.getenv("BOOMLIFY_DEBUG") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        self._domain_catalog_loaded = False

        self._token = (token or os.getenv("BOOMLIFY_TOKEN") or "").strip()
        self._domain_id = (domain_id or os.getenv("BOOMLIFY_DOMAIN_ID") or "").strip()
        self._domain = (domain or os.getenv("BOOMLIFY_DOMAIN") or "").strip().lstrip("@")
        self._temp_email_id = (temp_email_id or os.getenv("BOOMLIFY_TEMP_EMAIL_ID") or "").strip()

        self._boomlify_preset_email = (email or os.getenv("BOOMLIFY_EMAIL") or "").strip()

        ua = (user_agent or "").strip() or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        )

        session_kwargs: dict[str, Any] = {
            "headers": {
                "User-Agent": ua,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "accept-language": language,
                "x-user-language": language,
                "origin": "https://boomlify.com",
                "referer": "https://boomlify.com/",
            },
            "timeout": int(self.timeout),
            "proxies": self.proxies,
        }
        if self.impersonate:
            session_kwargs["impersonate"] = self.impersonate

        self._client = requests.Session(**session_kwargs)
        if self._token:
            self._apply_authorization_header(self._token)

        if (not self._domain) and self._domain_id:
            self._domain = self.DOMAIN_ID_TO_DOMAIN.get(self._domain_id, "")
        if (not self._domain_id) and self._domain:
            self._domain_id = self._find_domain_id_from_static_catalog(self._domain)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
        super().close()

    def _mask(self, s: str, *, keep: int = 6) -> str:
        s = str(s or "")
        if not s:
            return ""
        if len(s) <= keep:
            return s
        return s[:keep] + "..."

    def _log(self, msg: str) -> None:
        if not self._debug:
            return
        try:
            print(f"[BoomlifyComClient] {msg}", file=sys.stderr)
        except Exception:
            pass

    def _apply_authorization_header(self, token: str) -> None:
        token = str(token or "").replace("Bearer ", "").strip()
        if token:
            self._client.headers["authorization"] = f"Bearer {token}"
        else:
            self._client.headers.pop("authorization", None)

    def _get_json(self, response: Any) -> Any:
        if not getattr(response, "content", None):
            return {}
        payload = response.json()
        key_id = response.headers.get("x-enc-key-id") or response.headers.get("X-Enc-Key-Id")
        return self._decrypt_payload(payload, key_id=key_id)

    def _decrypt_payload(self, payload: Any, *, key_id: str | None = None) -> Any:
        if not isinstance(payload, dict) or "encrypted" not in payload:
            return payload

        encrypted = str(payload.get("encrypted") or "").strip()
        if not encrypted:
            return payload

        key = self.TRANSPORT_KEY_RING.get(str(key_id or "").strip()) or self.DEFAULT_TRANSPORT_KEY
        try:
            encrypted_bytes = bytes.fromhex(encrypted)
        except ValueError:
            return payload

        key_bytes = key.encode("utf-8")
        decoded = "".join(
            chr(value ^ key_bytes[index % len(key_bytes)])
            for index, value in enumerate(encrypted_bytes)
        )

        try:
            return json.loads(decoded)
        except json.JSONDecodeError:
            return payload

    def _jwt_payload(self, token: str) -> dict[str, Any]:
        token = str(token or "").replace("Bearer ", "").strip()
        parts = token.split(".")
        if len(parts) != 3:
            return {}

        segment = parts[1]
        padding = "=" * (-len(segment) % 4)
        try:
            decoded = base64.urlsafe_b64decode(segment + padding)
            payload = json.loads(decoded.decode("utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _token_expired(self, token: str, *, leeway_seconds: int = 30) -> bool:
        payload = self._jwt_payload(token)
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return False
        return float(exp) <= (time.time() + leeway_seconds)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        retry_on_auth: bool = True,
        **kwargs: Any,
    ) -> Any:
        http_method = getattr(self._client, method.lower())
        response = http_method(f"{self.BASE_URL}{path}", **kwargs)

        if response.status_code in (401, 403) and retry_on_auth and path != "/guest/init":
            self._log(f"{method} {path} returned {response.status_code}; refreshing guest token")
            self._ensure_guest_token(force=True)
            response = http_method(f"{self.BASE_URL}{path}", **kwargs)

        if response.status_code >= 400:
            try:
                error_payload = self._get_json(response)
            except Exception:
                error_payload = None
            if error_payload:
                raise RuntimeError(
                    f"boomlify {method} {path} failed with {response.status_code}: "
                    f"{json.dumps(error_payload, ensure_ascii=False)}"
                )
            response.raise_for_status()
        return self._get_json(response)

    def _ensure_guest_token(self, *, force: bool = False) -> str:
        if (not force) and self._token and (not self._token_expired(self._token)):
            return self._token

        payload = self._request_json("POST", "/guest/init", retry_on_auth=False, json={})
        token = str(payload.get("token") or "").strip() if isinstance(payload, dict) else ""
        if not token:
            raise RuntimeError("boomlify guest/init did not return a token")

        self._token = token
        self._apply_authorization_header(token)
        self._log(f"initialized guest token: {self._mask(token)}")
        return token

    def _find_domain_id_from_static_catalog(self, domain: str) -> str:
        target = str(domain or "").strip().lstrip("@").lower()
        for domain_id, catalog_domain in self.DOMAIN_ID_TO_DOMAIN.items():
            if catalog_domain.lower() == target:
                return domain_id
        return ""

    def _load_domain_catalog(self) -> None:
        if self._domain_catalog_loaded:
            return

        try:
            payload = self._request_json("GET", "/domains/public", retry_on_auth=False)
        except Exception as exc:
            self._log(f"GET /domains/public failed; using static catalog: {exc}")
            self._domain_catalog_loaded = True
            return

        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                domain_id = str(item.get("id") or "").strip()
                domain = str(item.get("domain") or "").strip().lstrip("@")
                if domain_id and domain:
                    self.DOMAIN_ID_TO_DOMAIN[domain_id] = domain

        self._domain_catalog_loaded = True

    def _ensure_domain_choice(self) -> None:
        self._load_domain_catalog()

        if self._domain_id and not self._domain:
            self._domain = self.DOMAIN_ID_TO_DOMAIN.get(self._domain_id, "")
        if self._domain and not self._domain_id:
            self._domain_id = self._find_domain_id_from_static_catalog(self._domain)

        if self._domain and self._domain_id:
            return

        if self.DOMAIN_ID_TO_DOMAIN:
            domain_items = list(self.DOMAIN_ID_TO_DOMAIN.items())
            self._domain_id, self._domain = secrets.choice(domain_items)
            return

        raise RuntimeError("boomlify has no usable public domains")

    @property
    def temp_email_id(self) -> str:
        if not self._temp_email_id:
            raise RuntimeError("boomlify missing temp_email_id; call get_email() first")
        return self._temp_email_id

    def _generate_email(self) -> str:
        if not self._domain:
            raise RuntimeError("boomlify missing domain; cannot generate mailbox address")
        local = "u" + secrets.token_hex(6)
        return f"{local}@{self._domain.lstrip('@')}"

    def _try_parse_create_payload(self, payload: Any) -> tuple[str, str]:
        if not isinstance(payload, dict):
            return "", ""

        data = payload.get("data")
        if isinstance(data, dict):
            payload = data

        email = str(
            payload.get("email")
            or payload.get("address")
            or payload.get("recipient")
            or ""
        ).strip()
        temp_id = str(
            payload.get("temp_email_id")
            or payload.get("tempEmailId")
            or payload.get("tempEmailID")
            or payload.get("id")
            or ""
        ).strip()
        return email, temp_id

    def _fetch_mailbox_list(self, *, page: int = 1, limit: int = 10) -> list[dict[str, Any]]:
        self._ensure_guest_token()
        payload = self._request_json(
            "GET",
            "/emails",
            params={"page": max(1, int(page)), "limit": max(1, int(limit))},
        )

        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = (
                payload.get("data")
                or payload.get("items")
                or payload.get("hydra:member")
                or payload.get("emails")
                or []
            )
        else:
            items = []

        out: list[dict[str, Any]] = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    out.append(item)
        return out

    def get_email(self) -> str:
        return self._get_or_create_email()

    def _prepare_inbox(self) -> None:
        if self._boomlify_preset_email and self._temp_email_id:
            self._email = self._boomlify_preset_email
            return

        self._ensure_guest_token()
        self._ensure_domain_choice()

        email_addr = self._requested_email or self._boomlify_preset_email or self._generate_email()
        self._log(
            "create mailbox: "
            f"email={email_addr!r} domain_id={self._domain_id!r} token={self._mask(self._token)}"
        )

        payload = self._request_json(
            "POST",
            "/emails/create",
            json={"email": email_addr, "domainId": self._domain_id},
        )

        parsed_email, parsed_temp_id = self._try_parse_create_payload(payload)
        self._email = parsed_email or email_addr
        if parsed_temp_id:
            self._temp_email_id = parsed_temp_id
            return

        boxes = self._fetch_mailbox_list(page=1, limit=10)
        for item in boxes:
            item_email = str(item.get("email") or item.get("address") or "").strip()
            if item_email and item_email.lower() == self._email.lower():
                item_id = str(
                    item.get("id")
                    or item.get("temp_email_id")
                    or item.get("tempEmailId")
                    or ""
                ).strip()
                if item_id:
                    self._temp_email_id = item_id
                    self._log(
                        "resolved temp_email_id via list: "
                        f"email={self._email!r} temp_email_id={self._temp_email_id}"
                    )
                    return

        sample = [
            {"id": item.get("id"), "email": item.get("email"), "domain_id": item.get("domain_id")}
            for item in boxes[:5]
        ]
        raise RuntimeError(
            "boomlify create response could not be resolved to a temp_email_id; "
            f"target_email={self._email!r}; list_sample={json.dumps(sample, ensure_ascii=False)}"
        )

    def _fetch_message_list(self) -> list[MessageSummary]:
        _ = self.get_email()

        data = self._request_json(
            "GET",
            f"/emails/{self.temp_email_id}/received",
            params={"page": 1, "limit": 10, "skipCache": "false"},
        )
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []

        out: list[MessageSummary] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            msg_id = str(item.get("id") or "").strip()
            subject = str(item.get("subject") or "")
            from_email = str(item.get("from_email") or "")
            from_name = str(item.get("from_name") or "")
            from_addr = from_email or from_name
            body_text = str(item.get("body_text") or "")
            body_html = str(item.get("body_html") or "")
            preview = (body_text or body_html)[:200]

            out.append(
                {
                    "id": msg_id,
                    "from": from_addr,
                    "subject": subject,
                    "preview": preview,
                    "_raw": item,
                }
            )
        return out

    def _fetch_message_detail(self, msg: MessageSummary) -> str:
        raw = msg.get("_raw")
        if not isinstance(raw, dict):
            return ""

        subject = str(raw.get("subject") or "")
        text = str(raw.get("body_text") or "")
        html = str(raw.get("body_html") or "")
        return "\n".join(part for part in [subject, text, html] if part).strip()

    # Use BaseEmailClient.wait_for_target().
