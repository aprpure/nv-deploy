import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import matplotlib
matplotlib.use("Agg")  # 强制无 GUI 模式，避免多线程 Tkinter 警告

import argparse
import asyncio
import base64
import csv
import json
import os
import random
import re
import shutil
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Type, TypeVar
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from pydantic import BaseModel, model_validator

from email_clients.factory import list_email_providers

# ---------------- 日志工具 ----------------

def _tid():
    """返回当前线程短标识 + 时间戳，便于多线程并发时区分日志来源。"""
    t = threading.current_thread()
    try:
        name = t.name.split("_")[-1][:8]
    except Exception:
        name = "?"
    return f"[{time.strftime('%H:%M:%S')}|t{name}]"


def _log(*args):
    print(_tid(), *args, flush=True)


# ---------------- 配置 ----------------
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

_DEFAULTS = {
    "llm": {
        "type": "openai",
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "json_mode": True,
        "proxy": "",
        "gemini_model": "gemini-3.7-flash",
        "gemini_proxy": "",
        "gemini_base_url": "",
        "openai_api_key": "",
        "openai_base_url": "",
        "openai_model": "",
        "openai_proxy": "",
        "openai_thinking": False,
        "openai_reasoning_effort": "",
    }
}


def _load_config_file() -> dict:
    cfg = json.loads(json.dumps(_DEFAULTS))
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        llm = data.get("llm", {})
        for k in cfg["llm"]:
            if k in llm:
                cfg["llm"][k] = llm[k]
    except Exception:
        pass
    return cfg


def _load_llm_config():
    cfg = _load_config_file()["llm"]
    cfg["json_mode"] = bool(cfg.get("json_mode", True))
    cfg["type"] = str(cfg.get("type", "openai")).strip().lower()
    cfg["openai_thinking"] = bool(cfg.get("openai_thinking", False))
    cfg["openai_reasoning_effort"] = str(cfg.get("openai_reasoning_effort", "")).strip().lower()
    return cfg


_llm_cfg = _load_llm_config()

TEMPMAIL_API_KEY = "mk_9u10jXE4BjJ-_utaZEaeFNZxdD_s9SqP"
TEMPMAIL_API_BASE_URL = "https://mail.ltr.pp.ua"
CSV_FILENAME = "nvidia_keys.csv"

EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "tinyhost")

# 浏览器求解并发上限：验证码占用的 Chromium 槽位数（run_tasks 启动时按 --browser 重建）
browser_semaphore = threading.Semaphore(3)

HCAPTCHA_SITE_KEY = "3443d8f6-da7a-4326-929f-4d7fc89ab0d1"

# LLM 后端类型: "gemini" 走官方 Google Gemini SDK；"openai" 走 OpenAI 兼容端点
LLM_TYPE = os.environ.get("LLM_TYPE", _llm_cfg["type"])

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", _llm_cfg.get("api_key", ""))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", _llm_cfg.get("gemini_model", "gemini-3.7-flash"))
GEMINI_PROXY = os.environ.get("GEMINI_PROXY", _llm_cfg.get("gemini_proxy", ""))
GEMINI_BASE_URL = os.environ.get("GEMINI_BASE_URL", _llm_cfg.get("gemini_base_url", ""))

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", _llm_cfg.get("openai_base_url", _llm_cfg.get("base_url", "https://api.openai.com/v1")))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", _llm_cfg.get("openai_api_key", _llm_cfg.get("api_key", "")))
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", _llm_cfg.get("openai_model", _llm_cfg.get("model", "gpt-4o")))
OPENAI_JSON_MODE = os.environ.get("OPENAI_JSON_MODE", "1" if _llm_cfg["json_mode"] else "0") == "1"
OPENAI_PROXY = os.environ.get("OPENAI_PROXY", _llm_cfg.get("openai_proxy", _llm_cfg.get("proxy", "")))
OPENAI_THINKING = (os.environ.get("OPENAI_THINKING", "1" if _llm_cfg["openai_thinking"] else "0") == "1")
OPENAI_REASONING_EFFORT = (os.environ.get("OPENAI_REASONING_EFFORT", _llm_cfg["openai_reasoning_effort"]) or "").strip().lower()

NGC_LOGIN_URL = "https://api.ngc.nvidia.com/login"
NVGS_BASE = "https://accounts.nvgs.nvidia.com/api/1/frontend/oauth"
NVGS_VALIDATOR = "https://accounts.nvgs.nvidia.com/api/1"
LOGIN_NVIDIA = "https://login.nvidia.com"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

csv_lock = threading.Lock()

CHECKBOX_IFRAME = 'iframe[src*="frame=checkbox"]'

ResponseT = TypeVar("ResponseT")


def generate_password():
    chars = string.ascii_letters + string.digits + "!@#$%"
    return "Nv" + "".join(random.choices(chars, k=12)) + "1!"


def generate_device_id():
    return "".join(random.choices(string.ascii_letters + string.digits, k=16))


