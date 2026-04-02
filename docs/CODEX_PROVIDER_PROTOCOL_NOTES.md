# ChatGPT Provider Protocol Notes

本文档记录分享版仓库仍然保留的 provider 协议约定. 当前只保留 `cfmail` 基线, 不再维护历史第三方邮箱 provider 的接入说明.

## 1. cfmail mailbox create

- Method: `POST`
- Endpoint: `https://<worker-domain>/admin/new_address`
- Headers:
  - `x-admin-auth: <admin-password>`
  - `Accept: application/json`
  - `Content-Type: application/json`
- Request body:

```json
{
  "enablePrefix": true,
  "name": "oc<random>",
  "domain": "mail.example.com"
}
```

- Success payload:

```json
{
  "address": "ocxxxx@mail.example.com",
  "jwt": "<mailbox-jwt>"
}
```

## 2. cfmail mail list

- Method: `GET`
- Endpoint: `https://<worker-domain>/api/mails?limit=<n>&offset=0`
- Headers:
  - `Authorization: Bearer <mailbox-jwt>`
  - `Accept: application/json`

典型返回中会包含 `results`, 每条邮件通常至少应能提供唯一 id 与原始正文片段.

## 3. OTP 提取约定

- 只处理当前 mailbox 的新邮件.
- 默认提取 6 位数字验证码.
- `before_ids` 用于忽略旧邮件.
- 若列表接口先看到旧邮件, `wait_for_code()` 会扩大窗口后继续轮询.

## 4. 错误分类

### 4.1 mailbox create 侧

- `transport_error`: 网络抖动或上游连接失败, 可重试.
- `HTTP 4xx`: 配置错误, 鉴权失败, 或上游资源不可用.
- `HTTP 5xx`: 上游临时错误, 应有限重试.

### 4.2 mail list / wait_otp 侧

- 列表可达但无新邮件: 记为 `wait_otp` 路径继续轮询.
- 列表接口异常: 记为 mailbox 侧问题, 先检查 worker 与 auth.
- 解析不到验证码: 检查邮件正文模板与关键词过滤.

## 5. 候选号 readiness

主池治理依赖两类探测:

- quota probe
- real service probe

分享版文档只保留这个抽象边界, 具体实现细节请以当前 `ops/rotate.py` 与 `ops/validate.py` 为准.
