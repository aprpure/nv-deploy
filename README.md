# nvinda — NVIDIA build.nvidia.com 注册工具（uv 部署版）

纯 HTTP 流程自动注册 [build.nvidia.com](https://build.nvidia.com) 账号并批量获取 `nvapi-` API Key。
hCaptcha 由 [hcaptcha-challenger](https://pypi.org/project/hcaptcha-challenger/)（PyPI 库）+
OpenAI 兼容格式视觉模型（或 Gemini）求解——OpenAI 兼容补丁已内置于主脚本，安装后开箱即用；
邮箱验证码由 40+ 临时邮箱服务商接码。

## 目录结构

```
nv-deploy/
├── pyproject.toml              # uv 项目定义（依赖均来自 PyPI）
├── uv.lock                     # 锁定的依赖版本（可复现部署）
├── uv.toml                     # uv 配置（缓存目录 .uv-cache）
├── config.example.json         # LLM 配置模板（复制为 config.json 后填写）
├── register_http_hc_openai.py  # 主脚本（唯一入口，含 OpenAI provider 补丁）
└── email_clients/              # 临时邮箱服务商客户端集合
```

## 环境要求

- [uv](https://docs.astral.sh/uv/)（Python 3.14 由 uv 自动下载管理）
- 可访问 OpenAI 兼容视觉模型的 API Key（需支持图像输入，如 `gpt-4o` / `qwen-vl-max` / Gemini）
- HTTP 代理（默认 `http://127.0.0.1:7890`，可用 `-p` 覆盖；支持 `socks5://`）

## 部署步骤

```powershell
# 1. 安装依赖（自动创建 .venv、按需下载 Python 3.14，按 uv.lock 复现版本）
uv sync

# 2. 安装 Playwright Chromium（hCaptcha 求解使用）
uv run playwright install chromium

# 3. 准备配置：从模板复制并填入你的 API Key
Copy-Item config.example.json config.json
# 编辑 config.json，填写 llm.openai_api_key / openai_base_url / openai_model 等
```

> 密钥也可以改用环境变量 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`
> 或命令行参数传入，无需写进 config.json。

## OpenAI 兼容补丁说明

hcaptcha-challenger 原生面向 Gemini，主脚本内置 `_install_openai_provider()`：

- 启动时将 `Reasoner._create_default_provider` 替换为脚本内的 `OpenAIProvider`
  （OpenAI Chat Completions 兼容格式，支持 base_url / proxy / json_mode / 重试），
  使库内所有验证码求解工具（二值分类、空间点/框/路径推理、挑战路由）全部走你的
  OpenAI 兼容端点与模型。
- 该补丁为运行时 monkeypatch，升级 hcaptcha-challenger 版本无需改动部署文件；
  若 `llm.type` 设为 `gemini` 则自动回退原生 GeminiProvider（支持自定义 base_url / proxy）。

## 使用方法

所有命令均通过 `uv run` 在项目虚拟环境中执行：

```powershell
# 最简用法：注册 1 个账号（默认邮箱服务商 tinyhost）
uv run python register_http_hc_openai.py --openai-key sk-xxx --openai-model gpt-4o

# 注册 5 个，8 线程并发，浏览器求解槽位 3
uv run python register_http_hc_openai.py -c 5 -t 8 -b 3 -p http://127.0.0.1:7890

# 无头模式 + 指定邮箱服务商
uv run python register_http_hc_openai.py -c 2 --headless -e emailnator

# 自定义固定域名邮箱（前缀 + 随机用户名@your.domain）
uv run python register_http_hc_openai.py -c 3 --email-domain your.domain --email-user pre

# 从文件读取自定义邮箱列表（每行一个，# 注释）
uv run python register_http_hc_openai.py -c 10 --email-file emails.txt
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `-c, --count` | 注册数量（默认 1） |
| `-t, --threads` | 并发线程数（默认 = 注册数量） |
| `-b, --browser` | 最大并发浏览器/验证码槽位（默认 3，控制内存峰值） |
| `-p, --proxy` | HTTP 代理（默认 `http://127.0.0.1:7890`） |
| `--headless` | 无头模式（识别率可能下降） |
| `-e, --email-provider` | 临时邮箱服务商（默认 tinyhost） |
| `--openai-key / --openai-base-url / --openai-model` | 视觉模型配置（也可走 config.json / 环境变量） |
| `--thinking / --reasoning-effort` | 推理模型思考模式开关与强度 |
| `--email / --email-file` | 自定义邮箱地址 / 列表文件 |
| `--email-domain / --email-user` | 固定域名自动生成邮箱 |

完整参数：`uv run python register_http_hc_openai.py --help`

## 输出

注册成功后结果追加写入工作目录下的 `nvidia_keys.csv`
（列：`email,password,api_key,created_at`），日志实时打印到控制台。

## 常见问题

- **Linux 服务器报 `ImportError: libGL.so.1`**：无桌面系统缺少 OpenCV 的系统依赖，
  安装即可（Python 环境不用动）：
  ```bash
  # Debian / Ubuntu
  apt-get install -y libgl1 libglib2.0-0
  # CentOS / RHEL
  yum install -y mesa-libGL glib2
  ```
- **`playwright` 浏览器未安装**：执行 `uv run playwright install chromium`
  （Linux 还可能需要 `uv run playwright install-deps chromium` 补齐系统库）。
- **验证码求解失败率高**：换更强的视觉模型；无头模式识别率低于有头模式。
- **代理不通**：确认 `-p` 指向的代理端口可用，NVIDIA 与 LLM 端点均可能需要代理。
- **升级依赖**：`uv sync` 按 `uv.lock` 恢复；`uv lock --upgrade` 后再 `uv sync` 升级。

## 许可说明

依赖的 hcaptcha-challenger 遵循 GPL-3.0-or-later，仅用于学习研究，请遵守目标站点服务条款。
