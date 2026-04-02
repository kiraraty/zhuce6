# zhuce6 部署指南

从零部署 zhuce6 注册机，对接 sub2api 后端，实现 ChatGPT 账号自动注册、入池、清理、校验、轮换。

---

## 一、你需要准备什么

| 准备项 | 说明 | 花费 |
|--------|------|------|
| 一台电脑 | macOS / Linux / WSL 均可 | - |
| 一个域名 | 用于 cfmail 收验证码，推荐 `.xyz` `.top` | ~5元/年 |
| Cloudflare 账号 | 免费，域名 DNS 托管到 Cloudflare | 免费 |
| 代理 | Clash / V2Ray 等，需要能访问 OpenAI | 自备 |
| sub2api 服务 | 已部署好的 sub2api 后端 | 自备 |

---

## 二、安装软件依赖

### macOS

```bash
# Python 3.11+
brew install python@3.13

# uv (Python 包管理)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Node.js 20+ (部署 cfmail Worker 需要)
brew install node

# sslocal (代理池模式需要)
brew install shadowsocks-rust

# 验证
python3 --version   # >= 3.11
uv --version
node --version      # >= 20
sslocal --version
```

### Linux (Debian/Ubuntu)

```bash
# Python
sudo apt install python3 python3-pip

# uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Node.js
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install nodejs

# sslocal
curl -fsSL https://github.com/shadowsocks/shadowsocks-rust/releases/latest/download/shadowsocks-v*-x86_64-unknown-linux-gnu.tar.xz | tar -xJ -C /usr/local/bin sslocal
```

---

## 三、配置 Cloudflare（cfmail 邮箱）

cfmail 的作用：用 Cloudflare Workers 自建临时邮箱，零成本接收 OpenAI 注册验证码。

### 第 1 步：买域名 + 接入 Cloudflare

