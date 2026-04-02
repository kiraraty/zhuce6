# zhuce6 Troubleshooting

先记住主线:

```bash
uv run python main.py init
uv run python main.py doctor --fix
uv run python main.py --mode lite
# 或
uv run python main.py --mode full
```

如果排障前还没跑 `doctor --fix`, 先跑它.

## 1. 基础检查

```bash
uv run python main.py status
uv run python main.py doctor --fix
```

启动后再看 API:

```bash
export ZHUCE6_BASE_URL="http://<dashboard-host>:<dashboard-port>"
curl -sS "$ZHUCE6_BASE_URL/api/runtime" | python3 -m json.tool | head -n 80
curl -sS "$ZHUCE6_BASE_URL/api/health/dependencies" | python3 -m json.tool | head -n 80
```

## 2. `init` 之后仍然跑不起来

优先检查:

- `.env` 是否是本次向导写入的目标文件
- `doctor --fix` 是否已执行
- 当前启动命令是否显式带了 `--mode lite` 或 `--mode full`
- `backend` 是否与你填写的后端配置一致

## 3. cfmail 不可用

### 新配置路径

现在新配置支持两条 cfmail 初始化路径:

- 从零部署 cfmail Worker: `Cloudflare API Token + zone_name`
- 复用已部署 cfmail Worker: `CF_AUTH_EMAIL + CF_AUTH_KEY + zone_name + worker_domain`

如果向导已经跑过, 先检查这些结果是否存在:

- `config/cfmail_accounts.json`
- `config/cfmail_provision.env`
- `.env` 中的:
  - `ZHUCE6_CFMAIL_API_TOKEN`
  - `ZHUCE6_CFMAIL_CF_AUTH_EMAIL`
  - `ZHUCE6_CFMAIL_CF_AUTH_KEY`
  - `ZHUCE6_CFMAIL_CF_ACCOUNT_ID`
  - `ZHUCE6_CFMAIL_CF_ZONE_ID`
  - `ZHUCE6_CFMAIL_WORKER_NAME`
  - `ZHUCE6_CFMAIL_ZONE_NAME`

### 已有配置路径

如果仓库内已有 cfmail 配置, `init` 默认会提示复用. 若你误选了重新生成, 应先对照现有 `config/` 内容确认是否与 live 配置一致.

### 仍需手工排查时

```bash
uv run python scripts/setup_cfmail.py --help
```

注意:

- `scripts/setup_cfmail.py` 的完整部署链依赖 `wrangler`.
- `wrangler` 在非交互环境下要求 `Cloudflare API Token`.
- 所以如果你只有 `CF_AUTH_EMAIL + CF_AUTH_KEY`, 不要指望它完成首次 Worker 部署, 应该在 `init` 时直接填写已部署的 `worker_domain`.
- 只有需要从零部署 Worker 时, 才应走 `uv run python scripts/setup_cfmail.py --api-token <token> --zone-name <zone>` 这条路径.
- 如果你填入的 `email_domain` 已经失效, `init` 在保存阶段会尝试自动轮换到新的可用子域名. 若仍失败, 再手工检查 DNS 与 Email Routing.

## 4. `doctor --fix` 之后还有依赖问题

`doctor --fix` 当前会自动处理:

- `uv sync`
- cfmail worker 目录下的 `npm install --no-fund --no-audit`

它不会替你全局安装:

- `git`
- `node`
- `npm`
- `npx`
- `sslocal`

所以如果报告里仍有失败项, 直接按报告补齐系统依赖即可.

如果 `d1_cleanup` 一直跳过, 再检查:

- `ZHUCE6_D1_DATABASE_ID` 是否为空
- 该值是否由 `init` / cfmail 向导自动写入

## 5. 代理问题

### 直接代理 URL

优先检查:

- `ZHUCE6_REGISTER_PROXY`
- `ZHUCE6_PROXY_POOL_DIRECT_URLS`

### Clash YAML

