# zhuce6 Configuration Reference

优先入口不是手改配置, 而是:

```bash
uv run python main.py init
uv run python main.py doctor --fix
uv run python main.py --mode lite
# 或
uv run python main.py --mode full
```

`.env.example` 是完整模板. 本文只整理主线变量.

## 1. 主线组合

### lite + cfmail + register

关键变量:

- `ZHUCE6_REGISTER_MAIL_PROVIDER=cfmail`
- `ZHUCE6_REGISTER_PROXY`
- `ZHUCE6_CFMAIL_API_TOKEN`
- `ZHUCE6_CFMAIL_CF_ACCOUNT_ID`
- `ZHUCE6_CFMAIL_CF_ZONE_ID`
- `ZHUCE6_CFMAIL_WORKER_NAME`
- `ZHUCE6_CFMAIL_ZONE_NAME`

### full + cpa

关键变量:

- `ZHUCE6_BACKEND=cpa`
- `ZHUCE6_CPA_MANAGEMENT_BASE_URL`
- `ZHUCE6_CPA_MANAGEMENT_KEY`

### full + sub2api

关键变量:

- `ZHUCE6_BACKEND=sub2api`
- `ZHUCE6_SUB2API_BASE_URL`
- 认证二选一:
  - `ZHUCE6_SUB2API_API_KEY`
  - `ZHUCE6_SUB2API_ADMIN_EMAIL` + `ZHUCE6_SUB2API_ADMIN_PASSWORD`

## 2. 核心服务与路径

| 变量 | 作用 | 常见值 |
| --- | --- | --- |
| `ZHUCE6_HOST` | Dashboard 监听地址 | `127.0.0.1` |
| `ZHUCE6_PORT` | Dashboard 监听端口 | `8000` |
| `ZHUCE6_DASHBOARD_ALLOWED_ORIGINS` | 允许跨域读取 runtime/summary 的 Origin 列表 | 逗号分隔 URL |
| `ZHUCE6_CONFIG_DIR` | 配置目录 | `<project>/config` |
| `ZHUCE6_STATE_DIR` | 状态目录 | `<project>/state` |
| `ZHUCE6_LOG_DIR` | 日志目录 | `<project>/logs` |
| `ZHUCE6_POOL_DIR` | 本地账号池目录 | `<project>/pool` |
| `ZHUCE6_CFMAIL_CONFIG_PATH` | cfmail accounts JSON | `<config>/cfmail_accounts.json` |
| `ZHUCE6_CFMAIL_ENV_FILE` | cfmail provision env | `<config>/cfmail_provision.env` |

## 3. backend 与治理任务

| 变量 | 作用 | 说明 |
| --- | --- | --- |
| `ZHUCE6_BACKEND` | full 模式后端 | `cpa` 或 `sub2api` |
| `ZHUCE6_CLEANUP_ENABLED` | cleanup 开关 | `true` / `false` |
| `ZHUCE6_VALIDATE_ENABLED` | validate 开关 | `true` / `false` |
| `ZHUCE6_ROTATE_ENABLED` | rotate 开关 | `true` / `false` |
| `ZHUCE6_VALIDATE_SCOPE` | validate 范围 | `all` 或 `used` |
| `ZHUCE6_ROTATE_INTERVAL` | rotate 周期秒数 | 按需调整 |
| `ZHUCE6_ROTATE_PROBE_WORKERS` | rotate 并发 | 正整数 |

### backend 运行时语义

- `pool/*.json` 是持久化备份, 不是候选池.
- backend inventory 才是运行时主池.
- register 的成功语义等于"已经成功写入 backend 主池". 单次上游创建成功但 backend 同步失败, 记为注册失败.
- pool 文件中的 `cpa_sync_status` 只保留为排障字段, 不再代表单独的业务阶段.
- `validate`, `rotate`, `cleanup` 删除账号时会双删 backend + pool.
- `rotate` 只删除 `401 invalidated`, `429 usage_limit_reached` 保留.
- `cpa_runtime_reconcile` 负责在 backend 与 pool 漂移时做双向补齐.

### CPA 变量

| 变量 | 作用 |
| --- | --- |
| `ZHUCE6_CPA_MANAGEMENT_BASE_URL` | CPA Management API 基础地址 |
| `ZHUCE6_CPA_MANAGEMENT_KEY` | CPA Management API Key |
| `ZHUCE6_CPA_RUNTIME_RECONCILE_ENABLED` | 是否启用 backend 与 pool 双向 reconcile |
| `ZHUCE6_CPA_RUNTIME_RECONCILE_COOLDOWN_SECONDS` | drift 观测 cooldown |
| `ZHUCE6_CPA_RUNTIME_RECONCILE_RESTART_ENABLED` | 兼容保留字段, API-only 模式下不会本地重启 |

### sub2api 变量

