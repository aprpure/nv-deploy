from __future__ import annotations

import importlib
import inspect
from typing import Any, Optional

from .base import BaseEmailClient

# Short name → (module_path, class_name). Aliases share the same target.
# 精简部署版：仅保留 3 个邮箱服务商
_PROVIDER_TARGETS: dict[str, tuple[str, str]] = {
    # tinyhost.shop (默认推荐)
    "tinyhost": (".tinyhost_shop", "TinyhostShopClient"),
    "tinyhost_shop": (".tinyhost_shop", "TinyhostShopClient"),
    "tinyhostshop": (".tinyhost_shop", "TinyhostShopClient"),
    # boomlify.com
    "boomlify": (".boomlify_com", "BoomlifyComClient"),
    "boomlify_com": (".boomlify_com", "BoomlifyComClient"),
    # freecustom.email
    "freecustom": (".freecustom_email", "FreecustomEmailClient"),
    "freecustom_email": (".freecustom_email", "FreecustomEmailClient"),
}

DEFAULT_EMAIL_PROVIDER = "tinyhost"


def normalize_provider_name(name: str) -> str:
    key = str(name or "").strip().lower()
    key = key.replace("-", "_").replace(" ", "_")
    if key.endswith("_client"):
        key = key[: -len("_client")]
    elif key.endswith("client"):
        key = key[: -len("client")]
    return key


def list_email_providers() -> list[str]:
    """Canonical short names (deduped, stable order — first alias per provider)."""
    seen_targets: set[tuple[str, str]] = set()
    out: list[str] = []
    for key, target in _PROVIDER_TARGETS.items():
        if target in seen_targets:
            continue
        seen_targets.add(target)
        out.append(key)
    return out


def resolve_email_provider_class(name: str) -> type[BaseEmailClient]:
    key = normalize_provider_name(name)
    target = _PROVIDER_TARGETS.get(key)
    if target is None:
        available = ", ".join(list_email_providers())
        raise ValueError(
            f"Unknown email provider {name!r}. "
            f"Available: {available}"
        )
    module_path, class_name = target
    mod = importlib.import_module(module_path, package=__package__)
    cls = getattr(mod, class_name)
    if not isinstance(cls, type) or not issubclass(cls, BaseEmailClient):
        raise TypeError(f"{class_name} is not a BaseEmailClient subclass")
    return cls  # type: ignore[return-value]


def create_email_client(
    name: str = DEFAULT_EMAIL_PROVIDER,
    *,
    email_address: Optional[str] = None,
    proxy: Optional[str] = None,
    proxies: Any = None,
    blocked_suffixes: tuple[str, ...] = (),
    **extra: Any,
) -> BaseEmailClient:
    """Instantiate an email client by short provider name."""
    cls = resolve_email_provider_class(name)
    sig = inspect.signature(cls.__init__)
    params = sig.parameters

    kwargs: dict[str, Any] = dict(extra)
    if "proxy" in params and proxy is not None:
        kwargs["proxy"] = proxy
    if "proxies" in params and proxies is not None:
        kwargs["proxies"] = proxies
    if email_address:
        if "email_address" in params:
            kwargs["email_address"] = email_address
        elif "email" in params:
            kwargs["email"] = email_address
    if blocked_suffixes and "blocked_suffixes" in params:
        kwargs["blocked_suffixes"] = blocked_suffixes

    return cls(**kwargs)