如果你选择 Clash YAML 模式, 还要确认:

```bash
command -v sslocal || command -v ss-local || true
```

如果没有 `sslocal`, 先安装它, 再重新执行:

```bash
uv run python main.py doctor --fix
```

## 6. `full + cpa` 不可用

重点检查:

- `ZHUCE6_BACKEND=cpa`
- `ZHUCE6_CPA_MANAGEMENT_BASE_URL`
- `ZHUCE6_CPA_MANAGEMENT_KEY`

然后看:

```bash
curl -sS "$ZHUCE6_BASE_URL/api/health/dependencies" | python3 -m json.tool | head -n 80
```

## 7. `full + sub2api` 不可用

重点检查:

- `ZHUCE6_BACKEND=sub2api`
- `ZHUCE6_SUB2API_BASE_URL`
- 认证是否完整:
  - `ZHUCE6_SUB2API_API_KEY`
  - 或 `ZHUCE6_SUB2API_ADMIN_EMAIL` + `ZHUCE6_SUB2API_ADMIN_PASSWORD`

`doctor` 与 `/api/health/dependencies` 都会明确显示 sub2api ready / unavailable.

## 8. Dashboard 正常, 但没有注册任务

先看 `/api/runtime`:

- `runtime_mode`
- `registered_tasks`
- `register_enabled`

常见原因:

- 当前模式是 `dashboard`
- `ZHUCE6_REGISTER_ENABLED=false`
- `cfmail` 运行时变量不完整
- 代理未配置或不可达

## 9. Windows / Linux / WSL

### Windows

- 推荐 PowerShell 7.
- 先保证 `python`, `uv`, `node`, `npm`, `npx`, `git` 可用.
- 直接按 README 主线运行即可.

### Linux

- 按 README 主线运行.
- 需要 Clash YAML 时, 记得额外准备 `sslocal`.

### WSL

- 与 Linux 路径一致.
- 推荐依赖与仓库都放在 WSL 内执行.

如果你需要从别的前端域读取 `/api/runtime` 或 `/api/summary`, 还要配置:

- `ZHUCE6_DASHBOARD_ALLOWED_ORIGINS=http://your-dashboard.example.com`

## 10. cfmail OTP 收不到 / 全部 wait_otp 超时

### 现象

注册日志所有线程卡在 `wait_otp`, 180s 超时:

```
[zhuce6:register] [thread-1] ❌ failed [stage=wait_otp]: otp retrieval failed
  ↳ verification code timed out after 181.88s
  ↳ otp mailbox diagnostics: polls=45 scanned=0
```

### 根因

cfmail 子域名的 DNS 记录 (MX + SPF) 缺失. 没有 MX 记录, Cloudflare 无法接收邮件, OTP 永远到不了.

### 诊断

```bash
source config/cfmail_provision.env

# 检查子域名 DNS 记录数量 (应 >= 4: 3xMX + 1xTXT)
curl -s "https://api.cloudflare.com/client/v4/zones/$ZHUCE6_CFMAIL_CF_ZONE_ID/dns_records?name=<subdomain>.example.com" \
  -H "X-Auth-Email: $ZHUCE6_CFMAIL_CF_AUTH_EMAIL" \
  -H "X-Auth-Key: $ZHUCE6_CFMAIL_CF_AUTH_KEY" \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print(f'records: {r[\"result_info\"][\"total_count\"]}')"
```

### 修复

```bash
source config/cfmail_provision.env
uv run python -c "
from core.cfmail_provisioner import CfmailProvisioner
p = CfmailProvisioner()
for acct in p._load_all_accounts():
    if not acct.get('enabled'): continue
    domain = acct['email_domain']
    label = acct['name'].replace('cfmail-', '')
    try:
        p._create_email_routing_rule(domain, label)
        p._create_dns_records(domain)
        p._update_worker_domains(domain)
        print(f'fixed {domain}')
    except Exception as e:
        print(f'error {domain}: {e}')
"
```

