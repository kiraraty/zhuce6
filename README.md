# zhuce6

## 项目介绍

`zhuce6` 是一个 ChatGPT 注册与治理仓库. 它把注册, cfmail 邮箱接入, backend 入库, 清理, 校验, 轮换, Dashboard 这些能力收口到统一入口 `main.py`.

当前主线支持三种运行组合:

- `lite + cfmail + register`
- `full + cpa`
- `full + sub2api`

仓库当前大约包含:

- `101` 个 tracked files
- `18` 个 `core/` 文件
- `16` 个 `ops/` 文件
- `15` 个 `platforms/` 文件
- `31` 个 `tests/` 文件

它主要做两类事情:

1. 注册: 通过 `cfmail` 等邮箱链路持续产出新账号.
2. 治理: 在 `full` 模式下对 backend 执行自动入库, 清理, 校验, 轮换与存活观测.

## 核心能力

- 统一入口: `main.py` 负责 `init / doctor / run / stop / status`
- 轻量注册: `lite + cfmail + register`
- 完整治理: `full + cpa` 或 `full + sub2api`
- 自动化配置: `init` 尽量自动推导 Cloudflare 资源, 包括 `account_id`, `zone_id`, `D1 database id`
- Dashboard: 提供 `/zhuce6` 概览页和 runtime / health API

## 环境要求

- Python 3.12+
- `uv`
- `git`
- `node` / `npm` / `npx`
- `sslocal` 或可用代理
- Cloudflare 凭据:
  - 从零部署 cfmail Worker: `Cloudflare API Token`
  - 复用已部署 Worker: `CF_AUTH_EMAIL + CF_AUTH_KEY + worker_domain`

## 仓库结构

- `main.py`: 统一 CLI 入口, 负责 `init / doctor / run / stop / status`
- `core/`: settings, doctor, setup wizard, cfmail, proxy, runtime 基础设施
- `ops/`: cleanup, validate, rotate, d1_cleanup 等治理任务
- `platforms/`: ChatGPT 注册链与平台适配
- `dashboard/`: `/zhuce6` 页面和 API
- `scripts/`: 独立脚本, 例如 cfmail setup
- `docs/`: 长期文档
- `tests/`: pytest 测试集

### 文件树

```text
zhuce6/
├── main.py
├── AGENTS.md
├── README.md
├── pyproject.toml
├── uv.lock
├── core/
├── ops/
├── platforms/
│   └── chatgpt/
├── dashboard/
├── scripts/
├── docs/
├── tests/
├── config/
└── vendor/
```

其中最重要的几层是:

- `main.py`: 统一入口, 新用户先看这里对应的 CLI 主线.
- `core/`: 配置, setup wizard, doctor, cfmail, runtime 基础设施.
- `ops/`: `cleanup / validate / rotate / d1_cleanup` 等治理任务.
- `platforms/chatgpt/`: 注册链, token 文件写入, backend 对接.
- `dashboard/`: `/zhuce6` 页面与 API.

## 快速开始

### 1. 首次配置

```bash
uv run python main.py init
```

### 2. 安装依赖并检查环境

```bash
uv run python main.py doctor --fix
```

### 3. 启动

轻量模式:

```bash
uv run python main.py --mode lite
```

完整模式:

```bash
uv run python main.py --mode full
```

如果你只记四条命令, 就记:

```bash
uv run python main.py init
uv run python main.py doctor --fix
uv run python main.py --mode lite
uv run python main.py --mode full
```

## 文档路由

如果你是第一次接手这个项目, 或者是新的 agent, 按下面顺序读:

1. `README.md`: 先理解项目主线, 正确入口, 模式组合.
2. `docs/TROUBLESHOOTING.md`: 启动失败, cfmail 异常, runtime 异常时先看这里.
3. `docs/CONFIG_REFERENCE.md`: 不知道某个环境变量或配置项是什么意思时看这里.
4. `docs/CODEX_PROVIDER_PROTOCOL_NOTES.md`: 需要理解 provider 侧行为与约束时看这里.
5. `main.py`: 需要看真实运行入口, 生命周期命令和 runtime 组装时再读源码.

