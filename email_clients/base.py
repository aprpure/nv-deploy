from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import string
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional, Required, TypedDict


MessageSummary = TypedDict(
    "MessageSummary",
    {
        "id": Required[str],
        "received_at_ts": float | None,
        "from": str,
        "subject": str,
        "preview": str,
        "_raw": Any,
    },
    total=False,
)


def _normalize_epoch_seconds(value: float) -> float:
    """基于数量级自动识别 秒/毫秒/微秒/纳秒，归一到秒。"""
    abs_value = abs(value)
    if abs_value >= 1e17:
        return value / 1_000_000_000.0
    if abs_value >= 1e14:
        return value / 1_000_000.0
    if abs_value >= 1e11:
        return value / 1_000.0
    return value


class BaseEmailClient(ABC):
    """临时邮箱 / 收件箱客户端抽象基类（无中心 hub）。

    -----------------------------------------------------------------------
    对外主 API
    -----------------------------------------------------------------------
    - ``get_email()``：**幂等读**当前地址。已绑定则返回；否则内部触发一次
      无参 ``create_email()``（随机或已有 intent）。
    - ``create_email(...)``：**写/开箱**。随机、指定域名、指定完整地址都走这里
      （参数形态不同，不是两套 API）。已有号时再次调用 = 换绑。
    - ``refresh_email()``：清空 bound + intent 后随机再 ``create_email()``。
    - ``wait_for_target(...)``：轮询摘要 → 筛选 → 拉正文 → 提取，直到成功或超时。
    - ``close()``：释放资源（默认关掉 base 自建的 HTTP session）。

    -----------------------------------------------------------------------
    get_email vs create_email
    -----------------------------------------------------------------------
    ============  ==============================  ===========================
    维度          get_email()                     create_email(...)
    ============  ==============================  ===========================
    含义          拿到**当前**会话邮箱             **创建/绑定**一个收件箱
    参数          无参                            email / localpart / domain
    副作用        仅未绑定时触发一次 create       按意图开箱；可换绑
    幂等          是                              否
    典型时机      发信前、wait_for_target 开头    明确要新号/指定域名或账号
    ============  ==============================  ===========================

    构造参数 ``email`` / ``localpart`` / ``domain`` 只存 **intent**，不发 HTTP；
    第一次 ``get_email()`` 或显式 ``create_email()`` 才开箱。

    -----------------------------------------------------------------------
    子类需要实现
    -----------------------------------------------------------------------
    - ``get_email()`` — 可一行 ``return self._get_or_create_email()``（新代码推荐）
    - ``_fetch_message_list()`` / ``_fetch_message_detail(msg)``
    - 新代码推荐实现 ``_prepare_inbox()``，由 base 的 ``create_email`` 调用；
      或整段 override ``create_email``。

    -----------------------------------------------------------------------
    能力标记（类属性，子类按真实能力覆盖）
    -----------------------------------------------------------------------
    - ``supports_random_address``（默认 True）
    - ``supports_custom_localpart`` / ``supports_custom_domain`` /
      ``supports_custom_address``（默认 False）
    走 base intent / ``create_email`` 时不支持则 **ValueError**，禁止静默改随机。
    """

    supports_random_address: bool = True
    supports_custom_localpart: bool = False
    supports_custom_domain: bool = False
    supports_custom_address: bool = False

    DEFAULT_LOCALPART_ALPHABET = string.ascii_lowercase + string.digits

    # ------------------------------------------------------------------
    # 静态工具：代理 / 时间 / 地址 / 消息
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_proxies(*, proxy: Optional[str] = None, proxies: Any = None) -> Any:
        """统一代理参数。

        - 优先使用显式传入的 proxies（requests 风格字典或 curl_cffi 支持的格式）
        - 否则将 proxy 字符串转换为 {"http": proxy, "https": proxy}
        """
        if proxies is not None:
            return proxies
        if proxy:
            return {"http": proxy, "https": proxy}
        return None

    @staticmethod
    def parse_received_at_ts(value: Any) -> Optional[float]:
        """把 provider 拿到的各种时间表示统一为 Unix epoch 秒（float）。

        接受的输入：
        - int / float：自动识别 秒/毫秒/微秒/纳秒（基于数量级）
        - 数字字符串：同上
        - ISO 8601 字符串：含或不含 Z/时区偏移
        - RFC 2822 邮件头日期字符串
        - None / 空字符串 / 无法解析 / NaN → 返回 None

        **职责边界（硬约束）：**
        - 只做 解析 → epoch 秒。不做格式化、不做比较、不做时长解析
        - provider 特异格式（"2 min ago"、某服务的本地化字符串等）留在
          该 provider 文件里，不污染这里
        - 如需加新分支：仅当 ≥2 个 provider 共享该格式时考虑；否则保持单独解析
        """

        if value is None:
            return None

        if isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric):
                return None
            return _normalize_epoch_seconds(numeric)

        text = str(value).strip()
        if not text:
            return None

        if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
            try:
                numeric = float(text)
            except ValueError:
                return None
            if not math.isfinite(numeric):
                return None
            return _normalize_epoch_seconds(numeric)

        iso_value = text
        if iso_value.endswith("Z"):
            iso_value = f"{iso_value[:-1]}+00:00"

        try:
            dt = datetime.fromisoformat(iso_value)
        except ValueError:
            try:
                dt = parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError, OverflowError):
                return None

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())

    @staticmethod
    def split_email(address: str) -> tuple[str, str]:
        """拆成 (localpart, domain)。无 ``@`` 或任一段为空则 ValueError。"""
        text = str(address or "").strip()
        if "@" not in text:
            raise ValueError(f"invalid email address (missing @): {address!r}")
        local, _, domain = text.partition("@")
        local = local.strip()
        domain = domain.strip()
        if not local or not domain:
            raise ValueError(f"invalid email address: {address!r}")
        return local, domain

    @staticmethod
    def join_email(localpart: str, domain: str) -> str:
        local = BaseEmailClient.normalize_localpart(localpart)
        dom = BaseEmailClient.normalize_domain(domain)
        if not local or not dom:
            raise ValueError("localpart and domain are required")
        return f"{local}@{dom}"

    @staticmethod
    def normalize_localpart(value: str, *, strict: bool = False) -> str:
        """规范化 localpart：默认只 strip。

        ``strict=True`` 时仅保留 ``[a-z0-9._-]``（小写），剥掉其它字符。
        """
        text = str(value or "").strip()
        if not strict:
            return text
        text = text.lower()
        return re.sub(r"[^a-z0-9._-]+", "", text)

    @staticmethod
    def normalize_domain(value: str) -> str:
        """strip、去掉前导 ``@``、lower。"""
        text = str(value or "").strip()
        if text.startswith("@"):
            text = text[1:]
        return text.strip().lower()

    @classmethod
    def random_localpart(
        cls,
        length: int = 10,
        *,
        prefix: str = "",
        alphabet: str | None = None,
    ) -> str:
        """用 ``secrets`` 生成随机 localpart。"""
        n = max(1, int(length))
        chars = alphabet if alphabet is not None else cls.DEFAULT_LOCALPART_ALPHABET
        if not chars:
            raise ValueError("alphabet must be non-empty")
        body = "".join(secrets.choice(chars) for _ in range(n))
        return f"{prefix}{body}"

    @staticmethod
    def resolve_address_intent(
        *,
        email: str | None = None,
        localpart: str | None = None,
        domain: str | None = None,
    ) -> dict[str, str | None]:
        """纯函数：合并地址意图。

        优先级：完整 ``email`` 优先于 local+domain。
        若同时给了 email 与 local/domain 且不一致 → ValueError。

        返回 ``{"email", "localpart", "domain"}``（已规范化；未指定为 None）。
        """
        raw_email = str(email or "").strip() or None
        raw_local = (
            BaseEmailClient.normalize_localpart(localpart) if localpart is not None else None
        )
        raw_local = raw_local or None
        raw_domain = (
            BaseEmailClient.normalize_domain(domain) if domain is not None else None
        )
        raw_domain = raw_domain or None

        if raw_email:
            split_local, split_domain = BaseEmailClient.split_email(raw_email)
            split_local = BaseEmailClient.normalize_localpart(split_local)
            split_domain = BaseEmailClient.normalize_domain(split_domain)
            if raw_local and raw_local != split_local:
                raise ValueError(
                    f"conflicting address intent: email localpart {split_local!r} "
                    f"!= localpart {raw_local!r}"
                )
            if raw_domain and raw_domain != split_domain:
                raise ValueError(
                    f"conflicting address intent: email domain {split_domain!r} "
                    f"!= domain {raw_domain!r}"
                )
            return {
                "email": BaseEmailClient.join_email(split_local, split_domain),
                "localpart": split_local,
                "domain": split_domain,
            }

        if raw_local and raw_domain:
            return {
                "email": BaseEmailClient.join_email(raw_local, raw_domain),
                "localpart": raw_local,
                "domain": raw_domain,
            }

        return {
            "email": None,
            "localpart": raw_local,
            "domain": raw_domain,
        }

    @staticmethod
    def synthesize_message_id(**fields: Any) -> str:
        """稳定合成消息 id：``sha1(json.dumps(fields, sort_keys=True))``。"""
        payload = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def make_message_summary(
        *,
        id: str,
        from_addr: str | None = None,
        subject: str | None = None,
        preview: str | None = None,
        received_at: Any = None,
        received_at_ts: float | None = None,
        raw: Any = None,
        **extra: Any,
    ) -> MessageSummary:
        """统一构造 MessageSummary；``from_addr`` 写入键 ``"from"``。"""
        msg_id = str(id or "").strip()
        if not msg_id:
            raise ValueError("message id is required")

        if received_at_ts is None and received_at is not None:
            received_at_ts = BaseEmailClient.parse_received_at_ts(received_at)

        out: dict[str, Any] = {"id": msg_id}
        if from_addr is not None:
            out["from"] = str(from_addr)
        if subject is not None:
            out["subject"] = str(subject)
        if preview is not None:
            out["preview"] = str(preview)
        if received_at_ts is not None:
            out["received_at_ts"] = received_at_ts
        if raw is not None:
            out["_raw"] = raw
        for key, value in extra.items():
            if key == "id":
                continue
            out[key] = value
        return out  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # 构造 / 生命周期
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        proxy: Optional[str] = None,
        proxies: Any = None,
        timeout: float = 15.0,
        impersonate: Optional[str] = None,
        email: Optional[str] = None,
        localpart: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> None:
        self._email: Optional[str] = None
        self.timeout = max(1.0, float(timeout))
        self.proxies: Any = self.normalize_proxies(proxy=proxy, proxies=proxies)
        self.impersonate: Optional[str] = impersonate
        self._http: Any = None
        self._http_owned: bool = False

        self._requested_email: Optional[str] = None
        self._requested_localpart: Optional[str] = None
        self._requested_domain: Optional[str] = None

        if email is not None or localpart is not None or domain is not None:
            self._set_address_intent(email=email, localpart=localpart, domain=domain)

    @property
    def email(self) -> str:
        """当前邮箱地址（只读）。未创建前访问会抛异常。"""
        if not self._email:
            raise RuntimeError("尚未创建邮箱，请先调用 get_email() 或 create_email()")
        return self._email

    def _http_kwargs(self, **extra: Any) -> dict[str, Any]:
        """合并 proxies / timeout / impersonate，供 curl_cffi 请求使用。"""
        kwargs: dict[str, Any] = {
            "proxies": self.proxies,
            "timeout": self.timeout,
        }
        if self.impersonate:
            kwargs["impersonate"] = self.impersonate
        kwargs.update(extra)
        return kwargs

    def _create_http_session(
        self,
        *,
        headers: Optional[dict[str, str]] = None,
        **kwargs: Any,
    ) -> Any:
        """创建 curl_cffi Session，挂到 ``self._http``（owned，close 时释放）。"""
        from curl_cffi import requests as curl_requests

        session_kwargs = self._http_kwargs(**kwargs)
        if headers is not None:
            session_kwargs["headers"] = headers
        session = curl_requests.Session(**session_kwargs)
        self._http = session
        self._http_owned = True
        return session

    def close(self) -> None:
        """释放 base 自建的 HTTP session（若有）。子类应 ``super().close()``。"""
        if self._http_owned and self._http is not None:
            try:
                close_fn = getattr(self._http, "close", None)
                if callable(close_fn):
                    close_fn()
            except Exception:
                pass
            finally:
                self._http = None
                self._http_owned = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # ------------------------------------------------------------------
    # 地址 Intent / create / get / refresh
    # ------------------------------------------------------------------

    def _clear_address_intent(self) -> None:
        self._requested_email = None
        self._requested_localpart = None
        self._requested_domain = None

    def _has_address_intent(self) -> bool:
        return bool(
            self._requested_email
            or self._requested_localpart
            or self._requested_domain
        )

    def _set_address_intent(
        self,
        *,
        email: str | None = None,
        localpart: str | None = None,
        domain: str | None = None,
    ) -> None:
        """写入地址意图并做能力校验。已 bound 时清除 ``_email`` 以便换绑。"""
        resolved = self.resolve_address_intent(
            email=email, localpart=localpart, domain=domain
        )
        self._validate_address_intent(
            email=resolved["email"],
            localpart=resolved["localpart"],
            domain=resolved["domain"],
        )
        self._requested_email = resolved["email"]
        self._requested_localpart = resolved["localpart"]
        self._requested_domain = resolved["domain"]
        # 意图变更 → 旧 bound 失效
        self._email = None

    def _validate_address_intent(
        self,
        *,
        email: str | None,
        localpart: str | None,
        domain: str | None,
    ) -> None:
        """按 supports_* 校验；不支持则 ValueError（禁止静默降级）。"""
        cls_name = type(self).__name__

        if email:
            if self.supports_custom_address:
                return
            if (
                self.supports_custom_localpart
                and self.supports_custom_domain
                and localpart
                and domain
            ):
                return
            raise ValueError(
                f"{cls_name} does not support custom address "
                f"(supports_custom_address={self.supports_custom_address}); "
                f"got email={email!r}"
            )

        if localpart and domain:
            if self.supports_custom_address or (
                self.supports_custom_localpart and self.supports_custom_domain
            ):
                return
            raise ValueError(
                f"{cls_name} does not support custom localpart+domain "
                f"(supports_custom_address={self.supports_custom_address}, "
                f"supports_custom_localpart={self.supports_custom_localpart}, "
                f"supports_custom_domain={self.supports_custom_domain})"
            )

        if localpart and not domain:
            if self.supports_custom_localpart or self.supports_custom_address:
                return
            raise ValueError(
                f"{cls_name} does not support custom localpart "
                f"(supports_custom_localpart={self.supports_custom_localpart}); "
                f"got localpart={localpart!r}"
            )

        if domain and not localpart:
            if self.supports_custom_domain or self.supports_custom_address:
                return
            raise ValueError(
                f"{cls_name} does not support custom domain "
                f"(supports_custom_domain={self.supports_custom_domain}); "
                f"got domain={domain!r}"
            )

        # 全空 = 随机
        if not self.supports_random_address:
            raise ValueError(
                f"{cls_name} does not support random address "
                f"(supports_random_address=False); provide a custom address intent"
            )

    def _validate_current_intent_or_random(self) -> None:
        """create 无新参时：校验已有 intent，或确认允许随机。"""
        self._validate_address_intent(
            email=self._requested_email,
            localpart=self._requested_localpart,
            domain=self._requested_domain,
        )

    def _prepare_inbox(self) -> None:
        """按 ``_requested_*``（或随机）真正开箱，并设置 ``self._email``。

        新 provider 应 override 本方法；或整段 override ``create_email``。
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement _prepare_inbox() "
            f"or override create_email(); legacy clients should keep using get_email() only"
        )

    def create_email(
        self,
        *,
        email: str | None = None,
        localpart: str | None = None,
        domain: str | None = None,
    ) -> str:
        """创建/绑定收件箱并返回地址（**写操作，非幂等**）。

        意图形态（同一入口）：
        1. 全空 → 随机开箱（或沿用已有 intent）
        2. 仅 ``domain`` → 随机 local + 指定域名
        3. 仅 ``localpart`` → 指定 local + 默认/服务端域名
        4. ``email=`` 或 local+domain → 指定完整地址

        本次带参会先 ``_set_address_intent``；成功后 ``self._email`` 为实际地址。
        """
        if email is not None or localpart is not None or domain is not None:
            self._set_address_intent(email=email, localpart=localpart, domain=domain)
        else:
            self._validate_current_intent_or_random()
            # 换绑/重开：无参 create 也清掉旧 bound，强制走 _prepare_inbox
            self._email = None

        self._prepare_inbox()
        if not self._email:
            raise RuntimeError(
                f"{type(self).__name__}.create_email() did not bind self._email"
            )
        return self._email

    def _get_or_create_email(self) -> str:
        """供子类 ``get_email`` 复用：有 bound 则返回，否则 ``create_email()``。"""
        if self._email:
            return self._email
        return self.create_email()

    @abstractmethod
    def get_email(self) -> str:
        """幂等返回当前邮箱地址；未绑定时应创建（推荐 ``return self._get_or_create_email()``）。"""

    def refresh_email(self) -> str:
        """清空当前绑定与 intent，再随机 ``create_email()`` 换一个新号。"""
        self._email = None
        self._clear_address_intent()
        if not self.supports_random_address:
            raise ValueError(
                f"{type(self).__name__} does not support random address; "
                f"cannot refresh_email()"
            )
        return self.create_email()

    # ------------------------------------------------------------------
    # 收件箱抽象
    # ------------------------------------------------------------------

    @abstractmethod
    def _fetch_message_list(self) -> list[MessageSummary]:
        """拉取收件箱邮件摘要列表（应尽量便宜）。

        每个 msg **必须**包含 'id' 字段（稳定、唯一、跨轮次一致）。
        无原生 id 时请自行合成（例如 :meth:`synthesize_message_id`）。
        base 不做兜底，缺失会在 wait_for_target 遍历到该 msg 时抛 KeyError。

        推荐填写 'received_at_ts'（float，Unix 秒）用于排序，缺失则该 msg
        视为最旧。
        """

    @abstractmethod
    def _fetch_message_detail(self, msg: MessageSummary) -> str:
        """拉取某封邮件正文内容（昂贵操作）。"""

    # ------------------------------------------------------------------
    # wait_for_target
    # ------------------------------------------------------------------

    def wait_for_target(
        self,
        *,
        condition_func: Callable[[MessageSummary], bool],
        extract_func: Callable[[MessageSummary, str], Any],
        max_attempts: int = 10,
        delay: float = 3,
        newest_first: bool = True,
        max_duration: Optional[float] = None,
        min_received_at_ts: Optional[float] = None,
        ignore_extract_miss: bool = True,
    ) -> Any:
        """通用“轮询 + 提取”引擎：反复轮询收件箱，直到提取到结果或超时。

        流程：
        - 先 ``get_email()``（幂等；未绑定则内部 create）
        - 每轮 ``_fetch_message_list()`` → 按 ``received_at_ts`` 排序 → 遍历
        - ``condition_func(msg)`` 为 False → 永久 ignore 该 id
        - 拉正文 ``_fetch_message_detail`` → ``extract_func(msg, content)``
        - extract 返回 truthy → 立刻返回该值
        - extract **正常**返回 falsy → 默认永久 ignore（``ignore_extract_miss=True``），
          避免每轮重复拉同一封失败邮件；设 False 可恢复旧行为
        - condition / detail / extract **抛异常** → 可重试（不 ignore）
        - ``min_received_at_ts``：若 msg 有 ``received_at_ts`` 且严格小于该值 → ignore；
          **缺 ts 的不因此丢弃**（避免误杀）

        base 不自动记录 session start；需要屏蔽历史时由调用方传入
        ``min_received_at_ts=time.time()``（建议在发信前打点）。
        """

        self.get_email()

        max_attempts = max(1, int(max_attempts))
        delay = max(0.0, float(delay))
        max_duration = None if max_duration is None else max(0.0, float(max_duration))
        min_ts = (
            None
            if min_received_at_ts is None
            else float(min_received_at_ts)
        )
        started_at = time.monotonic()

        ignored_ids: set[Any] = set()
        last_error: Optional[BaseException] = None
        attempts_done = 0

        for _ in range(max_attempts):
            attempts_done += 1
            if max_duration is not None and time.monotonic() - started_at >= max_duration:
                break

            try:
                messages = self._fetch_message_list() or []
            except Exception as exc:
                last_error = exc
                if delay:
                    time.sleep(float(delay))
                continue

            messages = self._sort_messages(messages, newest_first=newest_first)

            seen_this_round: set[Any] = set()
            for msg in messages:
                if max_duration is not None and time.monotonic() - started_at >= max_duration:
                    break

                # 契约：msg 必须包含 'id'。缺失是 provider bug，直接抛 KeyError。
                msg_key = msg["id"]
                if msg_key in ignored_ids or msg_key in seen_this_round:
                    continue
                seen_this_round.add(msg_key)

                if min_ts is not None:
                    ts = msg.get("received_at_ts")
                    if ts is not None:
                        try:
                            ts_val = float(ts)
                        except (TypeError, ValueError):
                            ts_val = None
                        if ts_val is not None and math.isfinite(ts_val) and ts_val < min_ts:
                            ignored_ids.add(msg_key)
                            continue

                try:
                    if not condition_func(msg):
                        ignored_ids.add(msg_key)
                        continue
                except Exception as exc:
                    last_error = exc
                    continue

                try:
                    content = self._fetch_message_detail(msg)
                except Exception as exc:
                    last_error = exc
                    continue

                try:
                    result = extract_func(msg, content)
                except Exception as exc:
                    last_error = exc
                    continue

                if result:
                    return result

                if ignore_extract_miss:
                    ignored_ids.add(msg_key)

            if delay:
                time.sleep(float(delay))

        elapsed = time.monotonic() - started_at
        timeout_hint = (
            0.0
            if max_attempts <= 1
            else (max_attempts - 1) * float(delay)
        )

        duration_hint = ""
        if max_duration is not None:
            duration_hint = (
                f", max_duration={max_duration}s, 实际耗时≈{elapsed:.1f}s"
            )

        extra = (
            f", attempts_done={attempts_done}, ignored_ids={len(ignored_ids)}"
        )
        if last_error is not None:
            raise TimeoutError(
                "等待目标邮件超时"
                f"（max_attempts={max_attempts}, delay={delay}s, "
                f"预计最少 sleep≈{timeout_hint:.1f}s{duration_hint}{extra}）；"
                f"最后错误：{last_error}"
            )
        raise TimeoutError(
            "等待目标邮件超时"
            f"（max_attempts={max_attempts}, delay={delay}s, "
            f"预计最少 sleep≈{timeout_hint:.1f}s{duration_hint}{extra}）"
        )

    @staticmethod
    def _sort_messages(
        messages: list[MessageSummary],
        *,
        newest_first: bool,
    ) -> list[MessageSummary]:
        """按 received_at_ts 排序。缺失视为 -inf（最旧）。

        - newest_first=True：降序（新的在前）
        - newest_first=False：升序（旧的在前）
        - tie-break 依赖 Python sorted 的稳定性，保持 provider 原序
        """

        def sort_key(msg: MessageSummary) -> float:
            ts = msg.get("received_at_ts")
            if ts is None:
                return -math.inf
            try:
                value = float(ts)
            except (TypeError, ValueError):
                return -math.inf
            if not math.isfinite(value):
                return -math.inf
            return value

        return sorted(messages, key=sort_key, reverse=newest_first)