def save_to_csv(email, password, api_key):
    with csv_lock:
        try:
            exists = os.path.isfile(CSV_FILENAME)
            now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
            with open(CSV_FILENAME, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not exists:
                    writer.writerow(["Email", "Password", "APIKey", "CreateTime"])
                writer.writerow([email, password, api_key, now])
            print(f"[CSV] 保存成功: {email}")
        except Exception as e:
            print(f"[CSV] 保存失败: {e}")


# ---------------- 临时邮箱 ----------------
class TempMailService:
    """临时邮箱客户端（基于 email_clients 抽象接口）。

    使用 email_clients 工厂按 EMAIL_PROVIDER 创建真实客户端；
    提供接口：get_email() / wait_for_pin() / close()。
    """

    def __init__(self, provider: str | None = None, proxy: str | None = None,
                 email_address: str | None = None):
        from email_clients import factory

        self.provider = provider or EMAIL_PROVIDER
        kwargs: dict = {"proxy": proxy, "email_address": email_address}
        try:
            self._client = factory.create_email_client(self.provider, **kwargs)
        except Exception as e:
            print(f"  [邮箱] provider={self.provider!r} 初始化失败({e!r})，回退 tinyhost")
            self.provider = "tinyhost"
            self._client = factory.create_email_client(self.provider, **kwargs)

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    def get_email(self):
        return self._client.get_email()

    def wait_for_pin(self, max_attempts=25, delay=3):
        """轮询 NVIDIA 验证邮件，提取 6 位 PIN。"""

        def _cond(msg):
            subj = (msg.get("subject") or "").lower()
            return ("nvidia email verification" in subj
                    or "nvidia account created" in subj
                    or "verify your email" in subj)

        def _extract(msg, content):
            m = re.search(r"(\d{3}-\d{3})", content or "")
            if not m:
                return None
            return m.group(1).replace("-", "")

        try:
            return self._client.wait_for_target(
                condition_func=_cond,
                extract_func=_extract,
                max_attempts=max_attempts,
                delay=delay,
            )
        except TimeoutError:
            return None


# ---------------- OpenAI 格式 Provider（实现 hcaptcha-challenger ChatProvider 协议） ----------------
def extract_first_json_block(text: str) -> dict | None:
    pattern = r"```json\s*([\s\S]*?)```"
    m = re.search(pattern, text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None


def _coerce_model_payload(data: dict) -> dict:
    """把模型输出的宽松 JSON 格式（裸数组/简写）规范成官方 pydantic schema 期望的结构。"""
    data = dict(data)

    def _to_point(v):
        if isinstance(v, dict):
            return v
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            return {"x": int(v[0]), "y": int(v[1])}
        if isinstance(v, str):
            parts = v.replace("[", "").replace("]", "").split(",")
            if len(parts) >= 2:
                return {"x": int(parts[0].strip()), "y": int(parts[1].strip())}
        return v

    # points：[[x,y],...] 或 {"points":[x,y]}  → [{"x":x,"y":y},...]
    if isinstance(data.get("points"), list):
        pts = data["points"]
        if pts and not isinstance(pts[0], dict):
            # 若是单条 [x,y] 或 x/y 分离数组
            if isinstance(pts[0], (int, float)):
                if len(pts) >= 2:
                    data["points"] = [{"x": int(pts[0]), "y": int(pts[1])}]
            else:
                data["points"] = [_to_point(p) for p in pts if isinstance(p, (list, tuple))]
        elif pts and isinstance(pts[0], dict) and isinstance(pts[0].get("x"), list):
            xs, ys = pts[0]["x"], pts[0]["y"]
            data["points"] = [{"x": int(x), "y": int(y)}
                              for x, y in zip(xs, ys)]

    # coordinates：[[r,c],...] → [{"box_2d":[r,c]},...]
    if isinstance(data.get("challenge_prompt"), (dict, list)):
        # 模型误把 schema 当输出，丢弃
        data["challenge_prompt"] = ""
    if isinstance(data.get("coordinates"), list):
        coords = data["coordinates"]
        if coords and not isinstance(coords[0], dict):
            data["coordinates"] = [
                {"box_2d": [int(v[0]), int(v[1])]}
                for v in coords if isinstance(v, (list, tuple)) and len(v) >= 2
            ]
        elif coords and isinstance(coords[0], dict):
            fixed = []
            for c in coords:
                b = c.get("box_2d")
                if isinstance(b, str):
                    b = [int(x) for x in b.replace("[", "").replace("]", "").split(",") if x.strip()]
                    c = {**c, "box_2d": b}
                elif b is None and "row" in c and "col" in c:
                    c = {**c, "box_2d": [int(c["row"]), int(c["col"])]}
                fixed.append(c)
            data["coordinates"] = fixed

    # paths：[{start_point,end_point},...]，start/end 可能是 [x,y] 或 {"x":[..]}
    if isinstance(data.get("paths"), list):
        fixed = []
        for p in data["paths"]:
            if not isinstance(p, dict):
                continue
            sp = p.get("start_point")
            ep = p.get("end_point")
            if isinstance(sp, list):
                sp = _to_point(sp)
            if isinstance(ep, list):
                ep = _to_point(ep)
            if isinstance(sp, dict) and isinstance(sp.get("x"), list):
                sp = {"x": int(sp["x"][0]), "y": int(sp["x"][1])}
            if isinstance(ep, dict) and isinstance(ep.get("x"), list):
                ep = {"x": int(ep["x"][0]), "y": int(ep["x"][1])}
            fixed.append({**p, "start_point": sp, "end_point": ep})
        data["paths"] = fixed

    # 字段丢失兜底：空结构而不是让 pydantic 报 missing
    if "challenge_prompt" not in data:
        data["challenge_prompt"] = ""
    for _fld in ("points", "coordinates", "paths"):
        if _fld not in data:
            data[_fld] = []

    return data


class OpenAIProvider:
    """OpenAI 兼容格式的视觉模型 provider（支持思考模式）。"""

    def __init__(self, api_key: str, model: str, base_url: str = OPENAI_BASE_URL,
                 proxy: str | None = None, thinking: bool | None = None,
                 reasoning_effort: str | None = None):
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._thinking = thinking if thinking is not None else OPENAI_THINKING
        re = reasoning_effort if reasoning_effort is not None else OPENAI_REASONING_EFFORT
        self._reasoning_effort = re if re in ("low", "high", "max") else ""
        client_kwargs: dict = {
            "base_url": self._base_url,
            "headers": {"Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"},
            "timeout": httpx.Timeout(180, connect=30),
            "verify": False,
        }
        if proxy:
            client_kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**client_kwargs)

    def _parse_response(self, text: str, response_schema: Type[ResponseT]) -> ResponseT:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        try:
            data = json.loads(text)
        except Exception:
            data = extract_first_json_block(text)
        if not data:
            raise ValueError(f"无法解析模型输出为 JSON: {text[:300]}")
        data = _coerce_model_payload(data)
        return response_schema(**data)

    async def generate_with_images(
        self,
        *,
        images: List[Path],
        response_schema: Type[ResponseT],
        user_prompt: str | None = None,
        description: str | None = None,
        **kwargs,
    ) -> ResponseT:
        content: list = []
        for img in images:
            img = Path(img)
            b64 = base64.b64encode(img.read_bytes()).decode()
            mime = "image/png" if img.suffix.lower() == ".png" else "image/jpeg"
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"}})
        if user_prompt:
            content.append({"type": "text", "text": user_prompt})

        messages: list = []
        sys_parts = []
        if description:
            sys_parts.append(description)
        sys_parts.append(
            "You MUST reply with ONLY a valid JSON object matching this schema: "
            + json.dumps(response_schema.model_json_schema(), ensure_ascii=False)
            + ". Do NOT include markdown fences or any text outside the JSON."
        )
        messages.append({"role": "system", "content": "\n\n".join(sys_parts)})
        messages.append({"role": "user", "content": content})

        payload = {
            "model": self._model,
            "messages": messages,
        }
        # 阿里云百炼/OpenAI 兼容思考模式：enable_thinking + reasoning_effort (+temperature 仍合法)
        if self._thinking or self._reasoning_effort:
            payload["enable_thinking"] = True
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        payload["temperature"] = kwargs.get("temperature", 0.1)
        if OPENAI_JSON_MODE:
            payload["response_format"] = {"type": "json_object"}

        last_err = None
        for attempt in range(3):
            try:
                resp = await self._client.post("/chat/completions", json=payload)
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                return self._parse_response(text, response_schema)
            except Exception as e:
                last_err = e
                if isinstance(e, httpx.HTTPError):
                    req = getattr(e, "request", None)
                    print(f"  [AI] 尝试 {attempt+1}/3 失败: {type(e).__name__}: {str(e)[:150]}"
                          + (f" ({req.url.host})" if req is not None else ""))
                if attempt < 2:
                    is_429 = isinstance(e, httpx.HTTPStatusError) and getattr(e.response, "status_code", None) == 429
                    await asyncio.sleep(10 if is_429 else 2 * (attempt + 1))
        raise RuntimeError(f"OpenAI 请求失败: {last_err}")


def _install_openai_provider():
    """monkey-patch Reasoner._create_default_provider，使所有工具使用 OpenAI 格式。"""
    from hcaptcha_challenger.tools.internal.base import Reasoner

    def _create_default_provider(self):
        return OpenAIProvider(
            api_key=OPENAI_API_KEY,
            model=OPENAI_MODEL,
            base_url=OPENAI_BASE_URL,
            proxy=OPENAI_PROXY or None,
        )

    Reasoner._create_default_provider = _create_default_provider


def _install_gemini_provider():
    """Gemini 模式：让 Reasoner 使用 GEMINI_MODEL；支持 GEMINI_BASE_URL / GEMINI_PROXY。"""
    from hcaptcha_challenger.tools.internal.base import Reasoner
    from hcaptcha_challenger.tools.internal.providers.gemini import GeminiProvider

    def _create_default_provider(self):
        return GeminiProvider(api_key=GEMINI_API_KEY, model=GEMINI_MODEL)

    Reasoner._create_default_provider = _create_default_provider

    if GEMINI_BASE_URL or GEMINI_PROXY:
        import httpx as _httpx
        from google import genai
        from google.genai import types as gtypes

        def _client_with_custom_http(self):
            if self._client is None:
                http_kwargs: dict = {}
                if GEMINI_BASE_URL:
                    http_kwargs["base_url"] = GEMINI_BASE_URL
                if GEMINI_PROXY:
                    # 每实例懒创建，避免跨线程 event loop 复用 AsyncClient
                    http_kwargs["httpx_client"] = _httpx.Client(
                        proxy=GEMINI_PROXY, timeout=_httpx.Timeout(180, connect=30)
                    )
                    http_kwargs["httpx_async_client"] = _httpx.AsyncClient(
                        proxy=GEMINI_PROXY, timeout=_httpx.Timeout(180, connect=30)
                    )
                self._client = genai.Client(
                    api_key=self._api_key,
                    http_options=gtypes.HttpOptions(**http_kwargs),
                )
            return self._client

        GeminiProvider.client = property(_client_with_custom_http)


def _new_provider():
    return OpenAIProvider(
        api_key=OPENAI_API_KEY,
        model=OPENAI_MODEL,
        base_url=OPENAI_BASE_URL,
        proxy=OPENAI_PROXY or None,
    )


# ---------------- 自定义挑战求解（高清切格 + 相对坐标 + 投票） ----------------

class _RelPoint(BaseModel):
    x: int
    y: int

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data):
        if isinstance(data, (list, tuple)) and len(data) >= 2:
            return {"x": data[0], "y": data[1]}
        if isinstance(data, dict):
            if isinstance(data.get("x"), (list, tuple)) and len(data["x"]) >= 2:
                return {"x": data["x"][0], "y": data["x"][1]}
        return data


class _RelSchema(BaseModel):
    points: list[_RelPoint]

    @model_validator(mode="before")
    @classmethod
    def _coerce_points(cls, data):
        if isinstance(data, dict) and isinstance(data.get("points"), list):
            pts = data["points"]
            if pts and not isinstance(pts[0], dict):
                data = {**data, "points": [{"x": p[0], "y": p[1]}
                                           for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]}
            elif pts and isinstance(pts[0], dict) and isinstance(pts[0].get("x"), (list, tuple)):
                xs, ys = pts[0]["x"], pts[0]["y"]
                data = {**data, "points": [{"x": xs[i], "y": ys[i]} for i in range(min(len(xs), len(ys)))]}
        return data


class _BinCoord(BaseModel):
    box_2d: list[int]

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data):
        if isinstance(data, (list, tuple)) and len(data) >= 2:
            return {"box_2d": [int(x) for x in data[:2]]}
        if isinstance(data, dict):
            v = data.get("box_2d")
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                return {"box_2d": [int(x) for x in v[:2]]}
            if isinstance(v, str):
                parts = v.replace("[", "").replace("]", "").split(",")
                return {"box_2d": [int(x) for x in parts if x.strip()][:2]}
            if v is None and "row" in data and "col" in data:
                return {"box_2d": [int(data["row"]), int(data["col"])]}
        return data


class _BinSchema(BaseModel):
    coordinates: list[_BinCoord] = []

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data):
        if isinstance(data, dict):
            c = data.get("coordinates")
            if c and isinstance(c, list) and not isinstance(c[0], dict):
                data = {**data, "coordinates": [{"box_2d": v} for v in c]}
        return data


class _DragPoint(BaseModel):
    x: int
    y: int

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data):
        if isinstance(data, (list, tuple)) and len(data) >= 2:
            return {"x": data[0], "y": data[1]}
        if isinstance(data, dict) and isinstance(data.get("x"), (list, tuple)) and len(data["x"]) >= 2:
            return {"x": data["x"][0], "y": data["x"][1]}
        return data


