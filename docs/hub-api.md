# Hub API 契约（融合网关 · 面板对接）

> 本文是 `hub_proxy.py`（服务端）与 `hub_dashboard.html`（面板）之间的**唯一契约**。
> 改任何一侧的字段，都要同时改本文与另一侧；`tests/_test_hub_panel_contract.py`
> 会核对面板里出现的路径都在服务端注册过。

融合网关（hub）是一个进程、一个端口，同时托管两个上游：

| 上游 | 挂载路径 | 协议 | 说明 |
|---|---|---|---|
| WorkBuddy | `/wb/` | OpenAI + Anthropic | 完整看板原样代理，前缀由服务端注入的 shim 自动补齐 |
| Qoder | `/qoder/` | OpenAI + Anthropic | 同上 |
| ZCode（`vendor/zcode2api`） | `/zcode/` | **仅 Anthropic Messages** | 后台管理页原样代理；它的模型只能走 `/v1/messages` |

`/v1/*` 是**合并后的网关入口**：按模型名把请求派给对应的上游。

---

## 1. 鉴权

| 范围 | 规则 |
|---|---|
| 面板接口（`/panel/*`、`/api/hub/*`） | `X-Panel-Token: <token>`（登录后拿到）**或** `Authorization: Bearer <api key>` |
| `/v1/*` | 配置了任一启用的 Key 时必须是合法 Key（`Authorization: Bearer` 或 `?key=`）；一个 Key 都没配且非局域网模式时不校验 |

`/health` 与 `/` 永远开放（面板本身会做登录门禁）。

---

## 2. 面板会话

### `POST /panel/login`
请求：`{"password": "..."}`
成功：`200 {"ok": true, "token": "..."}`
失败：`401 {"ok": false, "error": "密码错误"}`

### `POST /panel/logout`
请求：无（带 `X-Panel-Token`）
响应：`200 {"ok": true}`

### `GET /panel/status`
响应：`200 {"ok": true, "authenticated": true|false, "panel_password_default": true|false}`

---

## 3. 概览

### `GET /api/hub/overview`

```json
{
  "ok": true,
  "hub": {
    "version": "1.8.0",
    "pid": 1234,
    "started_iso": "2026-10-27 14:00:00",
    "uptime_s": 3600,
    "lan": false
  },
  "gateway": {
    "base_url": "http://127.0.0.1:8787/v1",
    "models": 88,
    "auth_required": true,
    "keys": 1
  },
  "providers": [
    {
      "id": "wb",
      "label": "WorkBuddy",
      "state": "running",
      "port": 8788,
      "mount": "/wb/",
      "version": "1.7.33",
      "realm": "intl",
      "accounts": 2,
      "accounts_ready": 2,
      "models": 52,
      "today_requests": 10,
      "today_tokens": 12000,
      "restarts": 0,
      "last_error": null,
      "uptime_s": 300
    },
    {
      "id": "qoder",
      "label": "Qoder",
      "state": "running",
      "port": 8790,
      "mount": "/qoder/",
      "version": "1.7.7",
      "realm": "cn",
      "accounts": 1,
      "accounts_ready": 1,
      "models": 14,
      "today_requests": 3,
      "today_tokens": 900,
      "restarts": 0,
      "last_error": null,
      "uptime_s": 300
    },
    {
      "id": "zcode",
      "label": "ZCode",
      "kind": "zcode",
      "protocol": "anthropic",
      "state": "running",
      "port": 8791,
      "mount": "/zcode/",
      "version": "2.0.0",
      "realm": "",
      "accounts": 2,
      "accounts_ready": 2,
      "models": 2,
      "today_requests": 0,
      "today_tokens": 0,
      "total_requests": 17,
      "failed_requests": 1,
      "restarts": 0,
      "last_error": null,
      "uptime_s": 300
    }
  ],
  "routing": {
    "default_provider": "wb",
    "overrides": {"auto": "qoder"}
  },
  "accounts_hint": null,
  "accounts": [
    {
      "provider": "wb",
      "uid": "u-1",
      "nickname": "主号",
      "realm": "intl",
      "enabled": true,
      "token_expired": false,
      "in_cooldown": false,
      "last_error": "",
      "weight": 1,
      "note": ""
    }
  ]
}
```

- `state` 取值：`running` / `starting` / `stopped` / `failed` / `disabled`。
- 上游没起来时对应条目仍然出现，`state` 说明原因，`last_error` 给一句人话；
  概览接口本身永远 `200`。
- `accounts` 是三边账号的并集，按 `provider` 分组展示；字段缺失时面板按空值渲染。
- `accounts_hint` 非空表示「账号表没读全」，通常是某个上游改过面板密码而中枢不知道：
  在 `data/hub/settings.json` 的 `providers.<id>.panel_password` 里配上即可（面板
  会把这句提示显示在空表下面）。

---

## 4. 模型与路由

### `GET /api/hub/models`

