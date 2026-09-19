from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from typing import Any, Literal, Optional

from curl_cffi import requests

from .base import BaseEmailClient, MessageSummary


def _s(val: Any) -> str:
    """Extract string and strip; None/empty -> ''."""
    return str(val or "").strip()


@dataclass(frozen=True)
class FreecustomEmailInboxInfo:
    address: str
    localpart: str
    domain: str
    encrypted_mailbox: Optional[str] = None


class FreecustomEmailClient(BaseEmailClient):
    """freecustom.email anonymous public mailbox client.

    2026-08 Chrome notes:
    - Core REST still works: POST /api/auth, GET /api/domains, GET /api/public-mailbox.
    - UI also mints realtime tickets via POST /api/ws-ticket (not required for polling).
    - Many legacy dit* free domains are returned with expires_in_days < 0; get_domains()
      filters those out and FALLBACK/PREFERRED prefer currently-active free domains.
    """

    ORIGIN = "https://www.freecustom.email"
    API_BASE = ORIGIN
    SITE_URL = f"{ORIGIN}/en"
    # Live free domains observed 2026-08-09 (many dit* domains are expired_in_days < 0).
    FALLBACK_DOMAINS = (
        "haloforge.online",
        "haloforge.info",
        "nimbusreach.info",
        "lumenbay.info",
        "echoharbor.in",
        "sqlcompiler.info",
        "addmy.space",
        "attachmy.site",
    )
    PREFERRED_DOMAINS = (
        "haloforge.online",
        "nimbusreach.info",
        "lumenbay.info",
        "haloforge.info",
        "echoharbor.in",
    )
    _LOCALPART_RE = re.compile(r"[^a-z0-9._-]+")
    _LOCALPART_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

    supports_random_address = True
    supports_custom_localpart = True
    supports_custom_domain = True
    supports_custom_address = True

    def __init__(
        self,
        *,
        email: Optional[str] = None,
        localpart: Optional[str] = None,
        domain: Optional[str] = None,
        random_domain: bool = True,
        preferred_domains: Optional[list[str] | tuple[str, ...]] = None,
        timeout: int = 20,
        proxy: Optional[str] = None,
        proxies: Any = None,
        impersonate: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        super().__init__(
            timeout=timeout,
            proxy=proxy,
            proxies=proxies,
            impersonate=impersonate,
            email=email,
            localpart=localpart,
            domain=domain,
        )

        session_kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.impersonate:
            session_kwargs["impersonate"] = self.impersonate
        self._client = requests.Session(
            timeout=self.timeout,
            headers={
                "Accept": "*/*",
                "Origin": self.ORIGIN,
                "Referer": self.SITE_URL,
                "User-Agent": user_agent or (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                ),
                "x-fce-client": "web-client",
            },
            impersonate=impersonate or "chrome124",
        )
        self._auth_token: Optional[str] = None
        self._domains_cache: Optional[tuple[str, ...]] = None
        self._random_domain = random_domain
        self.inbox_info: Optional[FreecustomEmailInboxInfo] = None

        preferred = preferred_domains or self.PREFERRED_DOMAINS
        self.preferred_domains = tuple(d for d in (_s(x) for x in preferred) if d)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
        super().close()

    def get_email(self) -> str:
        return self._get_or_create_email()

    def _prepare_inbox(self) -> None:
        if self._requested_email:
            lp, dom = self._split_email(self._requested_email)
            self._email = self._requested_email
            payload = self._get_mailbox(self._requested_email)
            self.inbox_info = FreecustomEmailInboxInfo(
                address=self._requested_email, localpart=lp, domain=dom,
                encrypted_mailbox=_s(payload.get("encryptedMailbox")) or None,
            )
            return

        localpart = self._requested_localpart or self._generate_localpart()
        dom = self._select_domain(self._requested_domain)
        address = f"{localpart}@{dom}"
        payload = self._get_mailbox(address)
        self._email = address
        self.inbox_info = FreecustomEmailInboxInfo(
            address=address, localpart=localpart, domain=dom,
            encrypted_mailbox=_s(payload.get("encryptedMailbox")) or None,
        )

    def get_domains(self) -> list[str]:
        if self._domains_cache is not None:
            return list(self._domains_cache)
        payload = self._api("GET", "/api/domains")
        domains: list[str] = []
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            domain = _s(item.get("domain"))
            if not domain:
                continue
            # Drop free domains the API itself marks as expired.
            days = item.get("expires_in_days")
            try:
                if days is not None and int(days) < 0:
                    continue
            except Exception:
                pass
            domains.append(domain)
        self._domains_cache = tuple(domains or self.FALLBACK_DOMAINS)
        return list(self._domains_cache)

    def list_messages(self) -> list[MessageSummary]:
        items = self._get_mailbox(self.get_email()).get("data")
        if not isinstance(items, list):
            return []
        msgs = [self._to_summary(it) for it in items if isinstance(it, dict)]
        msgs.reverse()  # API returns newest-first; normalize to oldest->newest
        return msgs

    def get_message(self, *, mid: str) -> dict[str, Any]:
        if not _s(mid):
            raise ValueError("mid required")
        payload = self._api("GET", "/api/public-mailbox",
                            params={"fullMailboxId": self.get_email(), "messageId": mid},
                            needs_auth=True)
        if not payload.get("success"):
            raise RuntimeError(f"freecustom message detail unexpected: {payload!r}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"freecustom message detail missing data: {payload!r}")
        return data

    def _fetch_message_list(self) -> list[MessageSummary]:
        return self.list_messages()

    def _fetch_message_detail(self, msg: MessageSummary) -> str:
        mid = _s(msg.get("id"))
        if not mid:
            raise KeyError("freecustom message missing id")
        raw = self.get_message(mid=mid)
        for key in ("text", "html", "verificationLink", "otp"):
            val = _s(raw.get(key))
            if val:
                return val
        return _s(msg.get("preview"))

    def _get_mailbox(self, address: str) -> dict[str, Any]:
        payload = self._api("GET", "/api/public-mailbox",
                            params={"fullMailboxId": address}, needs_auth=True)
        if not payload.get("success"):
            raise RuntimeError(f"freecustom mailbox list unexpected: {payload!r}")
        enc = _s(payload.get("encryptedMailbox")) or None
        if enc and self.inbox_info and self.inbox_info.address == address:
            self.inbox_info = FreecustomEmailInboxInfo(
                address=self.inbox_info.address, localpart=self.inbox_info.localpart,
                domain=self.inbox_info.domain, encrypted_mailbox=enc,
            )
        return payload

    def _ensure_auth(self) -> str:
        if self._auth_token:
            return self._auth_token
        payload = self._api("POST", "/api/auth")
        token = _s(payload.get("token")) if isinstance(payload, dict) else ""
        if not token:
            raise RuntimeError(f"freecustom auth missing token: {payload!r}")
        self._auth_token = token
        return token

    HttpMethod = Literal["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "TRACE"]

    def _api(
        self, method: HttpMethod, path: str, *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        needs_auth: bool = False,
        _retry: bool = True,
    ) -> Any:
        headers: dict[str, str] = {}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if needs_auth:
            headers["Authorization"] = f"Bearer {self._ensure_auth()}"

        url = f"{self.API_BASE}{path}" if path.startswith("/") else f"{self.API_BASE}/{path}"
        resp = self._client.request(method, url, params=params, json=json_body,
                                    headers=headers or None, proxies=self.proxies)

        if resp.status_code == 401 and needs_auth and _retry:
            self._auth_token = None
            return self._api(method, path, params=params, json_body=json_body,
                             needs_auth=needs_auth, _retry=False)
        resp.raise_for_status()
        return self._parse_json(resp)

    @staticmethod
    def _parse_json(resp: Any) -> Any:
        try:
            return resp.json()
        except Exception:
            text = str(resp.text or "").lstrip("﻿")
            try:
                return json.loads(text) if text else text
            except Exception:
                return text

    def _select_domain(self, requested: Optional[str]) -> str:
        domains = self.get_domains()
        if requested:
            if domains and requested not in domains:
                raise ValueError(f"freecustom domain not available: {requested!r}")
            return requested
        if self._random_domain and domains:
            return secrets.choice(domains)
        for d in self.preferred_domains:
            if d in domains:
                return d
        return domains[0] if domains else self.FALLBACK_DOMAINS[0]

    def _to_summary(self, item: dict[str, Any]) -> MessageSummary:
        subject = _s(item.get("subject"))
        otp = _s(item.get("otp")) or None
        vlink = _s(item.get("verificationLink")) or None
        preview = subject or (otp or "") or (vlink or "")
        return {
            "id": _s(item.get("id")),
            "from": _s(item.get("from")),
            "subject": subject,
            "preview": preview[:200],
            "date": item.get("date"),
            "received_at_ts": BaseEmailClient.parse_received_at_ts(item.get("date")),
            "to": _s(item.get("to")),
            "otp": otp,
            "verification_link": vlink,
            "has_attachment": bool(item.get("hasAttachment")),
            "was_attachment_stripped": bool(item.get("wasAttachmentStripped")),
            "_raw": item,
        }

    @classmethod
    def _generate_localpart(cls) -> str:
        return "".join(secrets.choice(cls._LOCALPART_ALPHABET) for _ in range(12))

    @classmethod
    def _normalize_localpart(cls, value: str) -> str:
        cleaned = re.sub(r"-{2,}", "-", cls._LOCALPART_RE.sub("-", value.lower())).strip("-.")
        if not cleaned or not re.search(r"[a-z0-9]", cleaned):
            raise ValueError(f"invalid freecustom localpart: {value!r}")
        return cleaned[:64]

    @staticmethod
    def _split_email(email: str) -> tuple[str, str]:
        val = _s(email).lower()
        if not val or "@" not in val:
            raise ValueError(f"invalid email: {email!r}")
        lp, dom = val.rsplit("@", 1)
        if not lp.strip() or not dom.strip():
            raise ValueError(f"invalid email: {email!r}")
        return lp.strip(), dom.strip()