class _DragPath(BaseModel):
    start_point: _DragPoint
    end_point: _DragPoint


class _DragSchema(BaseModel):
    paths: list[_DragPath] = []

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data):
        if isinstance(data, dict) and isinstance(data.get("paths"), list):
            ps = data["paths"]
            fixed = []
            for p in ps:
                if isinstance(p, list) and len(p) >= 2:
                    fixed.append({"start_point": p[0], "end_point": p[1]})
                elif isinstance(p, dict):
                    fixed.append(p)
            if len(fixed) != len(ps):
                data = {**data, "paths": fixed}
        return data


def _split_and_upscale(img, rows: int = 3, cols: int = 3, cell_size: int = 240):
    """按 rows x cols 切格并放大拼接成高清大图，带格线分隔。"""
    from PIL import Image as _PILImage
    w, h = img.size
    cw, chh = w // cols, h // rows
    grid = _PILImage.new("RGB", (cell_size * cols, cell_size * rows), (180, 180, 180))
    for r in range(rows):
        for c in range(cols):
            cell = img.crop((c * cw, r * chh, (c + 1) * cw, (r + 1) * chh))
            cell = cell.resize((cell_size, cell_size), _PILImage.LANCZOS)
            grid.paste(cell, (c * cell_size, r * cell_size))
    return grid


def _filter_points(points, max_points=None, *, max_x=1000, max_y=940, min_gap=25):
    """过滤模型输出点质量：越界剔除、近邻去重、数量封顶。"""
    out = []
    for x, y in points:
        if not (0 <= x <= max_x and 0 <= y <= max_y):
            continue
        if any(abs(x - ox) < min_gap and abs(y - oy) < min_gap for ox, oy in out):
            continue
        out.append((int(x), int(y)))
    if max_points and len(out) > max_points:
        out = out[:max_points]
    return out


def _cluster_consensus(all_pts, *, votes, radius=40):
    """把多次投票的候选点做近邻聚类，保留至少一半轮次支撑的点。

    radius 为近邻判定距离（同一目标在多次识别中的抖动范围）。
    返回最终保留的点列表（每簇取均值中心）。
    """
    from statistics import mean
    pts = [(int(x), int(y)) for x, y in all_pts if 0 <= x <= 1000 and 0 <= y <= 940]
    clusters: list[list] = []
    for x, y in pts:
        placed = False
        for cl in clusters:
            cx = mean(p[0] for p in cl)
            cy = mean(p[1] for p in cl)
            if abs(x - cx) <= radius and abs(y - cy) <= radius:
                cl.append((x, y))
                placed = True
                break
        if not placed:
            clusters.append([(x, y)])
    threshold = max(1, round(votes / 2.0))
    out = []
    for cl in clusters:
        if len(cl) >= threshold:
            out.append((int(mean(p[0] for p in cl)), int(mean(p[1] for p in cl))))
    return _filter_points(out)