## 11. no proxy available in pool

### 现象

```
[zhuce6:register] [thread-13] ❌ exception: no proxy available in pool
```

### 根因

`ZHUCE6_REGISTER_THREADS` 大于 `ZHUCE6_PROXY_POOL_SIZE`. 多余线程拿不到代理.

### 修复

确保线程数 <= 代理池大小:

```bash
# .env
ZHUCE6_REGISTER_THREADS=12
ZHUCE6_PROXY_POOL_SIZE=12
```

## 12. sslocal 残留进程堆积

### 现象

每次启动注册机生成 12 个 sslocal, 但停止时不清理. 长期积累后系统有上百个 sslocal 进程.

### 修复

重启前先清理:

```bash
pkill -f sslocal
```

## 13. `create_account` 已 200, 但 5 分钟 success 看起来是 0 / 全是 `add_phone_gate`

### 现象

- `logs/register.log` 里能看到 `create_account status: 200`
- 线程最终却频繁报:

```text
[zhuce6:register] [thread-1] ❌ failed [stage=add_phone_gate]: post-create flow requires phone gate
```

- 同时日志里还能看到:

```text
📥 deferred token retry enqueued
```

### 根因

这通常不是 `create_account` 本身失败, 而是 add_phone 后的 token 恢复链被拖慢或被误判:

- 账号创建已经成功, 但 direct session token / fresh login fallback 当次没有拿到 token.
- 账号被放进 pending retry queue 后, 如果首次重试发生得太晚, 5 分钟窗口里就会看起来像 `0%`.

当前实现已经做了两层处理:

1. 如果 `create_account` 已直接返回 `https://chatgpt.com/api/auth/callback/openai?...`, 先走 `callback/openai -> /api/auth/session` 直取 token.
2. `create_account` 命中 add_phone 后, 再尝试 direct session token 提取.
3. 如果当次线程仍未拿到 token, pending retry queue 会在短窗口内补取, 首轮基础延迟会钳到 `60s`.

### 先确认

优先同时看两类日志:

```bash
grep -n "add_phone_gate\\|deferred token retry enqueued\\|deferred token acquired\\|direct session token" logs/register.log | tail -n 80
```

如果你能看到:

- `post-create add_phone: attempting direct session token extraction`
- `📥 deferred token retry enqueued`
- `[pending] ✅ deferred token acquired`

说明问题在 token 恢复时序, 不是 `create_account` 没成功.

### 结论判断

- 只有 `add_phone_gate`, 没有任何 `[pending]` 成功: 再检查代码是否已包含 direct session token 路径与 60s pending retry 限制.
- 有 `[pending] ✅ deferred token acquired`: 说明账号并非 0 成功, 只是不能只按线程即时结果统计.
- 如果你在排查旧版本, 还要确认它是否在 `create_account` 后直接丢掉了 callback/session 结果, 又重新触发 fresh login, 这会显著提高 add_phone 命中率.

## 14. cfmail 全部报 `account unavailable` / mailbox 阶段持续失败

### 现象

注册日志持续出现:

```text
[zhuce6:register] [thread-1] ❌ failed [stage=mailbox]: cfmail account unavailable, current accounts: 无
```

或者线程长时间停在 mailbox 失败, 没有新的 cfmail 子域名被切出.

### 根因

这是 cfmail 全域 cooldown 场景:

- 当前 enabled 的 cfmail 账户全部进入 `CfmailAccountManager` cooldown.
- `select_account()` 返回 `None`, register 在 mailbox 阶段直接失败.
- 这类失败没有走到 OpenAI `unsupported_email` / `registration_disallowed` 信号时, 不会靠黑名单窗口自然触发 rotation.

当前实现已经在 register worker 顶部检测这个状态, 一旦发现所有 cfmail account 都不可选, 会主动调用 `CfmailProvisioner.rotate_active_domain()` 打破死锁.

### 先确认

看 `logs/register.log` 是否出现:

```text
[cfmail] all accounts in cooldown, forcing domain rotation to break deadlock
```

如果有, 说明死锁检测已经触发, 接着只需要看 rotation 是否成功.

### 手工验证 rotation

```bash
set -a && source .env && set +a
PYTHONPATH=. uv run python -c "
from core.cfmail_provisioner import CfmailProvisioner
p = CfmailProvisioner()
result = p.rotate_active_domain()
print(f'success={result.success}, new={result.new_domain}, error={result.error}')
"
```

如果这里成功, 但 register 仍不恢复, 再检查:

- `config/cfmail_accounts.json` 中是否已有新的 enabled 子域名
- worker domain 是否仍可访问
- 新子域名的 DNS / routing rule 是否已创建

## 15. `rotate_active_domain()` 因 DNS read-only / code 1043 报错

### 现象

手工调用或自动 rotation 时, 日志出现类似:

```text
HTTP 400 {"errors":[{"code":1043,"message":"DNS record is read only"}]}
```

### 根因

Cloudflare 某些历史 DNS record 或 email routing rule 可能是只读或受保护资源. 这些资源常出现在旧 auto 域名被切换后的残留清理阶段.

当前实现里:

- `_delete_domain_artifacts()` 对删除失败按 best-effort 处理
- `cleanup_stale_domains()` 会跳过 read-only DNS / routing rule
- `rotate_active_domain()` 在新域名已经完成 DNS + routing + worker binding + smoke test 后, 即使 cleanup 失败也不会回滚整个 rotation

所以 `1043` 更应被视为旧资源清理告警, 而不是新域名切换失败.

### 手工验证

如果怀疑 rotation 没真正切过去, 重点看最终结果而不是 cleanup 告警:

```bash
set -a && source .env && set +a
PYTHONPATH=. uv run python -c "
from core.cfmail_provisioner import CfmailProvisioner
p = CfmailProvisioner()
result = p.rotate_active_domain()
print(result)
"
```

只要输出里 `success=True`, 并且 `new_domain` 已变更, 就说明 rotation 主链成功.

## 16. CPA 里账号突然变少 / 重启后 inventory 丢失

### 现象

- CPA `auth-files` 数量明显小于本地 `pool/*.json`
- CPA 重启后 Dashboard 里的 `cpa_count` 突然下降
- 注册虽然还在成功, 但旧账号像是消失了

### 正确理解

当前稳定结构不是“本地 pool 单池”, 而是:

- `pool/*.json`: 持久化备份
- CPA backend: 运行时主池

因此 CPA 丢库存时, 正确修复动作不是重新解释成双池晋升, 而是做 backup reconcile.

### 已有机制

- register 启动时会先做一次 runtime reconcile
- `rotate` 周期任务也会检测 drift
- 若 backend 缺账号, 会从 `pool/*.json` 回灌到 CPA
- 若 backend 有账号但本地没有备份, 会反向补写 pool

### 先确认

```bash
uv run python main.py status
curl -sS "$ZHUCE6_BASE_URL/api/summary" | python3 -m json.tool | head -n 80
```

重点看:

- `pool_count`
- `cpa_count`
- register 概览里的 `total_attempts` / `total_success`
- `pool/cpa_runtime_reconcile_state.json`

### 结论判断

- 若 `pool_count` 明显大于 `cpa_count`, 优先看 reconcile 是否正在补回.
- 若 `total_attempts` 持续增长但 `total_success` 不动, 且失败热点出现 `cpa_sync_failed`, 说明 register 新号写 backend 失败, 应先排查 CPA Management API 或网络错误.

## 17. validate 删除了 backend, 但本地 pool 还残留

当前实现中, `validate`, `rotate`, `cleanup` 都应双删 backend + pool.

如果你仍看到“backend 已删但 pool 还在”的残留, 先确认代码是否为最新版本, 再复查对应任务日志:

- `validate`: `deleted`
- `rotate`: `deleted_401`
- `cleanup`: `deleted`
