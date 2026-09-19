from __future__ import annotations

import json
import random
import re
import string
from typing import Any, Optional, cast
from urllib.parse import quote

from curl_cffi import requests

from .base import BaseEmailClient, MessageSummary


class TinyhostShopClient(BaseEmailClient):
    """Tinyhost (tinyhost.shop) receive-only temporary inbox client.

    Chrome MCP/network confirmed endpoints (also documented on /api-docs.html):
    - GET /api/random-domains/?page=1&limit=20
    - GET /api/check-mx/{domain}
    - GET /api/email/{domain}/{user}/?page=1&limit=20
    - GET /api/email/{domain}/{user}/{email_id}
    - DELETE /api/email/{domain}/{user}/{email_id}
    """

    BASE_URL = "https://tinyhost.shop"

    def __init__(
        self,
        *,
        email_address: Optional[str] = None,
        user: Optional[str] = None,
        domain: Optional[str] = None,
        timeout: int = 20,
        proxy: Optional[str] = None,
        proxies: Any = None,
        impersonate: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        super().__init__()

        self.timeout = max(1, int(timeout))
        self.proxies = BaseEmailClient.normalize_proxies(proxy=proxy, proxies=proxies)

        headers: dict[str, str] = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": user_agent
            or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
        }

        session_kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "headers": headers,
        }
        if impersonate:
            session_kwargs["impersonate"] = impersonate

        self._client = requests.Session(**session_kwargs)

        req_user: Optional[str] = (user or "").strip() or None
        req_domain: Optional[str] = (domain or "").strip() or None

        if email_address and str(email_address).strip():
            parsed_user, parsed_domain = self._split_email(str(email_address).strip())
            req_user, req_domain = parsed_user, parsed_domain

        self._requested_user = req_user
        self._requested_domain = req_domain

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def _get(self, url: str, **kwargs: Any) -> Any:
        """GET with transparent proxy→direct fallback.

        Default proxy (127.0.0.1:7890) often returns
        ``CONNECT tunnel failed, response 502`` for tinyhost.shop.
        In that case retry once without proxies instead of failing
        the whole registration.
        """
        kwargs.setdefault("proxies", self.proxies)
        try:
            return self._client.get(url, **kwargs)
        except Exception as e:
            if self.proxies and self._is_proxy_tunnel_error(e):
                kwargs["proxies"] = {}
                return self._client.get(url, **kwargs)
            raise

    def _delete(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("proxies", self.proxies)
        try:
            return self._client.delete(url, **kwargs)
        except Exception as e:
            if self.proxies and self._is_proxy_tunnel_error(e):
                kwargs["proxies"] = {}
                return self._client.delete(url, **kwargs)
            raise

    @staticmethod
    def _is_proxy_tunnel_error(e: Exception) -> bool:
        msg = f"{type(e).__name__}: {e}"
        keys = ("CONNECT tunnel failed", "502", "Proxy", "ConnectionError", "Failed to perform")
        return any(k in msg for k in keys)

    def get_domains(self, *, page: int = 1, limit: int = 20) -> list[str]:
        page_i = max(1, int(page))
        limit_i = max(1, min(100, int(limit)))

        r = self._get(
            f"{self.BASE_URL}/api/random-domains/",
            params={"page": page_i, "limit": limit_i},
        )
        r.raise_for_status()
        data = self._json_or_text(r)
        if isinstance(data, dict) and isinstance(data.get("domains"), list):
            return [str(d).strip() for d in data["domains"] if str(d).strip()]
        if isinstance(data, list):
            return [str(d).strip() for d in data if str(d).strip()]
        return []

    def check_mx(self, *, domain: str) -> dict[str, Any]:
        d = str(domain or "").strip()
        if not d:
            raise ValueError("domain required")

        r = self._get(
            f"{self.BASE_URL}/api/check-mx/{self._q(d)}",
        )
        r.raise_for_status()
        data = self._json_or_text(r)
        return cast(dict[str, Any], data) if isinstance(data, dict) else {"raw": data}

    def create_email(self, *, email: Optional[str] = None) -> str:
        if email and str(email).strip():
            u, d = self._split_email(str(email).strip())
            self._email = f"{u}@{d}"
            return self._email

        domain = self._requested_domain
        if not domain:
            domains = self.get_domains(limit=20, page=1)
            if not domains:
                raise RuntimeError("tinyhost: failed to fetch domains")
            domain = domains[0]

        user = self._requested_user or self._random_user()
        self._email = f"{user}@{domain}"
        return self._email

    def refresh_email(self) -> str:
        self._email = None
        self._requested_user = None
        return self.get_email()

    def get_email(self) -> str:
        if self._email:
            return self._email
        return self.create_email(email=None)

    def list_messages(
        self,
        *,
        page: int = 1,
        limit: int = 20,
        max_pages: Optional[int] = None,
    ) -> list[MessageSummary]:
        _ = self.get_email()
        user, domain = self._split_email(self.email)

        page_i = max(1, int(page))
        limit_i = max(1, min(100, int(limit)))
        max_pages_i = None if max_pages is None else max(1, int(max_pages))

        out: list[MessageSummary] = []
        cur_page = page_i
        fetched_pages = 0
        while True:
            data = self._get_inbox_page(domain=domain, user=user, page=cur_page, limit=limit_i)
            emails = data.get("emails") if isinstance(data, dict) else None
            if not isinstance(emails, list):
                break

            for item in emails:
                if not isinstance(item, dict):
                    continue

                mid = self._pick_first_str(item, ["id", "email_id", "emailId", "mid", "uid", "uuid"])
                subject = self._pick_first_str(item, ["subject", "title"])
                sender = self._pick_first_str(item, ["sender", "from", "from_address", "fromAddress"])
                date = item.get("date") or item.get("created_at") or item.get("createdAt")

                preview = self._pick_first_str(item, ["preview", "snippet", "intro"])
                if not preview:
                    body = self._pick_first_str(item, ["body", "text", "content"])
                    preview = (body or "").strip()[:200]

                summary: MessageSummary = {
                    "id": mid,
                    "subject": subject,
                    "from": sender,
                    "preview": preview,
                    "date": date,
                    "received_at_ts": BaseEmailClient.parse_received_at_ts(date),
                    "_raw": item,
                }
                out.append(summary)

            fetched_pages += 1
            has_more = bool(data.get("has_more")) if isinstance(data, dict) else False
            if not has_more:
                break
            cur_page += 1
            if max_pages_i is not None and fetched_pages >= max_pages_i:
                break

        return out

    def get_message(self, *, mid: str) -> dict[str, Any]:
        _ = self.get_email()
        msg_id = str(mid or "").strip()
        if not msg_id:
            raise ValueError("mid required")

        user, domain = self._split_email(self.email)
        r = self._get(
            f"{self.BASE_URL}/api/email/{self._q(domain)}/{self._q(user)}/{self._q(msg_id)}",
        )
        r.raise_for_status()
        data = self._json_or_text(r)
        return cast(dict[str, Any], data) if isinstance(data, dict) else {"raw": data}

    def delete_message(self, *, mid: str) -> dict[str, Any]:
        _ = self.get_email()
        msg_id = str(mid or "").strip()
        if not msg_id:
            raise ValueError("mid required")

        user, domain = self._split_email(self.email)
        r = self._delete(
            f"{self.BASE_URL}/api/email/{self._q(domain)}/{self._q(user)}/{self._q(msg_id)}",
        )
        r.raise_for_status()
        data = self._json_or_text(r)
        return cast(dict[str, Any], data) if isinstance(data, dict) else {"raw": data}

    def _fetch_message_list(self) -> list[MessageSummary]:
        return self.list_messages(page=1, limit=20, max_pages=1)

    def _fetch_message_detail(self, msg: MessageSummary) -> str:
        raw = msg.get("_raw") if isinstance(msg.get("_raw"), dict) else {}
        if not isinstance(raw, dict):
            raw = {}

        html_body = raw.get("html_body") or raw.get("html") or raw.get("body_html")
        if isinstance(html_body, str) and html_body.strip():
            return html_body.strip()

        body = raw.get("body") or raw.get("text") or raw.get("content")
        if isinstance(body, str) and body.strip():
            return body.strip()

        msg_id = str(msg.get("id") or "").strip()
        if not msg_id:
            return ""

        detail = self.get_message(mid=msg_id)
        if not isinstance(detail, dict):
            return str(detail)

        html_detail = detail.get("html_body") or detail.get("html") or detail.get("body_html")
        if isinstance(html_detail, str) and html_detail.strip():
            return html_detail.strip()

        text_detail = detail.get("body") or detail.get("text") or detail.get("content")
        if isinstance(text_detail, str) and text_detail.strip():
            return text_detail.strip()

        try:
            return json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return str(detail)

    @staticmethod
    def _q(seg: str) -> str:
        return quote(str(seg), safe="")

    @staticmethod
    def _json_or_text(resp: Any) -> Any:
        try:
            return resp.json()
        except Exception:
            return resp.text

    @staticmethod
    def _pick_first_str(obj: dict[str, Any], keys: list[str]) -> str:
        for k in keys:
            v = obj.get(k)
            if isinstance(v, (int, float)) and k in {"id", "email_id", "emailId"}:
                return str(int(v))
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    @staticmethod
    def _split_email(email: str) -> tuple[str, str]:
        e = str(email or "").strip()
        if not e or "@" not in e:
            raise ValueError(f"invalid email: {email!r}")
        user, domain = e.split("@", 1)
        user = user.strip()
        domain = domain.strip()
        if not user or not domain:
            raise ValueError(f"invalid email: {email!r}")
        return user, domain

    def _get_inbox_page(self, *, domain: str, user: str, page: int, limit: int) -> dict[str, Any]:
        r = self._get(
            f"{self.BASE_URL}/api/email/{self._q(domain)}/{self._q(user)}/",
            params={"page": int(page), "limit": int(limit)},
        )
        r.raise_for_status()
        data = self._json_or_text(r)
        return cast(dict[str, Any], data) if isinstance(data, dict) else {}

    @staticmethod
    def _random_user(*, length: int = 14) -> str:
        length_i = max(6, min(32, int(length)))
        alphabet = string.ascii_lowercase + string.digits
        user = "".join(random.choice(alphabet) for _ in range(length_i))
        user = re.sub(r"[^a-z0-9]+", "", user.lower())
        return user or "testuser"