```json
{
  "ok": true,
  "routing": {"default_provider": "wb", "overrides": {}},
  "models": [
    {"id": "deepseek-v4.1-flash", "provider": "wb", "realm": "intl", "owned_by": "workbuddy"},
    {"id": "qoder/qmodel_38max", "provider": "qoder", "realm": "cn", "owned_by": "qoder",
     "route_id": "qmodel_38max"},
    {"id": "GLM-5.2", "provider": "zcode", "protocol": "anthropic",
     "realm": "", "owned_by": "ZCode"}
  ]
}
```

- `id` 是**客户端应当填写的模型名**；`route_id` 是转发给上游时用的真实模型名。
  两者不同只发生在「两个上游有同名模型」时：后注册的一侧加 `qoder/` 前缀。
- `provider` 是这条模型最终会被派到哪一侧（已把 `routing.overrides` 算进去）。

### `GET /api/hub/settings`

```json
{
  "ok": true,
  "settings": {
    "routing": {"default_provider": "wb", "overrides": {}},
    "providers": {
      "wb":    {"enabled": true, "port": 8788},
      "qoder": {"enabled": true, "port": 8790}
    },
    "auth_required": true
  }
}
```

### `POST /api/hub/settings`

请求是**局部补丁**，只提交要改的键：

```json
{"routing": {"default_provider": "qoder", "overrides": {"auto": "qoder"}}}
```

响应：

```json
{"ok": true, "settings": { ...同 GET... }, "restart_required": true}
```

`restart_required` 为真表示改了端口/启停这类需要重启上游才生效的字段
（服务端会顺手重启受影响的上游，所以通常不用用户干预）。

---

## 5. 网关 Key

### `GET /api/hub/keys`

```json
{
  "ok": true,
  "auth_required": true,
  "keys": [
    {"id": "k-1", "name": "默认", "key": "sk-hub-…", "masked": "sk-hub-…9f2c",
     "enabled": true, "created_at": "2026/10/27 14:00"}
  ]
}
```

### `POST /api/hub/keys`

```json
{"keys": [{"id": "k-1", "name": "默认", "key": "sk-hub-…", "enabled": true}],
 "delete_ids": ["k-2"]}
```

响应：`{"ok": true, "keys": [ ...同 GET... ]}`

- `key` 留空表示「沿用已存的密钥」（面板不回显时用）。
- `delete_ids` 是软删除：历史用量仍能按 id 找到名字。

### `POST /api/hub/panel-password`

```json
{"password": "新密码"}
```

响应：`{"ok": true}`

---

## 5.4 当前出口（硬切换）

### `GET /api/hub/egress`

```json
{"ok": true, "egress": {
  "provider": "auto",            // auto | wb | qoder | zcode
  "realm": "",                   // "" | intl | cn（只对 wb/qoder 有意义）
  "default_provider": "wb",
  "provider_realms": {"wb": "cn", "qoder": "cn"}
}}
```

### `POST /api/hub/egress`

```json
{"provider": "wb", "realm": "cn"}
```

响应同 GET。语义：

- `provider=auto`：按模型清单派发（同名模型按 `default_provider`，未知模型也按它兜底）；
- `provider=wb|qoder|zcode`：这是**偏好出口**而不是硬闸门——
  - 多家都有的模型（如 `auto`）→ 走选中的那家；
  - 只在某一家出现的模型 → **仍走那一家**（正确转发优先，避免「用 Qoder 的模型
    却把出口设成 wb」变成必然失败）；
  - 目录里查不到的模型 → 走选中的那家，由上游自己报错；
- `realm` 非空且出口是 wb/qoder 时，中枢会顺手调用该上游的 `POST /realm` 把区域切过去
  （wb 要求面板会话，中枢用自己那份）。

`overview` 里也带一份同样的 `egress` 对象，面板据此渲染「当前出口」卡片。

`egress.upstream_effort` 是全局的「上游推理强度」：`auto`（跟随客户端）或
`none/minimal/low/medium/high/max`。设为具体档位后，中枢在转发
`/v1/chat/completions` 与 `/v1/responses` 时统一写入 `reasoning_effort`
（覆盖客户端带来的值）；`auto` 完全不动请求。通过
`POST /api/hub/settings {"routing":{"upstream_effort":"high"}}` 修改。

### `POST /api/hub/providers/<id>/realm`

```json
{"realm": "intl"}
```

只对 `wb` / `qoder` 有效（zcode 无区域概念），把**该上游**切到国际版 / 国内版——
与在它自己看板里切换是同一个状态，与「当前出口」无关（不需要把它设为出口）。
响应：`{"ok": true, "egress": { ...含 provider_realms... }}`。

---

## 5.5 统一账号管理

面板上的「+ 添加账号」、行内「禁用/启用」「删除」都走这组接口——它们由中枢转发给对应上游，
所以**一个页面就能登录三家**，不用分别打开三个看板。

### `POST /api/hub/accounts/login/start`

```json
{"provider": "wb", "realm": "intl"}
```

