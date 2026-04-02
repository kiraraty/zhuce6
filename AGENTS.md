# zhuce6 Project AGENTS

## 项目定位

- `zhuce6` 是自用 ChatGPT 注册与治理仓库.
- 当前主线是单池 + backend API.
- `main.py` 是统一入口, 负责 `init / doctor / run / stop / status`, FastAPI 路由, register loop, 以及 `cleanup / validate / rotate / register` 后台任务装配.
- 主 Dashboard 路径是 `GET /zhuce6`, 页面文件位于 `dashboard/zhuce6.html`.
- 支持两类 full backend:
  - `cpa`
  - `sub2api`
- `cfmail` 是默认 mailbox provider 主线.

## 首先阅读什么

1. `README.md`
2. `docs/TROUBLESHOOTING.md`
3. `docs/CONFIG_REFERENCE.md`
4. `main.py`
5. 再按任务进入 `core/`, `ops/`, `platforms/chatgpt/`, `dashboard/`

## 正确入口认知

所有实施 agent 都应从以下主线理解项目:

```bash
uv run python main.py init
uv run python main.py doctor --fix
uv run python main.py --mode lite
# 或
uv run python main.py --mode full
```

### 主线组合

- `lite + cfmail + register`
- `full + cpa`
- `full + sub2api`

### 不要再按旧叙事理解

以下内容不再是项目主线认知:

- 历史两阶段池叙事
- 旧后台同步任务
- 旧归档主线
- 容器优先入口认知
- CPA-only 的 full 模式理解

## 架构说明

### 核心模块职责

- `main.py`: 统一 CLI 入口, FastAPI 应用创建, runtime mode 切换, 生命周期命令.
- `core/`: settings, doctor, setup wizard, cfmail, proxy pool, mailbox 抽象, 路径与共享基础设施.
- `platforms/chatgpt/`: ChatGPT 注册链, HTTP / OAuth 客户端, pool 写入与平台适配.
- `ops/`: `cleanup`, `validate`, `rotate` 与相关治理逻辑.
- `dashboard/`: `/zhuce6` 页面与 API 输出.
- `scripts/`: 独立脚本, 例如 cfmail 自动化与辅助工具.
- `tests/`: pytest 测试集.

### 运行模式说明

- `full`: 启动 Dashboard / API, 并按配置启用治理任务与 register.
- `dashboard`: 只启动 Dashboard / API.
- `lite`: 轻量运行模式, 适合只跑注册主线.
- `register-loop`: 只运行持续注册循环.
- `burst-scheduler`: 只运行批次调度注册.

## 实施规则

- 文档与代码都要围绕唯一正确入口: `init -> doctor --fix -> run`.
- `full` 必须显式区分 `backend=cpa` 与 `backend=sub2api`.
- `cfmail` 配置优先最小输入, 自动推导 Cloudflare 资源.
- 不要把临时排障口径写回长期文档.
- 不要把外部部署背景重新写成 repo 主逻辑.

## 验证规则

```bash
cd <PROJECT_ROOT>

export ZHUCE6_BASE_URL="http://<dashboard-host>:<dashboard-port>"

PYTHONPATH=. uv run pytest -q -s
curl -sS "$ZHUCE6_BASE_URL/api/summary" | python3 -m json.tool | head -n 80
curl -sS "$ZHUCE6_BASE_URL/api/runtime" | python3 -m json.tool | head -n 80
curl -sS "$ZHUCE6_BASE_URL/api/health/dependencies" | python3 -m json.tool | head -n 80
python3 - <<'PY'
import json
import os
import urllib.request

base_url = os.environ["ZHUCE6_BASE_URL"].rstrip("/")
with urllib.request.urlopen(f"{base_url}/api/runtime") as resp:
    runtime = json.load(resp)
registered = set(runtime.get("registered_tasks") or [])
assert ("sy" + "nc") not in registered, sorted(registered)
assert "register" in registered or runtime.get("runtime_mode") in {"dashboard"}, runtime
print(sorted(registered))
PY
curl -sS "$ZHUCE6_BASE_URL/zhuce6" | head
```

## 文档维护范围

优先维护:

- `README.md`
- `AGENTS.md`
- `docs/TROUBLESHOOTING.md`
- `docs/CONFIG_REFERENCE.md`
- `docs/CODEX_PROVIDER_PROTOCOL_NOTES.md`

阶段性说明若已被长期文档吸收, 应及时清理.