async def _vote(provider, images, schema, prompt, desc, *, votes=3, max_retries=2):
    """调用模型多次，按输出类型做共识：
    - points（点选）：`_cluster_consensus` 近邻聚类，保留 ≥半数轮次支撑的点
    - coordinates（9格）：多数票（一格 ≥半数轮次选中才保留）
    - paths（拖拽）：合并去重后的全部候选路径
    """
    def _norm_points(resp):
        p = getattr(resp, "points", None) or []
        return [(x.x, x.y) for x in p]

    def _norm_cells(resp):
        c = getattr(resp, "coordinates", None) or []
        return [tuple(x.box_2d) for x in c if len(x.box_2d) == 2]

    def _norm_paths(resp):
        p = getattr(resp, "paths", None) or []
        out = []
        for x in p:
            sx, sy = x.start_point.x, x.start_point.y
            ex, ey = x.end_point.x, x.end_point.y
            if not (0 <= sx <= 1000 and 0 <= sy <= 940 and 0 <= ex <= 1000 and 0 <= ey <= 940):
                continue
            out.append(((sx, sy), (ex, ey)))
        return out

    async def _one():
        last = None
        for _a in range(max_retries + 1):
            last = await provider.generate_with_images(
                images=images, response_schema=schema,
                user_prompt=prompt, description=desc, temperature=0.1)
            if hasattr(last, "points") and _norm_points(last):
                return last
            if hasattr(last, "coordinates") and _norm_cells(last):
                return last
            if hasattr(last, "paths") and _norm_paths(last):
                return last
            print(f"  [求解] 输出异常，重试 {_a+1}/{max_retries+1}")
        return last

    cands = []
    for _ in range(votes):
        cands.append(await _one())
    cands = [c for c in cands if c is not None]
    if not cands:
        raise RuntimeError("模型求解失败")

    first = cands[0]
    if hasattr(first, "points"):
        pts = []
        for c in cands:
            pts.append(_norm_points(c))
        all_pts = [p for sub in pts for p in sub]
        kept = _cluster_consensus(all_pts, votes=len(cands))
        points = [type("_P", (), {"x": int(x), "y": int(y)}) for x, y in kept]
        return type("_Resp", (), {"points": points})()

    if hasattr(first, "coordinates"):
        all_cells: list[tuple] = []
        for c in cands:
            for cell in _norm_cells(c):
                if 0 <= cell[0] < 3 and 0 <= cell[1] < 3:
                    all_cells.append(cell)
        from collections import Counter
        counts = Counter(all_cells)
        threshold = max(1, (len(cands) + 1) // 2)
        winner = [cell for cell, n in counts.items() if n >= threshold]
        coords = [type("_C", (), {"box_2d": list(cell)})() for cell in winner]
        return type("_Resp", (), {"coordinates": coords})()

    if hasattr(first, "paths"):
        seen: set[tuple] = set()
        all_paths: list[tuple] = []
        for c in cands:
            for p in _norm_paths(c):
                ((sx, sy), (ex, ey)) = p
                key = (sx // 20, sy // 20, ex // 20, ey // 20)
                if key in seen:
                    continue
                seen.add(key)
                all_paths.append(p)
        paths_out = [
            type("_D", (), {
                "start_point": type("_P", (), {"x": int(sx), "y": int(sy)})(),
                "end_point": type("_P", (), {"x": int(ex), "y": int(ey)})(),
            })()
            for (sx, sy), (ex, ey) in all_paths
        ]
        return type("_Resp", (), {"paths": paths_out})()

    return first


def _payload_question(arm):
    try:
        q = arm.captcha_payload.get_requester_question()
    except Exception:
        q = ""
    return q or "click on all the objects that match the instruction"


def _payload_max_shapes(arm):
    try:
        rc = arm.captcha_payload.request_config
        if isinstance(rc, dict):
            return rc.get("max_shapes_per_image") or 0
        return getattr(rc, "max_shapes_per_image", None) or 0
    except Exception:
        return 0


def _payload_entities(arm):
    """拖拽 payload 的服务器标注实体（coords/size 相对 canvas 原图 480x330）。"""
    try:
        tasks = arm.captcha_payload.tasklist
    except Exception:
        return []
    ents = []
    for task in tasks:
        for ent in getattr(task, "entities", None) or []:
            c = getattr(ent, "coords", None) or []
            s = getattr(ent, "size", None) or []
            if len(c) == 2 and len(s) == 2:
                ents.append((c, s))
    return ents


def _install_custom_solvers():
    """替换 RoboticArm 点选求解方法：整图相对坐标 → bbox 映射点击，带异常重试。"""
    import hcaptcha_challenger.agent.challenger as ch_mod

    async def solve_select(self, job_type):
        frame_challenge = await self.get_challenge_frame_locator()
        crumb_count = await self.check_crumb_count()
        cache_key = self.config.create_cache_key(self.captcha_payload)
        question = _payload_question(self)
        max_shapes = _payload_max_shapes(self)
        _log(f"[点选] 开始 crumb={crumb_count} 题目={question!r} max_shapes={max_shapes}")
        for cid in range(crumb_count):
            await self.page.wait_for_timeout(self.config.WAIT_FOR_CHALLENGE_VIEW_TO_RENDER_MS)
            canvas_loc = frame_challenge.locator("canvas")
            shot = cache_key.joinpath(f"{cache_key.name}_{cid}_canvas.png")
            shot.parent.mkdir(parents=True, exist_ok=True)
            await canvas_loc.screenshot(type="png", path=shot)
            bbox = await canvas_loc.bounding_box()
            if not bbox or bbox.get("width", 0) <= 0:
                raise RuntimeError("canvas bbox 获取失败")
            _log(f"[点选] 截图 {shot.name} bbox={bbox}")

            import io as _io
            from PIL import Image as _PILImage
            provider = _new_provider()

            hint = ""
            if max_shapes:
                hint = f"\n已知：这张图中最多有 {max_shapes} 个目标，不要输出超出这个数量的坐标。"

            def _block_prompt():
                return (
                    f"题目：{question}\n"
                    "请按照上面题目的要求，找出所有需要点击的目标，"
                    "识别物体的完整轮廓，把每个目标**中心点**坐标输出。\n"
                    "坐标系基于这张图片本身：X 轴 0~1000 对应图片从左到右，"
                    "Y 轴 0~940 对应图片从上到下。左上角为 (0,0)，右下角为 (1000,940)。\n"
                    "坐标必须是整数；请输出全部目标各一条坐标，不要遗漏。\n"
                    "输出必须是 JSON 格式：{\"points\": [[x1,y1], [x2,y2], ...]} "
                    "只输出 JSON，不要有其他文字。"
                    + hint
                )

            desc = (
                "You are a precise visual locator for CAPTCHA tasks. "
                f"The instruction is: {question}. "
                "Identify every object in the image that should be clicked, "
                "recognizing the full contour. Output the center (x, y) of each. "
                "Coordinates are relative to the image itself: x runs 0..1000 "
                "left-to-right, y runs 0..940 top-to-bottom; top-left is (0,0), "
                "bottom-right is (1000,940). Use integer coordinates only, one "
                "point per target, do not miss any."
            )
            if max_shapes:
                desc += f" At most {max_shapes} objects total exist across the whole image."

            all_pts = []
            # 整块图：放大到 800 长边喂模型（单次推理）
            import io as _vio2
            from PIL import Image as _PILImage2
            img = _PILImage2.open(_vio2.BytesIO(shot.read_bytes())).convert("RGB")
            w, h = img.size
            scale = 800 / max(w, h)
            full = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                              _PILImage2.LANCZOS)
            fp = cache_key.joinpath(f"{cache_key.name}_{cid}_full.png")
            full.save(fp)
            try:
                resp = await _vote(provider, [fp], _RelSchema,
                                   _block_prompt(), desc)
                pts = [(p.x, p.y) for p in (resp.points or [])]
            except Exception as e:
                _log(f"[点选] 整图投票失败: {e}")
                pts = []
            pts = _filter_points(pts, max_shapes)
            if not pts:
                raise RuntimeError(
                    f"点选投票后无有效目标点（题目={question!r}），"
                    "放弃本题让库刷新换题")
            _log(f"[点选] 共识点: {pts}")
            bw = bbox["width"]
            bh = bbox["height"]
            for x, y in pts:
                gx = bbox["x"] + (x / 1000.0) * bw
                gy = bbox["y"] + (y / 940.0) * bh
                all_pts.append((gx, gy))

            _log(f"[点选] 全图共 {len(all_pts)} 个目标点")
            for px, py in all_pts:
                _log(f"[点选] 点击 ({px:.1f},{py:.1f})")
                await self.page.mouse.click(px, py, delay=180)
                await self.page.wait_for_timeout(500)
            _log(f"[点选] 点击完成 共{len(all_pts)}点 提交")

            from contextlib import suppress
            from playwright.async_api import TimeoutError as PwTimeoutError
            with suppress(PwTimeoutError):
                submit_btn = frame_challenge.locator("//div[@class='button-submit button']")
                await self.click_by_mouse(submit_btn)

    async def solve_binary(self):
        frame_challenge = await self.get_challenge_frame_locator()
        crumb_count = await self.check_crumb_count()
        cache_key = self.config.create_cache_key(self.captcha_payload)
        question = _payload_question(self)
        _log(f"[9格] 开始 crumb={crumb_count} 题目={question!r}")
        for cid in range(crumb_count):
            await self._wait_for_all_loaders_complete()
            challenge_view = frame_challenge.locator("//div[@class='challenge-view']")
            shot = cache_key.joinpath(f"{cache_key.name}_{cid}_challenge_view.png")
            shot.parent.mkdir(parents=True, exist_ok=True)
            await challenge_view.screenshot(type="png", path=shot)

            import io as _io
            from PIL import Image as _PILImage
            img = _PILImage.open(_io.BytesIO(shot.read_bytes())).convert("RGB")
            grid_img = _split_and_upscale(img, 3, 3)
            grid_path = cache_key.joinpath(f"{cache_key.name}_{cid}_grid3x3.png")
            grid_img.save(grid_path)

            provider = _new_provider()
            prompt = (
                "以下图片是一个 3x3 的 9 宫格，每格一个对象。\n"
                f"题目：{question}\n"
                "请找出所有符合题目的格子，输出其网格坐标 [row, col]，"
                "row/col 取值 0~2（左上角 [0,0]，右下角 [2,2]）。"
                "只输出符合的格子坐标。"
            )
            desc = (
                "You are a precise hCaptcha 9-grid classifier. The image is a 3x3 grid. "
                "For each cell decide whether it matches the instruction, then output "
                "its grid coordinate [row, col] (row/col in 0..2). "
                "Output ONLY the coordinates of matching cells."
            )
            resp = await _vote(provider, [grid_path], _BinSchema, prompt, desc)
            cells = [tuple(x.box_2d) for x in (resp.coordinates or []) if len(x.box_2d) == 2]
            _log(f"[9格] 共识格子: {sorted(set(cells))}")
            if not cells:
                raise RuntimeError(
                    f"9格投票后无共识格子（题目={question!r}），"
                    "放弃本题让库刷新换题")

            boolean_matrix = [False] * 9
            for r, c in cells:
                if 0 <= r < 3 and 0 <= c < 3:
                    boolean_matrix[r * 3 + c] = True

            from contextlib import suppress
            from playwright.async_api import TimeoutError as PwTimeoutError

            positive_cases = 0
            xpath_task_image = "//div[@class='task' and contains(@aria-label, '{index}')]"
            for i, should_be_clicked in enumerate(boolean_matrix):
                if should_be_clicked:
                    task_image = frame_challenge.locator(xpath_task_image.format(index=i + 1))
                    _log(f"[9格] 点击 task index={i+1}")
                    await self.click_by_mouse(task_image)
                    positive_cases += 1
                elif positive_cases == 0 and i == len(boolean_matrix) - 1:
                    task_image = frame_challenge.locator(xpath_task_image.format(index=1))
                    await self.click_by_mouse(task_image)
            _log(f"[9格] 点击完成 共{positive_cases}格 提交")
            with suppress(PwTimeoutError):
                submit_btn = frame_challenge.locator("//div[@class='button-submit button']")
                await self.click_by_mouse(submit_btn)

    async def solve_drag(self, job_type):
        from hcaptcha_challenger.models import PointCoordinate, SpatialPath

        frame_challenge = await self.get_challenge_frame_locator()
        crumb_count = await self.check_crumb_count()
        cache_key = self.config.create_cache_key(self.captcha_payload)
        question = _payload_question(self)
        _log(f"[拖拽] 开始 crumb={crumb_count} 题目={question!r}")
        for cid in range(crumb_count):
            await self.page.wait_for_timeout(self.config.WAIT_FOR_CHALLENGE_VIEW_TO_RENDER_MS)
            challenge_view = frame_challenge.locator("//div[@class='challenge-view']")
            shot = cache_key.joinpath(f"{cache_key.name}_{cid}_challenge_view.png")
            shot.parent.mkdir(parents=True, exist_ok=True)
            await challenge_view.screenshot(type="png", path=shot)
            bbox = await challenge_view.bounding_box()
            if not bbox or bbox.get("width", 0) <= 0:
                raise RuntimeError("challenge-view bbox 获取失败")

            provider = _new_provider()

            # 起点：payload 服务器标注（coords/size 相对 canvas 原图 480x330）
            pay_starts = []
            try:
                canvas_loc = frame_challenge.locator("canvas")
                cbbox = await canvas_loc.first.bounding_box(timeout=3000)
            except Exception:
                cbbox = None
            cb_ = cbbox or bbox
            for c, s in _payload_entities(self):
                cx = cb_["x"] + (c[0] + s[0] / 2.0) / 480.0 * cb_["width"]
                cy = cb_["y"] + (c[1] + s[1] / 2.0) / 330.0 * cb_["height"]
                pay_starts.append((cx, cy))
                _log(f"[拖拽] payload 起点 coords={c} size={s} → 页面({cx:.1f},{cy:.1f})")

            prompt = (
                f"{question}\n\n"
                "请将可拖动的物体（有移动图标或凸起的形状）拖动到对应的目标轮廓内。"
                "图片视为 1000x940 坐标系，左上角 (0,0)，右下角 (1000,940)。\n"
                "输出每条拖拽路径：start_point 为被拖动物体中心，"
                "end_point 为该物体要放置的目标轮廓中心。按形状匹配。"
            )
            desc = (
                "You are a precise visual locator for drag-and-drop CAPTCHA tasks. "
                "The image is a 1000x940 coordinate system. Identify draggable objects "
                "(move icon or raised shape) and their matching target outlines. "
                "Output drag paths: start_point = center of draggable object, "
                "end_point = center of the matching target outline."
            )
            resp = await _vote(provider, [shot], _DragSchema, prompt, desc)
            paths = [((p.start_point.x, p.start_point.y), (p.end_point.x, p.end_point.y))
                     for p in (resp.paths or [])]
            if not paths:
                raise RuntimeError(
                    f"拖拽后无有效路径（题目={question!r}），放弃本题让库刷新换题")
            _log(f"[拖拽] 模型路径: {paths}")

            w, h = bbox["width"], bbox["height"]
            for i, ((sx, sy), (ex, ey)) in enumerate(paths):
                # 优先 payload 精确起点，不足用模型报的起点
                if pay_starts and i < len(pay_starts):
                    fx, fy = pay_starts[i]
                else:
                    fx = bbox["x"] + sx / 1000.0 * w
                    fy = bbox["y"] + sy / 940.0 * h
                tx = bbox["x"] + ex / 1000.0 * w
                ty = bbox["y"] + ey / 940.0 * h
                if not (0 <= tx <= bbox["x"] + w and 0 <= ty <= bbox["y"] + h):
                    _log(f"[拖拽] 端点越界跳过 from=({fx:.0f},{fy:.0f}) to=({tx:.0f},{ty:.0f})")
                    continue
                _log(f"[拖拽] from=({fx:.0f},{fy:.0f}) to=({tx:.0f},{ty:.0f})")
                for dx, dy in ((0, 0), (-10, 0), (10, 0), (0, -10), (0, 10)):
                    path = SpatialPath(
                        start_point=PointCoordinate(x=int(fx), y=int(fy)),
                        end_point=PointCoordinate(x=int(tx + dx), y=int(ty + dy)),
                    )
                    await self._perform_drag_drop(path)
                await self.page.wait_for_timeout(500)
            _log(f"[拖拽] crumb {cid+1}/{crumb_count} 完成")

        from contextlib import suppress
        from playwright.async_api import TimeoutError as PwTimeoutError
        with suppress(PwTimeoutError):
            submit_btn = frame_challenge.locator("//div[@class='button-submit button']")
            await self.click_by_mouse(submit_btn)

    ch_mod.RoboticArm.challenge_image_label_binary = solve_binary
    ch_mod.RoboticArm.challenge_image_label_select = solve_select
    ch_mod.RoboticArm.challenge_image_drag_drop = solve_drag
    print("  [验证] 已启用自定义求解器（9格 / 点选 / 拖拽）")


def _install_library_voting(votes=3):
    """让原库的求解工具也投票：多次推理 + 按类型共识合并。

    - ImageAreaSelectChallenge（点选）：`_cluster_consensus` 近邻聚类
    - ImageBboxChallenge（bbox）：四角坐标取中位数
    - ImageDragDropChallenge（拖拽）：路径去重合并
    - ImageBinaryChallenge（9格）：单元格多数票

    原理：只替换库的调用入口（`_invoke_spatial` / `ImageClassifier.__call__`），
    库的截图、点击、拖拽、提交流程完全不变。
    """
    import hcaptcha_challenger.tools.spatial.base as sb_mod
    import hcaptcha_challenger.tools.image_classifier as ic_mod
    from hcaptcha_challenger.models import (
        ImageAreaSelectChallenge,
        ImageBboxChallenge,
        ImageDragDropChallenge,
        ImageBinaryChallenge,
        PointCoordinate,
    )
    from collections import Counter

    def _run(provider, images, user_prompt, description, response_schema, kwargs):
        return provider.generate_with_images(
            images=images, user_prompt=user_prompt, description=description,
            response_schema=response_schema, **kwargs)

    async def _invoke_spatial_vote(self, *, challenge_screenshot, grid_divisions,
                                   auxiliary_information=None, response_schema, **kwargs):
        images = [challenge_screenshot, grid_divisions]
        cands = [c for c in (await asyncio.gather(*[
            _run(self._provider, images, auxiliary_information,
                 self.description, response_schema, kwargs)
            for _ in range(votes)
        ])) if c is not None]
        if not cands:
            raise RuntimeError("模型求解失败")

        first = cands[0]
        if issubclass(response_schema, ImageAreaSelectChallenge):
            all_pts = [(p.x, p.y) for c in cands for p in c.points]
            kept = _cluster_consensus(all_pts, votes=len(cands))
            return response_schema(
                challenge_prompt=first.challenge_prompt,
                points=[PointCoordinate(x=x, y=y) for x, y in kept])

        if issubclass(response_schema, ImageBboxChallenge):
            from statistics import median
            b = [c.bounding_boxes for c in cands]
            return response_schema(
                challenge_prompt=first.challenge_prompt,
                bounding_boxes={
                    "top_left_x": median(x.top_left_x for x in b),
                    "top_left_y": median(x.top_left_y for x in b),
                    "bottom_right_x": median(x.bottom_right_x for x in b),
                    "bottom_right_y": median(x.bottom_right_y for x in b),
                })

        if issubclass(response_schema, ImageDragDropChallenge):
            from statistics import mean
            from hcaptcha_challenger.models import SpatialPath
            all_paths = []
            for c in cands:
                for p in c.paths:
                    sp = (p.start_point.x, p.start_point.y)
                    ep = (p.end_point.x, p.end_point.y)
                    if (0 <= sp[0] <= 1000 and 0 <= sp[1] <= 940
                            and 0 <= ep[0] <= 1000 and 0 <= ep[1] <= 940):
                        all_paths.append((sp, ep))

            def _clusters(items, radius=40):
                out = []
                for it in items:
                    for cl in out:
                        cx = mean(q[0] for q in cl)
                        cy = mean(q[1] for q in cl)
                        if abs(it[0] - cx) <= radius and abs(it[1] - cy) <= radius:
                            cl.append(it)
                            break
                    else:
                        out.append([it])
                return out

            start_groups = []
            for sp, ep in all_paths:
                for g in start_groups:
                    gx = mean(q[0][0] for q in g)
                    gy = mean(q[0][1] for q in g)
                    if abs(sp[0] - gx) <= 40 and abs(sp[1] - gy) <= 40:
                        g.append((sp, ep))
                        break
                else:
                    start_groups.append([(sp, ep)])

            paths = []
            for g in start_groups:
                tgt_clusters = _clusters([q[1] for q in g])
                best = max(tgt_clusters, key=len)
                paths.append(SpatialPath(
                    start_point=PointCoordinate(
                        x=int(mean(q[0][0] for q in g)),
                        y=int(mean(q[0][1] for q in g))),
                    end_point=PointCoordinate(
                        x=int(mean(q[0] for q in best)),
                        y=int(mean(q[1] for q in best))),
                ))
            return response_schema(challenge_prompt=first.challenge_prompt, paths=paths)

        return first

    sb_mod.SpatialReasoner._invoke_spatial = _invoke_spatial_vote

    orig_classify = ic_mod.ImageClassifier.__call__

    async def _classify_vote(self, *, challenge_screenshot, **kwargs):
        cands = [c for c in (await asyncio.gather(*[
            orig_classify(self, challenge_screenshot=challenge_screenshot, **kwargs)
            for _ in range(votes)
        ])) if c is not None]
        if not cands:
            raise RuntimeError("模型求解失败")
        first = cands[0]
        from hcaptcha_challenger.models import BoundingBoxCoordinate
        counts = Counter(tuple(cell.box_2d) for c in cands for cell in c.coordinates)
        threshold = max(1, (len(cands) + 1) // 2)
        winner = [BoundingBoxCoordinate(box_2d=list(cell))
                  for cell, n in counts.items() if n >= threshold]
        return first.__class__(
            challenge_prompt=first.challenge_prompt, coordinates=winner)

    ic_mod.ImageClassifier.__call__ = _classify_vote
    print(f"  [验证] 已启用原库投票（{votes} 轮）")


# ---------------- hCaptcha Challenger 求解器（本地浏览器 + OpenAI 视觉模型） ----------------
class HcaptchaChallengerSolver:
    def __init__(self, proxy=None, headless=False, tag=""):
        self.proxy = proxy
        self.headless = headless
        self.tag = tag

    def _p(self, *args):
        print(self.tag or "[?]", *args, flush=True)

_CHALLENGER_PATCHED = False


def _install_challenger_frame_patch():
    """库兼容补丁（幂等）：

    1) get_challenge_frame_locator：iframe 竞态返回 None 时短重试多次，
       避免 challenge 方法直接 AttributeError 崩溃；
    2) refresh_challenge：frame 为 None 时安全跳过（原实现会直接崩）。
    """
    global _CHALLENGER_PATCHED
    if _CHALLENGER_PATCHED:
        return
    import hcaptcha_challenger.agent.challenger as ch_mod
    RoboticArm = ch_mod.RoboticArm

    _orig_get_frame = RoboticArm.get_challenge_frame_locator

    async def _get_frame_retry(self, retries=5):
        frame = await _orig_get_frame(self)
        for _ in range(retries):
            if frame is not None:
                return frame
            await self.page.wait_for_timeout(800)
            frame = await _orig_get_frame(self)
        return frame

    async def _safe_refresh(self):
        frame = await self.get_challenge_frame_locator()
        if frame is None:
            print("[hcc-patch] challenge iframe 不可见，跳过刷新等待下一轮", flush=True)
            await self.page.wait_for_timeout(2000)
            return
        refresh_element = frame.locator("//div[@class='refresh button']")
        await self.click_by_mouse(refresh_element)

    RoboticArm.get_challenge_frame_locator = _get_frame_retry
    RoboticArm.refresh_challenge = _safe_refresh
    _CHALLENGER_PATCHED = True

    def solve(self) -> str:
        last_err = None
        for attempt in range(1, 4):
            try:
                return asyncio.run(self._solve_async())
            except Exception as e:
                last_err = e
                self._p(f"  [验证] 挑战第 {attempt} 次失败: {e}，重试...")
        raise RuntimeError(f"挑战重试 3 次均失败: {last_err}")

    async def _solve_async(self) -> str:
        from playwright.async_api import async_playwright
        from hcaptcha_challenger.agent import AgentV, AgentConfig
        from hcaptcha_challenger.models import ChallengeSignal, ChallengeTypeEnum

        _install_challenger_frame_patch()

        work_dir = Path(__file__).resolve().parent / "tmp" / "hcc"
        async with async_playwright() as p:
            launch_kwargs = {"headless": self.headless}
            if self.proxy:
                launch_kwargs["proxy"] = {"server": self.proxy}
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(user_agent=UA, locale="zh-CN")
            page = await context.new_page()

            try:
                await page.goto("https://login.nvgs.nvidia.com/v1/login",
                                wait_until="domcontentloaded", timeout=30000)

                await page.evaluate(f"""
                    () => {{
                        document.body.innerHTML = '';
                        const div = document.createElement('div');
                        div.id = 'hc-root';
                        document.body.appendChild(div);
                        window.__hcToken = null;
                        window.__hcOnLoad = () => {{
                            hcaptcha.render('hc-root', {{
                                sitekey: '{HCAPTCHA_SITE_KEY}',
                                hl: 'zh',
                                theme: 'light',
                                callback: (t) => {{ window.__hcToken = t; }},
                            }});
                        }};
                        const s = document.createElement('script');
                        s.src = 'https://hcaptcha.com/1/api.js?render=explicit&onload=__hcOnLoad';
                        s.async = true;
                        document.head.appendChild(s);
                    }}
                """)

                await page.wait_for_selector(CHECKBOX_IFRAME, timeout=30000)
                self._p("  [验证] hCaptcha checkbox 已渲染，开始挑战...")

                config = AgentConfig(
                    GEMINI_API_KEY=GEMINI_API_KEY,
                    cache_dir=work_dir / "cache",
                    challenge_dir=work_dir / "challenge",
                    captcha_response_dir=work_dir / "captcha",
                    enable_skills_update=False,
                    # 模型对多物体拖拽题输出空坐标时会白等 30s 超时，
                    # 直接跳过让库刷新换题
                    ignore_request_types=[ChallengeTypeEnum.IMAGE_DRAG_MULTI],
                )
                if LLM_TYPE == "gemini":
                    config.IMAGE_CLASSIFIER_MODEL = GEMINI_MODEL
                    config.SPATIAL_POINT_REASONER_MODEL = GEMINI_MODEL
                    config.SPATIAL_PATH_REASONER_MODEL = GEMINI_MODEL
                    config.CHALLENGE_CLASSIFIER_MODEL = GEMINI_MODEL
                agent = AgentV(page=page, agent_config=config)
                await agent.robotic_arm.click_checkbox()
                signal = await agent.wait_for_challenge()

                if signal == ChallengeSignal.SUCCESS and agent.cr_list:
                    token = agent.cr_list[-1].generated_pass_UUID
                    if token:
                        return token

                token = await page.evaluate("window.__hcToken")
                if token:
                    return token
                raise RuntimeError(f"挑战未通过: {signal}")
            finally:
                await browser.close()


# ---------------- 纯 HTTP 注册器 ----------------
class NvidiaHttpRegister:
    def __init__(self, proxy=None, headless=False, email_address=None):
        client_args = {"headers": {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                       "timeout": 30, "follow_redirects": False}
        if proxy:
            client_args["proxy"] = proxy
        self.proxy = proxy
        self.client = httpx.Client(**client_args)
        self.mail = TempMailService(proxy=proxy, email_address=email_address)
        self.captcha = HcaptchaChallengerSolver(proxy=proxy, headless=headless,
                                                tag=f"[{email_address}]" if email_address else "")
        self.key = None
        self._email = email_address or ""
        # NVGS 端点（默认 .com；若会话重定向落到 .cn 域名则动态切换）
        self.nvgs_login = "https://login.nvgs.nvidia.com"
        self.nvgs_base = NVGS_BASE
        self.nvgs_validator = NVGS_VALIDATOR
        self.login_nvidia = LOGIN_NVIDIA

        def _switch_nvgs_venue(suffix: str):
            if suffix == ".com":
                self.nvgs_login = "https://login.nvgs.nvidia.com"
                self.nvgs_base = "https://accounts.nvgs.nvidia.com/api/1/frontend/oauth"
                self.nvgs_validator = "https://accounts.nvgs.nvidia.com/api/1"
                self.login_nvidia = "https://login.nvidia.com"
            elif suffix == ".cn":
                self.nvgs_login = "https://login.nvgs.nvidia.cn"
                self.nvgs_base = "https://accounts.nvgs.nvidia.cn/api/1/frontend/oauth"
                self.nvgs_validator = "https://accounts.nvgs.nvidia.cn/api/1"
                self.login_nvidia = "https://login.nvidia.cn"

        self._switch_nvgs_venue = _switch_nvgs_venue

    def _p(self, *args):
        print(f"[{self._email}]" if self._email else "[?]", *args, flush=True)

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass
        self.mail.close()

    # ---- 通用工具 ----
    def _walk_redirects(self, url, max_hops=15):
        for _ in range(max_hops):
            r = self.client.get(url, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://build.nvidia.com/",
            })
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location")
                if not loc:
                    return r
                url = str(httpx.URL(url).join(loc))
                continue
            return r
        raise RuntimeError(f"重定向链过长: {url}")

    def _nvgs_headers(self):
        return {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.nvgs_login,
            "Referer": f"{self.nvgs_login}/",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.key}",
        }

    def _nvgs(self, method, path, body=None, auth=True):
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.nvgs_login,
            "Referer": f"{self.nvgs_login}/",
            "Content-Type": "application/json",
        }
        if auth:
            headers["Authorization"] = f"Bearer {self.key}"
        url = f"{self.nvgs_base}{path}"
        if method == "GET":
            r = self.client.get(url, headers=headers)
        else:
            r = self.client.post(url, headers=headers, json=body if body is not None else {})
        return r

    def _rebuild_direct_client(self):
        """重建直连 client（去掉代理后重试一次，用于代理 IP 被 NGC 拉黑时）。"""
        try:
            self.client.close()
        except Exception:
            pass
        self.client = httpx.Client(
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=30, follow_redirects=False,
        )
        self.proxy = None

    # ---- 1. OAuth 入口 ----
    def oauth_entry(self, email):
        params = urlencode({"email": email, "app": "api-catalog",
                            "redirect_uri": "https://build.nvidia.com/"})
        r = self._walk_redirects(f"{NGC_LOGIN_URL}?{params}")
        if r.status_code == 403 and self.proxy:
            self._p(f"  [OAuth] 代理 IP 被拒 (403)，改直连重试一次...")
            self._rebuild_direct_client()
            r = self._walk_redirects(f"{NGC_LOGIN_URL}?{params}")
        if r.status_code != 200:
            body = (r.text or "")[:300].replace("\n", " ")
            raise RuntimeError(
                f"OAuth 入口失败: {r.status_code} url={r.url} body={body} "
                f"(代理/IP 被 NGC 风控拦截；尝试 --no-proxy 直连或换住宅代理后重试)"
            )
        q = parse_qs(urlparse(str(r.url)).query)
        if "key" not in q:
            raise RuntimeError(f"未从最终 URL 提取到 key: {r.url}")
        self.key = q["key"][0]
        self._p(f"  [OAuth] 第一把 key 已获取 (client_id={q.get('client_id', ['?'])[0]})")

    # ---- 2. NVGS 注册 ----
    def nvgs_register(self, email, password, device_id):
        self._p("  [NVGS] 初始化...")
        self._nvgs("POST", "/initialize/check",
                   {"browserMode": "Private", "passkeySupported": True})
        self._nvgs("GET", "/login/mode")
        self._nvgs("GET", "/client")
        self.client.get(f"{self.nvgs_validator}/validation")
        r = self.client.get(
            f"{self.nvgs_validator}/validator/checkAccount",
            headers=self._nvgs_headers())
        if r.status_code != 204:
            raise RuntimeError(f"checkAccount 异常: {r.status_code}")

        r = self._nvgs("POST", "/account/check",
                       {"email": email, "rememberLogin": True, "deviceId": device_id})
        if r.status_code == 200:
            raise RuntimeError(f"邮箱已注册: {email}")
        if r.status_code != 404:
            raise RuntimeError(f"account/check 异常: {r.status_code} {r.text[:200]}")

        self._nvgs("GET", "/external?order=Preferred")
        self._nvgs("GET", "/announcement?screen=EmailEntry&locale=zh-CN")
        self._nvgs("POST", "/log", {"type": "AccountCreateInitiated"})
        self.client.get(f"{self.nvgs_validator}/password/validation/policy")

        self._p("  [NVGS] 获取 hCaptcha 挑战...")
        r = self.client.get(
            f"{self.nvgs_validator}/validator/register",
            headers=self._nvgs_headers())
        challenge = r.json()["validation"]
        validator_key = challenge["key"]["token"]
        captcha_mode = challenge.get("captchaMode", "Visible")

        self._nvgs("GET", "/region/policy")
        r = self._nvgs("POST", "/email/check", {"email": email})
        if not r.json().get("hasDomainPolicy") is False and r.json().get("hasDomainPolicy") is not False:
            self._p(f"  [NVGS] email/check 返回: {r.text[:200]}")

        if LLM_TYPE == "gemini":
            self._p(f"  [验证] 本地 hcaptcha-challenger 求解（模型 {GEMINI_MODEL} @ Gemini）...")
        else:
            self._p(f"  [验证] 本地 hcaptcha-challenger 求解（模型 {OPENAI_MODEL} @ {OPENAI_BASE_URL}）...")
        response = self.captcha.solve()

        self._p("  [NVGS] 提交注册...")
        r = self._nvgs("POST", "/user/register", {
            "email": email,
            "password": password,
            "rememberLogin": True,
            "autoLogin": False,
            "deviceId": device_id,
            "validation": {
                "key": {"token": validator_key},
                "type": "CaptchaHCaptcha",
                "captchaMode": captcha_mode,
                "response": response,
            },
        })
        if r.status_code != 201:
            raise RuntimeError(f"注册失败: {r.status_code} {r.text[:300]}")
        self.key = r.json()["key"]
        self._p("  [NVGS] 注册成功")

        r = self._nvgs("POST", "/user/next")
        user_token = r.json()["values"]["user_token"]
        self.key = r.json()["key"]

        r = self._nvgs("POST", "/user/next")
        code = r.json()["values"]["code"]
        self.key = r.json()["key"]

        r = self._nvgs("GET", f"/resource?key={self.key}&code={code}&scope=profile_complete")
        return user_token

    # ---- 3. 邮箱验证 ----
    def email_verify(self, user_token):
        self._p("  [验证] 等待邮箱验证邮件并轮询...")
        last_wrong_at = 0.0
        for attempt in range(45):
            if attempt % 3 == 0:
                pin = self.mail.wait_for_pin(max_attempts=1, delay=1)
                if pin and time.time() - last_wrong_at > 5:
                    self._p(f"  [验证] 收到 PIN: {pin}")
                    r = self._nvgs("POST", "/user/email/verification/complete",
                                   {"pinCode": pin})
                    result = r.json().get("result")
                    if result == "Complete":
                        self._p("  [验证] 邮箱验证成功")
                        break
                    if result == "PinCodeInvalid":
                        self._p("  [验证] PIN 无效，重新请求验证邮件...")
                        self._nvgs("POST", "/user/requestverify")
                        last_wrong_at = time.time()
            r = self._nvgs("GET", "/user/email/verification/poll")
            if r.json().get("result") == "Complete":
                self._p("  [验证] 邮箱验证成功 (轮询确认)")
                break
            time.sleep(3)
        else:
            raise RuntimeError("邮箱验证超时")

    # ---- 4. profile / passkey ----
    def finish_profile(self):
        r = self._nvgs("POST", "/user/profile/complete", {"emailVerification": True})
        self.key = r.json()["key"]

        r = self._nvgs("POST", "/user/next")
        self.key = r.json()["key"]
        if r.json().get("page") == "PasskeyPromptSetup":
            r = self._nvgs("POST", "/passkey/setup/skip")
            self.key = r.json()["key"]

        r = self._nvgs("POST", "/user/next")
        self.key = r.json()["key"]
        return r.json()["externalUrl"]

    # ---- 5. 同意页 + 会话 ----
    def consent_and_session(self, external_url):
        r = self._walk_redirects(external_url)
        final_url = str(r.url)
        if "static-login.nvidia.com" in final_url or "/consent" in final_url:
            state = parse_qs(urlparse(final_url).query).get("state")
            if not state:
                raise RuntimeError(f"未从同意页提取 state: {final_url}")
            self._p("  [同意] 提交同意...")
            r = self.client.post(f"{self.login_nvidia}/callback/consent",
                                 data={"trackBehavioralData": "false",
                                       "opt_in": "false",
                                       "state": state[0]})
            if r.status_code not in (301, 302, 303):
                raise RuntimeError(f"同意提交失败: {r.status_code} {r.text[:200]}")
            session_url = str(httpx.URL(f"{self.login_nvidia}/callback/consent").join(
                r.headers["location"]))
        elif "/session" in final_url:
            session_url = final_url
        else:
            raise RuntimeError(f"回调异常，未进入同意页或 session: {final_url}")

        r = self._walk_redirects(session_url)
        final_url = str(r.url)
        q = parse_qs(urlparse(final_url).query)
        m = re.match(r"https://login\.nvgs\.nvidia\.(com|cn)/", final_url)
        if m and "key" in q:
            if m.group(1) == "cn":
                self._switch_nvgs_venue(".cn")
                self._p("  [OAuth] 会话落在 .cn 端点，NVGS 后续请求切换至 .cn")
            self.key = q["key"][0]
            self._p(f"  [OAuth] 第二轮 key 已获取 (venue={m.group(1)})")
            return True
        if "build.nvidia.com" in final_url:
            self._p("  [OAuth] 已直接登录（无需第二轮）")
            return False
        raise RuntimeError(f"会话兑换后未进入预期页面: {final_url}")

    # ---- 6. 自动登录 ----
    def auto_login(self, user_token):
        self._nvgs("POST", "/initialize/check",
                   {"browserMode": "Private", "passkeySupported": True})
        self._nvgs("POST", "/user/info/get", {"values": [user_token]})
        r = self._nvgs("POST", "/user/login", {"token": user_token})
        if r.status_code != 201:
            raise RuntimeError(f"自动登录失败: {r.status_code} {r.text[:200]}")
        self.key = r.json()["key"]
        r = self._nvgs("POST", "/user/next")
        self.key = r.json()["key"]
        return r.json()["externalUrl"]

    # ---- 7. NCA 创建 ----
    def create_nca(self, external_url, org_name):
        r = self._walk_redirects(external_url)
        final_url = str(r.url)
        if "select-account" not in final_url:
            raise RuntimeError(f"未进入 NCA 选择页: {final_url}")
        state = parse_qs(urlparse(final_url).query).get("state")
        if not state:
            raise RuntimeError(f"未从 select-account 提取 state: {final_url}")
        # state 由 login.nvidia.com 签发（consent 阶段），NCA 回调首选 .com；
        # .cn 仅作为回退（防御性保留，目前实测均 401）
        primary = "https://login.nvidia.com"
        other = "https://login.nvidia.cn"
        self._p(f"  [NCA] 创建组织 {org_name} ...")
        last_err = None
        for base in dict.fromkeys([primary, other]):
            r = self.client.post(f"{base}/callback/nca_picker",
                                 data={"state": state[0], "action": "create", "name": org_name})
            if r.status_code in (301, 302, 303):
                if base != primary:
                    self._p(f"  [NCA] {primary} 被拒，回退 {base} 成功")
                session_url = str(httpx.URL(f"{base}/callback/nca_picker").join(r.headers["location"]))
                r2 = self._walk_redirects(session_url)
                if "build.nvidia.com" not in str(r2.url):
                    raise RuntimeError(f"NCA 后未回到 build.nvidia.com: {r2.url}")
                self._p("  [NCA] 组织创建完成，已登录 build.nvidia.com")
                return
            last_err = f"{base}: {r.status_code} {r.text[:200]}"
            self._p(f"  [NCA] {last_err}")
        raise RuntimeError(f"nca_picker 失败（所有 host 均尝试）: {last_err}")

    # ---- 8. NGC API ----
    def ngc_create_key(self, org_name):
        ngc = "https://api.ngc.nvidia.com"
        h = {"Origin": "https://build.nvidia.com", "Referer": "https://build.nvidia.com/"}
        self.client.headers.update(h)

        r = self.client.get(f"{ngc}/v2/users/me")
        me = r.json()["user"]
        starfleet_id = me["starfleetId"]
        email = me["email"]
        org_id = me["roles"][0]["org"]["name"]
        self._p(f"  [NGC] user: {email} orgId: {org_id}")

        self.client.post(f"{ngc}/v3/users/me/verify", json={})
        r = self.client.patch(f"{ngc}/v2/user-profile", json={
            "userProfile": {
                "starfleetId": starfleet_id,
                "email": email,
                "country": "United States",
                "locale": "en_us",
                "orgInfo": [{"jobTitle": "engineer", "organization": ""}],
                "preferences": ["generative_ai"],
                "rdpSource": "api_catalog",
                "rdpLogin": True,
                "rdpLoginDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "devEmailable": False,
                "entEmailable": False,
            }
        })
        self._p(f"  [NGC] profile 补全: {r.json().get('requestStatus', {}).get('statusCode')}")

        for enablement in ("ai-foundations", "nim-dev"):
            for _ in range(2):
                r = self.client.post(f"{ngc}/v2/org/{org_id}/enablement/{enablement}", json={})
                status = r.json().get("requestStatus", {}).get("statusCode")
                if status == "SUCCESS":
                    self._p(f"  [NGC] enablement {enablement}: SUCCESS")
                    break
                time.sleep(3)

        products = []
        for _ in range(20):
            r = self.client.get(f"{ngc}/v2/users/me/subscriptions")
            products = r.json()["subscriptions"][0]["products"] if r.json().get("subscriptions") else []
            if "nim-dev" in products:
                break
            time.sleep(3)
        if "nim-dev" not in products:
            self._p(f"  [NGC] 订阅尚未同步: {products}")

        expiry = (datetime.now(timezone.utc) + timedelta(days=36500)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "name": f"Key-{random.randint(1000, 9999)}",
            "type": "AI_PLAYGROUNDS_KEY",
            "expiryDate": expiry,
            "policies": [{
                "product": "nv-cloud-functions",
                "scopes": ["invoke_function"],
                "resources": [{"id": "*", "type": "account-functions"}],
            }],
        }
        for attempt in range(3):
            r = self.client.post(f"{ngc}/v3/orgs/{org_id}/keys/type/AI_PLAYGROUNDS_KEY",
                                 json=payload)
            if r.status_code == 200:
                key = r.json().get("apiKey", {}).get("value")
                if key and key.startswith("nvapi-"):
                    self._p("  [NGC] ✅ API Key 已创建")
                    return key
            self._p(f"  [NGC] 建 Key 尝试 {attempt + 1}/3 失败: {r.status_code}")
            time.sleep(2 * (attempt + 1))
        raise RuntimeError("API Key 创建失败")

    # ---- 完整流程 ----
    def run(self):
        email = self.mail.get_email()
        if not email:
            raise RuntimeError("邮箱生成失败")
        self._email = email
        self.captcha.tag = f"[{email}]"
        password = generate_password()
        device_id = generate_device_id()
        org_name = "Org" + "".join(random.choices(string.ascii_uppercase, k=4))
        self._p(f"开始注册 (deviceId={device_id}, org={org_name})")

        self.oauth_entry(email)
        with browser_semaphore:
            user_token = self.nvgs_register(email, password, device_id)
        self.email_verify(user_token)
        external_url = self.finish_profile()
        need_round2 = self.consent_and_session(external_url)
        if need_round2:
            external_url2 = self.auto_login(user_token)
            self.create_nca(external_url2, org_name)
        api_key = self.ngc_create_key(org_name)
        save_to_csv(email, password, api_key)
        self._p(f"✅ 完成: {api_key}")
        return api_key


