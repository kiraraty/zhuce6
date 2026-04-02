# Pool 文件格式

zhuce6 注册成功的账号保存在 `pool/` 目录, 每个账号一个 JSON 文件. 当前仓库的稳定口径是:

- `pool/*.json` 是持久化备份层
- CPA / sub2api 后端是运行时主池
- register 成功后先写 `pool`, 再立即同步到后端
- rotate / validate / cleanup 删除时始终双删 backend + pool
- backend 库存与本地备份发生漂移时, 会做双向 reconcile

文件名生成逻辑见 `platforms/chatgpt/pool.py` 的 `build_pool_filename`.

## 文件命名

`<email>.json`, 例如 `user123@mail.example.com.json`.

当 `email` 缺失时, 会回退为 `<account_id>.json` 或 `chatgpt_<timestamp>.json`.

## 字段说明

以下字段来自实际写盘逻辑 `platforms/chatgpt/pool.py` 与 CPA 上传逻辑 `platforms/chatgpt/cpa_upload.py`.

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| email | string | ✅ | 注册邮箱, 同时用于文件名 |
| password | string | 通常有 | 注册密码 |
| access_token | string | ✅ | ChatGPT access token |
| refresh_token | string | 常见 | 用于刷新 access_token |
| account_id | string | 常见 | OpenAI 账号 ID |
| workspace_id | string | 常见 | Workspace ID |
| id_token | string | 可选 | 登录链返回的 ID token |
| session_token | string | 可选 | 会话 token |
| source | string | ✅ | 来源, 默认 `register` |
| health_status | string | ✅ | 最近一次健康标记, 默认 `unknown` |
| created_at | string (ISO 8601) | ✅ | 本地 pool 文件创建时间 |
| backup_written | boolean | ✅ | 本地持久化备份是否已写入, 默认 `true` |
| cpa_sync_status | string | ✅ | 最近一次 backend 同步状态, `pending` / `synced` / `failed` |
| last_cpa_sync_at | string | ✅ | 最近一次 backend 同步时间 |
| last_cpa_sync_error | string | ✅ | 最近一次 backend 同步失败信息 |
| last_probe_at | string | ✅ | 最近一次探测时间 |
| last_probe_status_code | integer or null | ✅ | 最近一次探测 HTTP 状态码 |
| last_probe_result | string | ✅ | 最近一次探测结果摘要 |
| last_probe_detail | string | ✅ | 最近一次探测详细信息 |

## 示例

```json
{
  "email": "user@example.com",
  "password": "pass-example",
  "access_token": "sk-access-token-exam...",
  "refresh_token": "refresh-token-examp...",
  "account_id": "acct_example",
  "workspace_id": "ws_example",
  "id_token": "id-token-example-123...",
  "session_token": "session-token-examp...",
  "source": "register",
  "health_status": "unknown",
  "created_at": "2026-03-28T04:15:30+08:00",
  "backup_written": true,
  "cpa_sync_status": "pending",
  "last_cpa_sync_at": "",
  "last_cpa_sync_error": "",
  "last_probe_at": "",
  "last_probe_status_code": null,
  "last_probe_result": "",
  "last_probe_detail": ""
}
```

## 对接说明

### 对接 CPA

如果你走的是 CPA Management API, register 成功后会直接同步到 CPA. 当 CPA 容器重启导致库存丢失时, runtime reconcile 会从 `pool/*.json` 回灌缺失账号.

### 对接 sub2api

如果你走的是 `backend=sub2api`, 应按 sub2api Admin API 的上传接口对接.

### 自定义对接

`pool/*.json` 是标准 JSON, 可以用任何语言解析.

关键字段:

- `refresh_token`: 长期访问与续期最关键的字段.
- `access_token`: 短期访问 token, 一般有效期较短.
- `backup_written`: 当前本地持久化备份是否有效.
- `cpa_sync_status`: backend 是否已经与本地备份完成同步.
