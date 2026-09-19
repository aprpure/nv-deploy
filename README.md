# nv-deploy

## 目录结构

```
nv-deploy/
├── pyproject.toml              # uv 项目定义（依赖均来自 PyPI）
├── uv.lock                     # 锁定的依赖版本（可复现部署）
├── uv.toml                     # uv 配置（缓存目录 .uv-cache）
├── config.example.json         # LLM 配置模板（复制为 config.json 后填写）
├── register_http_hc_openai.py  # 主脚本（唯一入口）
└── email_clients/              # 临时邮箱服务商客户端集合
```

## 环境要求

- [uv](https://docs.astral.sh/uv/)（Python 3.14 由 uv 自动下载管理）
- 一个支持图像输入的 OpenAI 兼容视觉模型 API Key（如 `gpt-4o` / `qwen-vl-max`）
- 网络能直连或经代理访问 NVIDIA 与 LLM 端点

## 部署步骤

```bash
# 1. 安装依赖（自动创建 .venv、按需下载 Python 3.14，按 uv.lock 复现版本）
uv sync

# 2. 安装 Playwright Chromium（Linux 用 --with-deps 补齐系统库）
uv run playwright install chromium
uv run playwright install-deps chromium   # 仅 Linux 需要

# 3. 准备配置：从模板复制并填入你的 API Key
cp config.example.json config.json
# 编辑 config.json，填写 llm.openai_api_key / openai_base_url / openai_model 等
```

> 密钥也可以改用环境变量 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`
> 或命令行参数传入，无需写进 config.json。

## 使用方法

所有命令均通过 `uv run` 在项目虚拟环境中执行：

```bash
# 最简用法：注册 1 个账号（默认邮箱服务商 tinyhost）
uv run python register_http_hc_openai.py --openai-key sk-xxx --openai-model gpt-4o

# 注册 5 个，8 线程并发，浏览器求解槽位 3
uv run python register_http_hc_openai.py -c 5 -t 8 -b 3 -p http://127.0.0.1:7890

# 无头模式 + 指定邮箱服务商
uv run python register_http_hc_openai.py -c 2 --headless -e boomlify

# 不使用代理（直连，适合境外服务器）
uv run python register_http_hc_openai.py -c 2 --headless --no-proxy

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
| `-p, --proxy` | 代理地址（默认 `http://127.0.0.1:7890`；支持 `socks5://`） |
| `--no-proxy` | 不使用代理，直连访问 |
| `--headless` | 无头模式（识别率可能下降） |
| `-e, --email-provider` | 邮箱服务商（可用：tinyhost / boomlify / freecustom） |
| `--openai-key / --openai-base-url / --openai-model` | 视觉模型配置（也可走 config.json / 环境变量） |
| `--thinking / --reasoning-effort` | 推理模型思考模式开关与强度 |
| `--email / --email-file` | 自定义邮箱地址 / 列表文件 |
| `--email-domain / --email-user` | 固定域名自动生成邮箱 |

完整参数：`uv run python register_http_hc_openai.py --help`

## 输出

注册成功后结果追加写入工作目录下的 `nvidia_keys.csv`
（列：`email,password,api_key,created_at`），日志实时打印到控制台。

## 常见问题

- **Linux 服务器报 `ImportError: libGL.so.1`**：无桌面系统缺少 OpenCV 的系统依赖：
  ```bash
  # Debian / Ubuntu
  apt-get install -y libgl1 libglib2.0-0
  # CentOS / RHEL
  yum install -y mesa-libGL glib2
  ```
- **`playwright` 浏览器未安装**：执行 `uv run playwright install chromium`
  （Linux 还可能需要 `uv run playwright install-deps chromium` 补齐系统库）。
- **`Connection refused`**：默认代理 `http://127.0.0.1:7890` 在你机器上不存在，
  本机没有代理就用 `--no-proxy` 直连，或用 `-p` 指向你的代理。
- **验证码求解失败率高**：换更强的视觉模型；无头模式识别率低于有头模式。
- **升级依赖**：`uv sync` 按 `uv.lock` 恢复；`uv lock --upgrade` 后再 `uv sync` 升级。

## 许可说明

依赖的 hcaptcha-challenger 遵循 GPL-3.0-or-later，仅用于学习研究，请遵守目标站点服务条款。