| 变量 | 作用 |
| --- | --- |
| `ZHUCE6_SUB2API_BASE_URL` | sub2api Admin API 地址 |
| `ZHUCE6_SUB2API_API_KEY` | API Key 认证 |
| `ZHUCE6_SUB2API_ADMIN_EMAIL` | 管理员邮箱认证 |
| `ZHUCE6_SUB2API_ADMIN_PASSWORD` | 管理员密码认证 |

## 4. cfmail

### 最小输入

对于新配置, `init` 向导支持两条 cfmail 初始化路径:

- 从零部署 cfmail Worker: `Cloudflare API Token + zone_name`
- 复用已部署 cfmail Worker: `CF_AUTH_EMAIL + CF_AUTH_KEY + zone_name + worker_domain`

### 自动推导结果

向导会尽量自动生成:

- `account_id`
- `zone_id`
- `ZHUCE6_D1_DATABASE_ID`
- 在 API Token 路径下自动推导 worker domain
- `config/cfmail_accounts.json`
- `config/cfmail_provision.env`

如果没有 API Token, 向导不会假设可以替你完成首次 Worker 部署, 而是要求显式填写一个已经可用的 `worker_domain`.
如果复用的 `email_domain` 已经失效, 向导会在保存阶段尝试自动轮换到新的可用子域名.

### 运行时必需字段

| 变量 | 作用 |
| --- | --- |
| `ZHUCE6_CFMAIL_API_TOKEN` | Cloudflare API Token. 从零部署 cfmail Worker 时必需, `wrangler` 非交互部署也依赖它 |
| `ZHUCE6_CFMAIL_CF_AUTH_EMAIL` | Cloudflare 认证邮箱 |
| `ZHUCE6_CFMAIL_CF_AUTH_KEY` | Cloudflare Global API Key |
| `ZHUCE6_CFMAIL_CF_ACCOUNT_ID` | Cloudflare Account ID |
| `ZHUCE6_CFMAIL_CF_ZONE_ID` | Cloudflare Zone ID |
| `ZHUCE6_CFMAIL_WORKER_NAME` | Worker 名称 |
| `ZHUCE6_CFMAIL_ZONE_NAME` | Zone 名称 |

补充:

- `scripts/setup_cfmail.py` 的完整 Worker 部署链依赖 `wrangler`.
- 在非交互环境下, `wrangler` 要求 `Cloudflare API Token`.
- 所以只有 `CF_AUTH_EMAIL + CF_AUTH_KEY` 时, 正确用法是复用一个已部署的 `worker_domain`, 而不是期待脚本自动完成首次 Worker 部署.

### 常见调优字段

| 变量 | 作用 |
| --- | --- |
| `ZHUCE6_CFMAIL_MAIL_LIST_LIMIT` | inbox 拉取列表上限 |
| `ZHUCE6_CFMAIL_ROTATION_WINDOW` | 域名轮换观测窗口 |
| `ZHUCE6_CFMAIL_ROTATION_BLACKLIST_THRESHOLD` | 域名黑名单阈值 |
| `ZHUCE6_CFMAIL_ROTATION_COOLDOWN_SECONDS` | 域名轮换冷却 |
| `ZHUCE6_CFMAIL_ADD_PHONE_THRESHOLD` | add-phone gate 阈值 |
| `ZHUCE6_CFMAIL_WAIT_OTP_THRESHOLD` | wait-otp 阈值 |

### 运行时约束

- cfmail 主线默认只应保留当前 active 子域名为 enabled, 旧 auto 子域名在切换后会直接从配置中删除.
- 当旧子域名被 OpenAI ban 后, register 线程会依赖 `CfmailProvisioner.rotate_active_domain()` 自动切换到新的 `.example.com` 子域名.
- 若所有 enabled cfmail account 都进入 cooldown, register worker 会主动触发 rotation, 而不是一直卡死在 mailbox 阶段.
- `CfMailMailbox` 与 register loop 共享同一套 `CfmailAccountManager` cooldown 视图, 因此 mailbox 侧 cooldown 与 rotation 检测必须保持同一配置文件.
- `ZHUCE6_D1_DATABASE_ID` 不再有 repo 内默认 UUID. 新环境必须由 `init` 自动写入或手工配置; 若为空, `d1_cleanup` 会直接跳过.

## 5. 注册线程与代理池

| 变量 | 作用 | 常见值 | 约束 |
| --- | --- | --- | --- |
| `ZHUCE6_REGISTER_THREADS` | 注册并发线程数 | `12` | **不得大于代理池大小**, 否则多余线程永远拿不到代理 |
| `ZHUCE6_REGISTER_TARGET_COUNT` | 注册目标数 | `0` (无限) | 达到后自动停止 |
| `ZHUCE6_REGISTER_SLEEP_MIN` | 注册间隔下限 (秒) | `3` | - |
| `ZHUCE6_REGISTER_SLEEP_MAX` | 注册间隔上限 (秒) | `10` | - |
| `ZHUCE6_REGISTER_MAX_CONSECUTIVE_FAILURES` | 连续失败上限 | `3` | 超过后线程暂停 |