响应：

```json
{"ok": true, "provider": "wb", "state": "…", "auth_url": "https://…"}
```

`state` 是随后轮询用的会话标识（ZCode 是它后台的 `flow_id`）；`auth_url` 给用户在浏览器打开。
`realm` 只对 wb/qoder 有意义（`intl` / `cn`，省略则用该上游当前区域）。

### `GET /api/hub/accounts/login/poll?provider=wb&state=…`

```json
{"ok": true, "status": "pending|ready|failed|expired", "message": "", "account": {…}}
```

`status=ready` 时账号已经入池，面板据此刷新概览。

### `POST /api/hub/accounts/login/cancel`

```json
{"provider": "wb", "state": "…"}
```

对已结束的会话调用是安全的（幂等）。

### `POST /api/hub/accounts/set`

```json
{"provider": "wb", "uid": "…", "enabled": false}
```

### `POST /api/hub/accounts/delete`

```json
{"provider": "wb", "uid": "…"}
```

### `POST /api/hub/accounts/import`

只给 ZCode 用（它的账号是 JWT / API Key，没有设备授权也能加）：

```json
{"provider": "zcode", "rows": [{"name": "可选", "secret": "JWT 或 API Key"}]}
```

响应：`{"ok": true, "added": 1, "ids": ["…"]}`。其它上游调用会返回 `400` 并提示用 OAuth。

---

## 6. 上游控制

### `POST /api/hub/providers/<id>/start`
### `POST /api/hub/providers/<id>/stop`
### `POST /api/hub/providers/<id>/restart`

响应：`{"ok": true, "provider": { ...同 overview.providers 的单条... }}`

`id` 只能是 `wb` / `qoder`；未知 id 返回 `404 {"ok": false, "error": "..."}`。

### `GET /api/hub/logs?provider=wb&tail=200`

```json
{"ok": true, "provider": "wb", "lines": ["[14:00:01] listening ...", "..."]}
```

`tail` 上限 2000，默认 200。

---

## 7. 网关数据面

### `GET /health`

```json
{
  "ok": true,
  "service": "wb-hub",
  "version": "1.8.0",
  "pid": 1234,
  "started_iso": "2026-10-27 14:00:00",
  "gateway": {"models": 88, "auth_required": true},
  "providers": {
    "wb":    {"state": "running", "ok": true, "accounts": 2, "accounts_ready": 2, "realm": "intl"},
    "qoder": {"state": "running", "ok": true, "accounts": 1, "accounts_ready": 1, "realm": "cn"}
  }
}
```

### `GET /v1/models`

OpenAI 形状，额外带 `provider`：

```json
{"object": "list", "data": [
  {"id": "deepseek-v4.1-flash", "object": "model", "created": 0,
   "owned_by": "WorkBuddy", "provider": "wb", "protocol": "openai"},
  {"id": "GLM-5.2", "object": "model", "created": 0,
   "owned_by": "ZCode", "provider": "zcode", "protocol": "anthropic"}
]}
```

### `POST /v1/chat/completions` · `POST /v1/responses` · `POST /v1/messages`

按请求体里的 `model` 选上游：

1. 模型名带 `wb/` 或 `qoder/` 前缀 → 强制该上游，转发前剥掉前缀；
2. `routing.overrides` 里点名的模型 → 按配置；
3. 两边模型清单里都有的 → `routing.default_provider`；
4. 只在一边有 → 那一边；
5. 都没有 → `routing.default_provider`（由上游给出它自己的 404/400）。

**协议闸门**：`protocol == "anthropic"` 的上游（目前只有 ZCode）只接受
`POST /v1/messages`。用 `/v1/chat/completions` 或 `/v1/responses` 调它的模型会得到：

```json
{"error": {"message": "模型 GLM-5.2 由 ZCode 提供，而它只支持 Anthropic Messages 协议：请把请求发到 POST /v1/messages …",
           "type": "unsupported_protocol"}}
```

响应（含 SSE 流）原样透传；上游连不上时返回 `502`：

```json
{"error": {"message": "WorkBuddy 上游未就绪（state=failed）", "type": "upstream_unavailable"}}
```

---

## 8. 上游面板挂载

`/wb/*`、`/qoder/*`、`/zcode/*` 是**原样反代**：路径去掉前缀后转给对应上游，
响应（含 SSE、下载）透传。为了不改上游一行代码，服务端做三件事：

1. HTML 里的 `href` / `src` / `action` 绝对路径补前缀（`/static/x.css` →
   `/zcode/static/x.css`）——否则浏览器会去中枢根上找静态资源；
2. 3xx 的 `Location` 头补前缀（zcode 后台把 `/` 重定向到 `/admin`，不补就打到中枢根）；
3. 注入一段 shim：`fetch('/api/...')`、`window.open(...)`、`<a href="/...">` 点击
   都自动补前缀，并在左下角加一个「← 中枢」返回按钮。

所以三个上游的看板/后台代码**一行都不用改**。