### 遇到问题先看什么

- 配不起来, `init` / `doctor --fix` 报错:
  - 先看 `docs/TROUBLESHOOTING.md`
- 不确定配置项怎么填:
  - 看 `docs/CONFIG_REFERENCE.md`
- 想确认正确启动命令:
  - 回到本 README 的"快速开始"
- 想确认 runtime 到底启用了哪些后台任务:
  - 看 `main.py`

## 这四步分别做什么

### 1. `uv run python main.py init`

交互式配置向导, 负责生成或更新当前 `.env` 中的主线配置.

向导会覆盖这些场景:

- 模式选择: `lite` / `full`
- `full` 后端选择: `cpa` / `sub2api`
- 代理输入: 直接代理 URL 或 Clash YAML
- 邮箱 provider: `cfmail`

### 2. `uv run python main.py doctor --fix`

环境检查与依赖补齐.

当前行为:

- 自动执行 `uv sync`, 安装 Python 依赖.
- 如果存在 cfmail worker 目录, 自动执行 `npm install --no-fund --no-audit`.
- 汇总仍需人工安装的外部依赖, 例如 `git`, `node`, `npx`, `sslocal`.
- 分别判断以下路径是否 ready:
  - `lite`
  - `full(cpa)`
  - `full(sub2api)`

### 3. `uv run python main.py --mode lite`

轻量启动. 适合 `cfmail + register` 主线.

### 4. `uv run python main.py --mode full`

完整启动. 会根据 `.env` 中的 `backend` 走不同后端:

- `ZHUCE6_BACKEND=cpa`
- `ZHUCE6_BACKEND=sub2api`

`full` 的启动语义固定为:

- 启动 register
- 启动 `/zhuce6` dashboard
- 启动 rotate
- 同时按配置启动 cleanup / validate / d1_cleanup

## 三条主线

### A. lite + cfmail + register

适用场景:

- 只跑注册链.
- 暂时不接外部后端治理.

最小路径:

```bash
uv run python main.py init
uv run python main.py doctor --fix
uv run python main.py --mode lite
```

### B. full + cpa

适用场景:

- 继续使用 CPA Management API.
- 需要 cleanup / validate / rotate / register 全链路.
- 需要以 CPA 作为运行时主池, 但仍保留本地 `pool/*.json` 持久化备份.

`init` 中选择:

- mode = `full`
- backend = `cpa`

### C. full + sub2api

适用场景:

- 后端治理走 sub2api Admin API.
- 不再把 CPA 当成唯一 full 路径.

`init` 中选择:

- mode = `full`
- backend = `sub2api`

## cfmail 配置现在怎么走

`cfmail` 已并入 `uv run python main.py init`.

### 最小输入

向导支持两种 Cloudflare 凭据输入路径:

- `Cloudflare API Token` + `zone_name`: 适用于从零部署 cfmail Worker.
- `CF_AUTH_EMAIL` + `CF_AUTH_KEY` + `zone_name` + `worker_domain`: 适用于复用一个已经部署好的 cfmail Worker.

通常还会让你确认:

- `worker_name`
- `email_domain`
- `admin_password`

### 向导会自动处理

向导会调用 `scripts/setup_cfmail.py` 的自动化能力, 尽量自动完成:

- 校验 Cloudflare 凭据
- 解析 `zone_id`
- 获取 `account_id`
- 创建或复用 D1
- 在提供 API Token 时自动推导 worker domain
- 在未提供 API Token 时要求显式输入已部署的 `worker_domain`
- 生成:
  - `config/cfmail_accounts.json`
  - `config/cfmail_provision.env`
- 如果复用的 `email_domain` 已失效, 向导会在保存阶段尝试自动轮换到新的可用子域名