1. 去 [Namesilo](https://namesilo.com) 或 [Spaceship](https://spaceship.com) 买个便宜域名（如 `mydomain.xyz`）
2. 打开 [dash.cloudflare.com](https://dash.cloudflare.com) → **Add a site** → 输入域名 → 选 **Free** plan
3. Cloudflare 会给你两个 nameserver（如 `clay.ns.cloudflare.com`），去域名注册商把 NS 改成这两个
4. 等几分钟，Cloudflare 显示域名状态为 **Active**

### 第 2 步：创建 API Token

1. Cloudflare Dashboard → 右上角头像 → **My Profile** → **API Tokens**
2. 点 **Create Token** → 选 **Create Custom Token**
3. 添加以下 5 个权限：

```
帐户 - D1           - 编辑
帐户 - Workers 脚本  - 编辑
区域 - 电子邮件路由规则 - 编辑
区域 - 区域          - 读取
区域 - DNS          - 编辑
```

4. Zone Resources 选 **All zones**
5. 点 **Continue to summary** → **Create Token**
6. **复制 Token**（只显示一次，务必保存好）

### 第 3 步：启用 Email Routing

1. Dashboard → 选你的域名 → 左侧 **Email** → **Email Routing**
2. 点 **Enable Email Routing**
3. 如果提示 DNS 记录冲突，点 **自动添加记录** 或手动删掉旧的 MX 记录
4. 确认状态显示 **Active**

到这里 Cloudflare 部分就配好了。你需要记住两样东西：
- **API Token**：刚才复制的那串
- **域名**：如 `mydomain.xyz`

---

## 四、安装 zhuce6 并部署 cfmail Worker

### 第 1 步：下载代码 + 安装依赖

```bash
git clone https://github.com/kiraraty/zhuce6.git
cd zhuce6
uv sync
```

### 第 2 步：部署 cfmail Worker

这一步会自动在 Cloudflare 上部署一个 Worker 来收发邮件。

```bash
PYTHONPATH=. uv run python scripts/setup_cfmail.py \
  --api-token "你的API_TOKEN" \
  --zone-name "你的域名.xyz"
```

脚本会自动完成：
- 校验 Token 权限
- 获取 account_id、zone_id
- 创建 D1 数据库
- 下载并部署 cfmail Worker
- 配置 DNS 和 Email Routing
- 生成 `config/cfmail_accounts.json` 和 `config/cfmail_provision.env`

> **如果报 npm 权限错误**：先执行 `sudo chown -R $(whoami) ~/.npm`，再重跑。
>
> **如果已经部署过**：加 `--skip-clone` 跳过源码下载。

### 第 3 步：验证部署

```bash
uv run python main.py doctor
```

看到 `cfmail ok` 就说明 cfmail 配置成功。

---

## 五、配置 .env

### 方式一：交互向导（推荐新手）

```bash
uv run python main.py init
```

按提示选择 mode=full、backend=sub2api，填入 sub2api 地址和凭据。

### 方式二：手动创建

在项目根目录创建 `.env` 文件：

```bash
# ============================
# 核心配置
# ============================
ZHUCE6_HOST=127.0.0.1
ZHUCE6_PORT=8000
ZHUCE6_RUNTIME_MODE=full

# ============================
# sub2api 后端（改成你的）
# ============================
ZHUCE6_BACKEND=sub2api
ZHUCE6_SUB2API_BASE_URL=http://你的服务器IP:8080
ZHUCE6_SUB2API_ADMIN_EMAIL=你的管理员邮箱
ZHUCE6_SUB2API_ADMIN_PASSWORD=你的管理员密码

# ============================
# 注册设置
# ============================
ZHUCE6_REGISTER_ENABLED=true
ZHUCE6_REGISTER_THREADS=4          # 注册线程数，建议 1-4
ZHUCE6_REGISTER_PROXY=http://127.0.0.1:7897   # 你的本地代理端口
ZHUCE6_REGISTER_MAIL_PROVIDER=cfmail
ZHUCE6_REGISTER_SLEEP_MIN=3
ZHUCE6_REGISTER_SLEEP_MAX=10

# ============================
# 代理池（二选一）
# ============================

# --- 选项 A：不用代理池，走单代理 ---
# ZHUCE6_ENABLE_PROXY_POOL=0

# --- 选项 B：开启代理池（推荐） ---
ZHUCE6_ENABLE_PROXY_POOL=1
ZHUCE6_PROXY_POOL_CONFIG=config/clash_config.yaml
ZHUCE6_PROXY_POOL_SIZE=40
ZHUCE6_PROXY_POOL_EXCLUDE_NAMES=香港,台湾,日本,新加坡    # 排除无效节点

# ============================
# 后台治理任务
# ============================
ZHUCE6_CLEANUP_ENABLED=true        # 清理过期账号
ZHUCE6_VALIDATE_ENABLED=true       # 校验 token 有效性
ZHUCE6_ROTATE_ENABLED=true         # 轮换（401 删除，429 保留）

# ============================
# cfmail（由 setup_cfmail.py 自动生成，通常不用改）
# ============================
ZHUCE6_CFMAIL_API_TOKEN=你的API_TOKEN
ZHUCE6_CFMAIL_CF_ACCOUNT_ID=自动获取的account_id
ZHUCE6_CFMAIL_CF_ZONE_ID=自动获取的zone_id
ZHUCE6_CFMAIL_WORKER_NAME=zhuce6-cfmail
ZHUCE6_CFMAIL_ZONE_NAME=你的域名.xyz
ZHUCE6_CFMAIL_ROTATION_WINDOW=10
ZHUCE6_CFMAIL_ROTATION_BLACKLIST_THRESHOLD=6

# ============================
# D1 清理
# ============================
ZHUCE6_D1_CLEANUP_ENABLED=true
ZHUCE6_D1_DATABASE_ID=自动获取的d1_id

# ============================
# 账号存活追踪
# ============================
ZHUCE6_ACCOUNT_SURVIVAL_ENABLED=true
```

---

## 六、配置代理池（可选但推荐）

代理池让注册请求分散到多个 IP，降低风控压力。

### 重要前提

**必须关闭 Clash 的 TUN 模式**，否则 sslocal 的流量会被二次劫持导致全部超时。

### 准备 Clash YAML

如果你的机场订阅是 Base64 格式（一堆 `ss://` 开头的文本），需要转换成 Clash YAML：

```bash
# 下载订阅
curl -sL "你的订阅链接" -o /tmp/sub.txt

# 转换
python3 -c "
import base64, urllib.parse, yaml
with open('/tmp/sub.txt') as f:
    data = base64.b64decode(f.read().strip()).decode()
proxies = []
for line in data.splitlines():
    line = line.strip()
    if not line.startswith('ss://'): continue
    parts = line.split('#', 1)
    name = urllib.parse.unquote(parts[1]) if len(parts) > 1 else 'unnamed'
    uri = parts[0][5:]
    if '@' not in uri: continue
    enc, srv = uri.rsplit('@', 1)
    try: dec = base64.b64decode(enc + '==').decode()
    except: dec = enc
    if ':' not in dec: continue
    method, pw = dec.split(':', 1)
    host, port = srv.split(':')
    if '超时' in name or '更新' in name: continue
    proxies.append({'name':name,'type':'ss','server':host,'port':int(port),'cipher':method,'password':pw})
with open('config/clash_config.yaml','w') as f:
    yaml.dump({'proxies':proxies}, f, allow_unicode=True)
print(f'写入 {len(proxies)} 个节点')
"
```

如果订阅本身就是 Clash YAML 格式，直接放到 `config/clash_config.yaml`。

### 节点选择建议

| 地区 | 能注册 | 建议 |
|------|--------|------|
| 美国 | ✅ | 推荐，OpenAI 本土 |
| 英国 / 德国 | ✅ | 推荐，成功率高 |
| 澳大利亚 | ✅ | 推荐 |
| 越南 / 马来西亚 | ✅ | 推荐，成功率不错 |
| 新加坡 | ⚠️ | 时好时坏 |
| 日本 | ⚠️ | 成功率低 |
| 香港 | ❌ | OpenAI 不支持 |
| 台湾 | ❌ | 灰区，基本被拦 |

在 `.env` 中排除无效节点：
```bash
ZHUCE6_PROXY_POOL_EXCLUDE_NAMES=香港,台湾,日本,新加坡
```

---

## 七、启动

### 环境最终检查

```bash
uv run python main.py doctor --fix
```

确认输出包含 `full(sub2api): available`。

### 启动服务

```bash
# 前台运行（看日志）
uv run python main.py --mode full

# 后台运行
nohup uv run python main.py --mode full > /dev/null 2>&1 &
```

### 打开 Dashboard

浏览器访问 `http://127.0.0.1:8000/zhuce6`

### 停止

```bash
uv run python main.py stop
```

---

## 八、验证是否正常工作

### 看注册日志

```bash
tail -f logs/register.log
```

正常的日志流程：
```
starting chatgpt registration flow
created mailbox: tmpocXXX@yourdomain.xyz
oauth flow initialized
device_id acquired: xxx-xxx
sentinel token acquired
signup form status: 200
register password status: 200
verification code received: 123456
validate otp status: 200
create account status: 200
```

### 看有多少账号了

```bash
ls pool/tmpoc*.json | wc -l
```

### API 查询

```bash
# 运行状态
curl http://127.0.0.1:8000/api/runtime | python3 -m json.tool

# 注册统计
curl http://127.0.0.1:8000/api/summary | python3 -m json.tool

# 依赖健康
curl http://127.0.0.1:8000/api/health/dependencies | python3 -m json.tool
```

---

## 九、自动运维机制

你不需要手动干预，以下全部自动运行：

### 域名轮换

当 OpenAI 封禁当前邮箱域名（返回 `registration_disallowed`），系统自动创建新子域名继续注册：

```
mydomain.xyz → auto0401xxxx.mydomain.xyz → auto0402xxxx.mydomain.xyz → ...
```

### 账号治理

| 后台任务 | 做什么 | 默认间隔 |
|----------|--------|----------|
| rotate | 探测账号状态，401 删、429 等冷却 | 2 分钟 |
| validate | 校验所有 token 是否有效 | 3 分钟 |
| cleanup | 清理过期账号 | 5 分钟 |
| d1_cleanup | 清理 cfmail 数据库过期邮件 | 30 分钟 |
| reconcile | pool 和 sub2api 双向同步防丢号 | 随 rotate |
| survival | 追踪账号存活状态 | 2 分钟 |

### 代理池管理

- 同一节点使用后自动冷却 120 秒
- 不同地区自动轮换
- 坏节点（连续失败 3 次且从未成功）自动禁用替换
- device_id 连续失败 2 次的节点冷却 10 分钟

---

## 十、常见问题

### Q: device_id 一直超时

**原因**：代理不通，或 Clash TUN 模式劫持了 sslocal 流量。

**解决**：
1. 关闭 Clash TUN 模式
2. 测试代理连通性：`curl -x http://127.0.0.1:7897 https://cloudflare.com/cdn-cgi/trace`

### Q: 一直报 phone gate

**原因**：OpenAI 根据 IP 质量要求手机验证。机场 IP 容易触发。

**解决**：
- 不用慌，账号会自动进入 deferred 队列，后台 retry 大概率能拿到 token
- 换更干净的 IP（住宅代理最佳）
- 减少线程数降低频率

### Q: registration_disallowed

**原因**：当前邮箱域名被 OpenAI 封禁。

**解决**：系统会自动轮换到新子域名，无需手动干预。如果轮换失败，检查 API Token 权限是否完整。

### Q: cfmail 域名轮换失败

**原因**：API Token 缺少权限。

**解决**：确认 Token 有这 5 个权限：D1 编辑、Workers 脚本编辑、电子邮件路由规则编辑、区域读取、DNS 编辑。

### Q: sslocal 进程挂了（process exited with code 0）

**解决**：重启服务即可，sslocal 会重新拉起。

```bash
uv run python main.py stop
uv run python main.py --mode full
```

### Q: sub2api 同步失败

**解决**：
1. 确认 sub2api 可达：`curl http://你的地址:8080/health`
2. 检查 `.env` 中的 `ZHUCE6_SUB2API_*` 配置
3. reconcile 会自动补同步，也可以重启触发一次

---

## 十一、远程访问 Dashboard

如果需要从外网查看 Dashboard，用 frpc 做端口转发。

在 frpc 配置文件中添加：

```toml
[[proxies]]
name = "zhuce6"
type = "tcp"
localIP = "127.0.0.1"
localPort = 8000
remotePort = 18600
```

重启 frpc 后通过 `http://你的服务器IP:18600/zhuce6` 访问。

---

## 十二、常用命令速查

```bash
# 首次配置
uv run python main.py init

# 环境检查
uv run python main.py doctor --fix

# 启动
uv run python main.py --mode full

# 停止
uv run python main.py stop

# 查看状态
uv run python main.py status

# 查看注册日志
tail -f logs/register.log

# 查看成功记录
grep "✅ deferred token acquired" logs/register.log

# 查看池内账号数
ls pool/tmpoc*.json | wc -l

# 查看哪个节点在出货
grep -B20 "deferred token acquired" logs/register.log | grep "acquired proxy"

# 部署 cfmail Worker
PYTHONPATH=. uv run python scripts/setup_cfmail.py \
  --api-token "TOKEN" --zone-name "domain.xyz"

# API 查询
curl http://127.0.0.1:8000/api/runtime | python3 -m json.tool
curl http://127.0.0.1:8000/api/summary | python3 -m json.tool
```