def register_process(thread_id, proxy, headless, email_address=None,
                     email_domain=None, email_prefix=""):
    if not email_address and email_domain:
        chars = string.ascii_lowercase + string.digits
        user = f"{email_prefix}{''.join(random.choices(chars, k=8))}" if email_prefix \
            else "".join(random.choices(chars, k=12))
        email_address = f"{user}@{email_domain}"
    reg = NvidiaHttpRegister(proxy=proxy, headless=headless, email_address=email_address)
    t = f"[{email_address}]" if email_address else f"[Thread-{thread_id}]"
    try:
        if not email_address:
            email_address = reg.mail.get_email()
            t = f"[{email_address}]"
        reg.run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"{t} ❌ 失败: {e}", flush=True)
    finally:
        reg.close()


def run_tasks(count, workers, proxy, headless, email_list=None,
              email_domain=None, email_prefix="", browser_concurrency=3):
    global browser_semaphore
    browser_limit = max(1, int(browser_concurrency))
    browser_semaphore = threading.Semaphore(browser_limit)
    print(f"=== 开始任务: 数量 {count}, 线程 {workers}, 浏览器 {browser_limit}, "
          f"代理 {proxy or '直连'}, headless={headless}"
          + (f", 自定义邮箱 {len(email_list)} 个" if email_list else "") + " ===")
    emails = list(email_list or [])
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = []
        for i in range(count):
            addr = emails[i] if emails and i < len(emails) else None
            futures.append(executor.submit(register_process, i + 1, proxy, headless, addr,
                                           email_domain=email_domain, email_prefix=email_prefix))
        for f in as_completed(futures):
            try:
                f.result()
            except Exception:
                pass
    print("=== 全部结束 ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NVIDIA build.nvidia.com 纯 HTTP 注册工具 "
                    "（hcaptcha-challenger + OpenAI 兼容格式视觉模型）")
    parser.add_argument("-c", "--count", type=int, default=1, help="注册数量 (默认: 1)")
    parser.add_argument("-t", "--threads", type=int, default=None,
                        help="并发线程数 (默认: =注册数量，配合 -b 浏览器槽位实现流水线)")
    parser.add_argument("-b", "--browser", type=int, default=3,
                        help="最大并发浏览器数（验证码求解槽位，默认: 3，控制内存峰值）")
    parser.add_argument("-p", "--proxy", type=str, default=None,
                        help="HTTP 代理 (例如: http://127.0.0.1:7890 或 socks5://...；境外机器可传 --no-proxy 或留空)")
    parser.add_argument("--no-proxy", action="store_true",
                        help="强制不使用代理（直连访问）")
    parser.add_argument("--openai-key", type=str, default=None,
                        help="OpenAI 兼容 API Key (默认读 OPENAI_API_KEY 环境变量)")
    parser.add_argument("--openai-base-url", type=str, default=None,
                        help="OpenAI 兼容端点 (默认读 OPENAI_BASE_URL，默认 "
                             "https://api.openai.com/v1)")
    parser.add_argument("--openai-model", type=str, default=None,
                        help="视觉模型名 (默认读 OPENAI_MODEL，默认 gpt-4o；"
                             "需支持图像输入，如 gpt-4o / gpt-4.1 / qwen-vl-max)")
    parser.add_argument("--thinking", action="store_true", default=None,
                        help="启用思考模式 (阿里云百炼等兼容端点 enable_thinking=true；"
                             "DeepSeek/MiniMax 推理模型可用)")
    parser.add_argument("--reasoning-effort", type=str, default=None,
                        help="思考强度 (low/high/max，配合 --thinking；默认读 config 或环境变量)")
    parser.add_argument("--headless", action="store_true", help="无头模式（识别率可能下降）")
    parser.add_argument("--email-provider", "-e", type=str, default=None,
                        help="邮箱服务商 (默认 tinyhost；可用: " + ", ".join(
                            [p for p in list_email_providers()]) + ")")
    parser.add_argument("--email", action="append", default=None,
                        help="自定义邮箱地址（可多次指定，每个地址对应一次注册；"
                             "需该服务商能收验证码，如 tinyhost 的 user@domain）")
    parser.add_argument("--email-file", type=str, default=None,
                        help="自定义邮箱列表文件，每行一个邮箱，空行/# 注释忽略")
    parser.add_argument("--email-domain", type=str, default=None,
                        help="固定邮箱域名，脚本自动生成 前缀+随机@域名（需该服务商托管，"
                             "如 tinyhost/freecustom 的域名；freecustom 会自动校验）")
    parser.add_argument("--email-user", type=str, default=None,
                        help="配合 --email-domain 使用，自定义本地部分前缀（默认 12 位随机）")
    args = parser.parse_args()

    if LLM_TYPE == "gemini":
        if not GEMINI_API_KEY:
            print("❌ type=gemini 需要 API Key：config.json llm.api_key 或环境变量 GEMINI_API_KEY")
            raise SystemExit(1)
        _install_gemini_provider()
        print(f"  [验证] LLM 后端: gemini (model={GEMINI_MODEL}"
              + (f", proxy={GEMINI_PROXY}" if GEMINI_PROXY else "") + ")")
    else:
        if args.openai_key:
            OPENAI_API_KEY = args.openai_key
        if args.openai_base_url:
            OPENAI_BASE_URL = args.openai_base_url
        if args.openai_model:
            OPENAI_MODEL = args.openai_model
        if args.thinking is not None:
            OPENAI_THINKING = args.thinking
        if args.reasoning_effort:
            OPENAI_REASONING_EFFORT = args.reasoning_effort.strip().lower()
        if not OPENAI_API_KEY:
            print("❌ 需要 API Key：--openai-key 或环境变量 OPENAI_API_KEY")
            raise SystemExit(1)
        _install_openai_provider()
        thinking_note = "，thinking=" + (OPENAI_REASONING_EFFORT or "on") if OPENAI_THINKING else ""
        print(f"  [验证] LLM 后端: openai (model={OPENAI_MODEL} @ {OPENAI_BASE_URL})"
              + thinking_note)

    # _install_custom_solvers()  # 禁用自定义求解器，走原库实现
    # _install_library_voting(votes=args.votes)  # 原库投票被禁用，走原库原生实现

    if args.email_provider:
        EMAIL_PROVIDER = args.email_provider

    email_list: list[str] = []
    if args.email:
        email_list.extend(e.strip() for e in args.email if e and e.strip())
    if args.email_file:
        with open(args.email_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                email_list.append(line.split()[0])
    email_list = list(dict.fromkeys(email_list))
    if email_list:
        print(f"  [邮箱] 使用 {len(email_list)} 个自定义邮箱，不足 {args.count} 的用随机补足")

    email_domain = None
    email_prefix = ""
    if args.email_domain:
        domain = str(args.email_domain).strip().lower().lstrip("@")
        if not domain or "@" in domain:
            print(f"❌ --email-domain 格式非法: {args.email_domain!r}")
            raise SystemExit(1)
        email_domain = domain
        email_prefix = (args.email_user or "").strip()
        print(f"  [邮箱] 固定域名 {domain}" + (f"，前缀 {email_prefix!r}" if email_prefix else "") +
              f"，每个注册任务开始前单独生成")

    if args.no_proxy:
        proxy = None
    elif args.proxy:
        proxy = args.proxy
    else:
        # 如果未显式传参，优先从环境变量读取，其次默认为本地代理
        proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "http://127.0.0.1:7890"
    threads = args.threads or args.count
    run_tasks(args.count, threads, proxy, args.headless, email_list,
              email_domain=email_domain, email_prefix=email_prefix,
              browser_concurrency=args.browser)

    # ---- 强制收尾：防止 curl_cffi / playwright 残留的非 daemon 线程挂住解释器退出 ----
    lingering = [t for t in threading.enumerate()
                 if t is not threading.main_thread() and not t.daemon and t.is_alive()]
    if lingering:
        print(f"  [退出] 发现 {len(lingering)} 个残留线程 {[t.name for t in lingering[:8]]}，强制结束进程")
        os._exit(0)