### add_phone 与 token 恢复调优

| 变量 | 作用 | 默认语义 |
| --- | --- | --- |
| `ZHUCE6_ADD_PHONE_OAUTH_MAX_ATTEMPTS` | add_phone 后 fresh login fallback 的最大尝试次数 | `1`, 范围 `1..3` |
| `ZHUCE6_ADD_PHONE_OAUTH_OTP_TIMEOUT_SECONDS` | add_phone fallback 登录链里的 OTP 等待上限 | 默认 `90`, 范围 `30..180` |
| `ZHUCE6_POST_CREATE_LOGIN_DELAY_SECONDS` | `create_account` 后进入 fresh login fallback 前的等待秒数 | 默认 `0`, 范围 `0..600` |
| `ZHUCE6_PENDING_TOKEN_RETRY_DELAY_SECONDS` | add_phone deferred retry 的基础延迟 | 当前实现会把首轮基础延迟钳到 `60s`, 避免把 token 恢复推到 5 分钟观测窗口之外 |

运行时要点:

- 如果 `create_account` 已直接返回 `https://chatgpt.com/api/auth/callback/openai?...`, 主流程会先走 `callback/openai -> /api/auth/session` 直取 token, 跳过 workspace flow 与 fresh login.
- `create_account` 命中 `add_phone` 不等于账号一定废掉.
- 主流程会先尝试 direct session token 提取, 失败后才回退 fresh login.
- 如果当次线程仍拿不到 token, 账号会带着凭据进入 pending retry queue, 由后台补取 token.
- Dashboard 总览中的"成功"已经是 backend 入池成功后的累计值, 不再单独区分 CPA sync.

### 代理池

| 变量 | 作用 | 常见值 |
| --- | --- | --- |
| `ZHUCE6_REGISTER_PROXY` | 注册链主代理 | 单个 URL, 启用代理池时留空 |
| `ZHUCE6_ENABLE_PROXY_POOL` | 是否启用代理池 | `1` / `0` |
| `ZHUCE6_PROXY_POOL_SIZE` | 代理池节点数 | `12` |
| `ZHUCE6_PROXY_POOL_DIRECT_URLS` | 直接代理列表 | 分号分隔 |
| `ZHUCE6_PROXY_POOL_CONFIG` | Clash YAML 路径 | 文件路径 |
| `ZHUCE6_PROXY_POOL_REGIONS` | 优先地区 | `sg,us,jp` |

> **重要**: `ZHUCE6_REGISTER_THREADS` 必须 <= `ZHUCE6_PROXY_POOL_SIZE`. 代理池每个节点同一时间只服务一个线程, 多余线程会因 `no proxy available in pool` 持续失败.

## 5.1 cfmail 子域名轮换

| 变量 | 作用 | 常见值 |
| --- | --- | --- |
| `ZHUCE6_CFMAIL_ROTATION_WINDOW` | 域名轮换观测窗口 | - |
| `ZHUCE6_CFMAIL_ROTATION_BLACKLIST_THRESHOLD` | 域名黑名单阈值 | - |
| `ZHUCE6_CFMAIL_ROTATION_COOLDOWN_SECONDS` | 域名轮换冷却 | - |

> **注意**: `cleanup_stale_domains` 会清理所有非 active 的 `auto*.zone` 历史域名. 切域后旧 auto 子域名会直接从 `config/cfmail_accounts.json` 删除, 非 auto 手工域名不会自动接管 active 语义.

> **注意**: stale cleanup 是 best-effort. 如果 Cloudflare 返回 DNS read-only / code `1043`, 实现会跳过该旧记录, 不应因为旧资源删除失败而回滚已经完成的新域名切换.

### 轮换主链

`CfmailProvisioner.rotate_active_domain()` 的成功判定以新域名主链为准:

1. 创建 email routing rule
2. 创建 DNS 记录
3. 更新 worker domain bindings
4. smoke test
5. 切换 active domain
6. best-effort cleanup old auto-domain artifacts

因此排障时, 应优先关注 `success` / `new_domain` 与 `config/cfmail_accounts.json` 中的 active 域名是否变化, 而不是把 cleanup 告警误判为 rotation 失败.

## 6. 平台差异

### Windows

- 推荐 PowerShell 7.
- `doctor --fix` 会自动处理 Python 依赖与 worker npm 依赖.
- `git`, `node`, `npx` 仍需你自行安装.

### Linux / WSL

- 命令与 README 主线一致.
- 如果使用 Clash YAML, 还需要系统里可用的 `sslocal`.
- 手工部署 cfmail worker 时优先使用 `npx wrangler`.

## 7. 推荐阅读顺序

1. `README.md`
2. `.env.example`
3. `docs/TROUBLESHOOTING.md`
4. `AGENTS.md`