注意:

- `scripts/setup_cfmail.py` 的完整 Worker 部署链依赖 `wrangler`, 而 `wrangler` 在非交互环境下要求 `Cloudflare API Token`.
- 所以如果你只有 `CF_AUTH_EMAIL + CF_AUTH_KEY`, 初次配置时必须填写一个已经部署完成的 `worker_domain`, 这样 `init -> doctor --fix -> run` 才能直接跑通.

如果发现已有 cfmail 配置, 向导会优先提示你复用现有配置, 不会静默重置.

### cfmail 运行时约束

- 示例 zone 可以是 `.example.com`.
- active cfmail 域名应保持为当前唯一 enabled 子域名, 旧 auto 子域名在轮换后会直接从配置中删除.
- 旧子域名被 OpenAI ban 后, register 线程会通过 `CfmailProvisioner.rotate_active_domain()` 自动切换到新的 `auto*.example.com` 子域名.
- `CfMailMailbox` 与 register loop 共享同一套 `CfmailAccountManager` cooldown 视图, 因此 mailbox 阶段的 cooldown 与 rotation 检测是联动的.
- `ZHUCE6_D1_DATABASE_ID` 由 cfmail 向导自动写入. 如果为空, `d1_cleanup` 会直接跳过, 不会误打到某个硬编码数据库.

### cfmail 自动轮换与 cleanup

`CfmailProvisioner.rotate_active_domain()` 的主链是:

1. 创建 email routing rule
2. 创建 DNS 记录
3. 更新 worker domain bindings
4. smoke test
5. 切换 active domain
6. best-effort cleanup old auto-domain artifacts

这意味着:

- 如果所有 enabled cfmail account 都进入 cooldown, register worker 会主动触发 rotation, 不会一直卡死在 mailbox 阶段.
- 旧 auto 子域名在切换后会立刻从 `config/cfmail_accounts.json` 删除, 其 Cloudflare DNS / routing 残留资源再做 best-effort cleanup.
- cleanup 属于 best-effort. 遇到 Cloudflare DNS read-only / code `1043` 这类旧资源删除失败时, 不应回滚已经完成的新域名切换.
- 排障时应优先看 `rotate_active_domain()` 的 `success` 与 `new_domain`, 以及 `config/cfmail_accounts.json` 中 active 域名是否已经变化.

## pool 与 CPA 现在的关系

当前 register 入池链已经收敛为:

1. 注册成功
2. 原子写入本地 `pool/*.json` 作为持久化备份
3. 立即同步到 CPA backend

这意味着:

- `pool` 不是候选池, 也不是任何晋升前置门.
- `pool` 的职责是备份与灾备恢复.
- CPA 是运行时主池, Dashboard 与治理任务优先看 CPA 实际 inventory.

### backend 清理与回灌

- `rotate`, `validate`, `cleanup` 删除失效账号时, 会双删 backend + pool.
- `rotate` 只删除 `401 invalidated` 账号, `429 usage_limit_reached` 账号保留在主池里等待下一个额度窗口.
- 如果 CPA 因重启丢失账号, runtime reconcile 会把 `pool/*.json` 中缺失的账号回灌到 CPA.
- 如果 backend 里有账号但本地没有备份, runtime reconcile 也会补写本地 pool 备份.

### 手工部署 cfmail Worker

如果你需要手工补 cfmail Worker 部署, 优先使用:

```bash
uv run python scripts/setup_cfmail.py --help

# 从零部署 cfmail Worker 时, 请优先使用 API Token
uv run python scripts/setup_cfmail.py --api-token <token> --zone-name example.com
```

执行 wrangler 时优先使用 `npx wrangler`, 不要求全局安装.

## 平台说明

## Windows

推荐:

1. 安装 Python 3.11+.
2. 安装 `uv`.
3. 安装 Node.js 20+.
4. 在 PowerShell 7 中运行:

```powershell
uv run python main.py init
uv run python main.py doctor --fix
uv run python main.py --mode lite
```

如果 `doctor --fix` 提示缺少 `git` 或 `node`, 先补齐再继续.

## Linux

推荐先保证这些命令可用:

- `python3`
- `uv`
- `git`
- `node`
- `npm`
- `npx`

然后执行:

```bash
uv run python main.py init
uv run python main.py doctor --fix
uv run python main.py --mode full
```

## WSL

WSL 路径与 Linux 一致. 推荐把仓库与依赖都保留在 WSL 内运行.

如果你在 WSL 中使用 Clash YAML 模式, 还需要保证 `sslocal` 可用. `doctor --fix` 会明确提示是否缺少这一类外部依赖.

## 常用配置键

### 运行与后端

- `ZHUCE6_BACKEND`: `cpa` 或 `sub2api`
- `ZHUCE6_DASHBOARD_ALLOWED_ORIGINS`: 允许跨域读取 `/api/runtime` 与 `/api/summary` 的 Origin 列表, 逗号分隔
- `ZHUCE6_CPA_MANAGEMENT_BASE_URL`
- `ZHUCE6_CPA_MANAGEMENT_KEY`
- `ZHUCE6_SUB2API_BASE_URL`
- `ZHUCE6_SUB2API_API_KEY`
- `ZHUCE6_SUB2API_ADMIN_EMAIL`
- `ZHUCE6_SUB2API_ADMIN_PASSWORD`

### cfmail

- `ZHUCE6_CFMAIL_CONFIG_PATH`
- `ZHUCE6_CFMAIL_ENV_FILE`
- `ZHUCE6_CFMAIL_API_TOKEN`
- `ZHUCE6_CFMAIL_CF_ACCOUNT_ID`
- `ZHUCE6_CFMAIL_CF_ZONE_ID`
- `ZHUCE6_CFMAIL_WORKER_NAME`
- `ZHUCE6_CFMAIL_ZONE_NAME`
- `ZHUCE6_CFMAIL_ROTATION_WINDOW`
- `ZHUCE6_CFMAIL_ROTATION_BLACKLIST_THRESHOLD`
- `ZHUCE6_CFMAIL_ROTATION_COOLDOWN_SECONDS`

### 代理

- `ZHUCE6_ENABLE_PROXY_POOL`
- `ZHUCE6_PROXY_POOL_DIRECT_URLS`
- `ZHUCE6_PROXY_POOL_CONFIG`
- `ZHUCE6_REGISTER_PROXY`

完整变量说明见 `docs/CONFIG_REFERENCE.md` 与 `.env.example`.

## Dashboard 与 API

启动后可访问:

- `GET /zhuce6`
- `GET /api/summary`
- `GET /api/runtime`
- `GET /api/health/dependencies`

建议先设置:

```bash
export ZHUCE6_BASE_URL="http://<dashboard-host>:<dashboard-port>"
```

然后查看:

```bash
curl -sS "$ZHUCE6_BASE_URL/api/runtime" | python3 -m json.tool
curl -sS "$ZHUCE6_BASE_URL/api/health/dependencies" | python3 -m json.tool
```

## backend 说明

仓库主线是 backend API 驱动:

- `backend=cpa` => CPA Management API
- `backend=sub2api` => Sub2API Admin API
- 统一入口始终是 `main.py init / doctor / run`

## 开发与验证

```bash
PYTHONPATH=. uv run pytest -q -s
```

如果需要补跑重点用例:

```bash
PYTHONPATH=. uv run pytest \
  tests/test_setup_wizard.py \
  tests/test_setup_cfmail.py \
  tests/test_setup_wizard_proxy_validation.py \
  tests/test_doctor.py \
  tests/test_main_cli.py \
  tests/test_sub2api_adapter.py \
  tests/test_sub2api_client.py \
  tests/test_main_summary.py \
  -q -s
```
