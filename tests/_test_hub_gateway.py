"""融合中枢（hub_proxy）的网关数据面：路由、合并、挂载、鉴权。

用两个**桩上游**（真 HTTP 服务）起一个中枢，验证的是中枢自己的逻辑：
- `/v1/models` 把两边模型合并、同名模型加前缀；
- `/v1/chat/completions` 按模型名派给对应上游（含前缀强制 / 配置覆盖 / 兜底）；
- SSE 原样透传；
- `/wb/`、`/qoder/` 挂载会把上游 HTML 注入 shim，非 HTML 原样透传；
- 面板会话、概览聚合、Key 鉴权、日志尾读。

不碰真实上游、不联网。
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hub_proxy as H          # noqa: E402
import hub_settings as S       # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + repr(extra) if extra != "" else ""))


class StubUpstream(BaseHTTPRequestHandler):
    """最小上游：/health、/v1/models、/accounts、/（HTML）、/v1/chat/completions。"""

    protocol_version = "HTTP/1.1"
    label = "wb"
    models = ()
    calls = None

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "service": self.label + "-proxy",
                                    "version": "stub-9.9", "accounts": 2,
                                    "accounts_ready": 2, "realm": "intl"})
        if self.path == "/meta":
            # zcode2api 的探活端点：只回版本号，没有 ok 字段。
            return self._json(200, {"version": "stub-2.0"})
        if self.path == "/admin/api/accounts":
            return self._json(200, {
                "accounts": [{"id": "zc-1", "name": "zcode-acct", "provider": "zai",
                              "enabled": True, "status": "active", "plan": "pro"}],
                "stats": {"total": 1, "active": 1, "calls": 7, "fail": 1},
            })
        if self.path == "/admin":
            self.send_response(302)
            self.send_header("Location", "/admin/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/admin/login":
            body = ("<html><head><link rel=\"stylesheet\" href=\"/static/css/app.css\">"
                    "</head><body><a href=\"/admin/accounts\">accounts</a>"
                    "<script>fetch('/admin/api/accounts')</script></body></html>"
                    ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/static/css/app.css":
            body = b"body{color:#000}"
            self.send_response(200)
            self.send_header("Content-Type", "text/css")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/v1/models":
            return self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": self.label,
                 "reasoning_efforts": ["low", "high", "max"],
                 "reasoning_default_effort": "high",
                 "supports_reasoning": True,
                 "max_output_tokens": 128000,
                 "context_length": 1000000}
                for m in self.models]})
        if self.path.startswith("/accounts"):
            return self._json(200, {"accounts": [
                {"uid": self.label + "-u1", "nickname": self.label + "-acct",
                 "realm": "intl", "enabled": True, "weight": 1}]})
        if self.path.startswith("/echo"):
            return self._json(200, {"path": self.path,
                                    "auth": self.headers.get("Authorization")})
        if self.path == "/":
            body = ("<html><body><h1>%s panel</h1>"
                    "<script>fetch('/accounts').then(r => r.json())</script>"
                    "</body></html>" % self.label).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._json(404, {"error": "not found", "path": self.path})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.calls is not None:
            self.calls.append({"path": self.path, "body": raw.decode("utf-8", "replace"),
                               "auth": self.headers.get("Authorization")})
        if self.path.endswith("/chat/completions"):
            payload = json.loads(raw or b"{}")
            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for piece in (b'data: {"delta":"hi"}\n\n', b"data: [DONE]\n\n"):
                    self.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                return
            return self._json(200, {"id": "x", "served_by": self.label,
                                    "model": payload.get("model")})
        if self.path == "/realm":
            payload = json.loads(raw or b"{}")
            if self.calls is not None:
                self.calls.append({"path": "/realm", "body": raw.decode("utf-8", "replace")})
            return self._json(200, {"ok": True, "current": payload.get("realm")})
        if self.path == "/panel/login":
            return self._json(200, {"ok": True, "token": "stub-token-" + self.label})
        if self.path.endswith("/v1/messages"):
            payload = json.loads(raw or b"{}")
            return self._json(200, {"type": "message", "role": "assistant",
                                    "served_by": self.label,
                                    "model": payload.get("model"),
                                    "content": [{"type": "text", "text": "ok"}]})
        return self._json(404, {"error": "not found"})


def start_upstream(label, models, calls):
    handler = type("Bound" + label, (StubUpstream,),
                   {"label": label, "models": tuple(models), "calls": calls})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def http(base, path, method="GET", payload=None, headers=None, follow=True):
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=hdrs,
                                 method=method)
    opener = urllib.request.build_opener() if follow else \
        urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http3(base, path, method="GET", payload=None, headers=None):
    """同 http()，但把响应头也带回来（重定向的 Location 只能从头上看）。"""
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=hdrs,
                                 method=method)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def main():
    calls = []
    wb_server, wb_port = start_upstream("wb", ["auto", "deepseek-v4.1-flash"], calls)
    qd_server, qd_port = start_upstream("qoder", ["auto", "qmodel_38max"], calls)
    zc_server, zc_port = start_upstream("zcode", ["GLM-5.2", "GLM-5-Turbo"], calls)
    data_dir = tempfile.mkdtemp(prefix="hub-gateway-")

    hub = H.Hub(data_dir, attach=True)
    hub.providers["wb"].port = wb_port
    hub.providers["qoder"].port = qd_port
    hub.providers["zcode"].port = zc_port
    server = H.HubServer(("127.0.0.1", 0), H.HubHandler, hub)
    hub.server_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % hub.server_port
    hub.models.refresh(force=True)

    # ---------------- /health ----------------
    status, raw = http(base, "/health")
    health = json.loads(raw)
    check("health 200", status == 200, status)
    check("health 标识自己是 wb-hub", health.get("service") == "wb-hub", health)
    check("health 汇总三个上游都 ok",
          all(p["ok"] for p in health["providers"].values()),
          health.get("providers"))
    check("zcode 用 /meta 探活也算 ok",
          health["providers"]["zcode"]["ok"] is True, health.get("providers"))
    check("health 暴露鉴权状态", health["gateway"]["auth_required"] is False,
          health.get("gateway"))

    # ---------------- /v1/models 合并 ----------------
    status, raw = http(base, "/v1/models")
    payload = json.loads(raw)
    ids = [m["id"] for m in payload["data"]]
    check("合并模型 200", status == 200 and payload.get("object") == "list", status)
    check("wb 的独有模型保留裸名字", "deepseek-v4.1-flash" in ids, ids)
    check("qoder 的独有模型保留裸名字", "qmodel_38max" in ids, ids)
    check("同名 auto 归先注册的 wb", "auto" in ids, ids)
    check("同名 auto 的 qoder 侧加前缀", "qoder/auto" in ids, ids)
    check("zcode 的模型进了合并清单",
          "GLM-5.2" in ids and "GLM-5-Turbo" in ids, ids)
    check("模型条目带 provider",
          all("provider" in m for m in payload["data"]), payload["data"][:2])
    proto = {m["id"]: m.get("protocol") for m in payload["data"]}
    check("zcode 模型标记 anthropic 协议", proto.get("GLM-5.2") == "anthropic",
          proto)
    check("wb/qoder 模型标记 openai 协议",
          proto.get("deepseek-v4.1-flash") == "openai"
          and proto.get("qmodel_38max") == "openai", proto)

    # ---------------- 上游模型元数据透传（客户端据此判断推理能力） ----------------
    status, raw = http(base, "/v1/models")
    rows = json.loads(raw)["data"]
    first = rows[0]
    check("透传上游 reasoning_efforts",
          first.get("reasoning_efforts") == ["low", "high", "max"], first)
    check("透传 reasoning_default_effort",
          first.get("reasoning_default_effort") == "high", first)
    check("透传 supports_reasoning", first.get("supports_reasoning") is True, first)
    check("透传 max_output_tokens / context_length",
          first.get("max_output_tokens") == 128000
          and first.get("context_length") == 1000000, first)
    check("仍带中枢的 provider / protocol",
          first.get("provider") and first.get("protocol"), first)

    status, raw = http(base, "/api/hub/models", headers={"X-Panel-Token": "x"})
    # 面板接口需要会话，这里只关心 entries 形状（无会话时应 401）
    check("面板模型接口仍受面板鉴权保护", status == 401, status)

    # ---------------- 路由 ----------------
    calls.clear()
    status, raw = http(base, "/v1/chat/completions", "POST",
                       {"model": "deepseek-v4.1-flash", "messages": []})
    body = json.loads(raw)
    check("按模型路由到 wb", body.get("served_by") == "wb", raw[:200])

    calls.clear()
    status, raw = http(base, "/v1/chat/completions", "POST",
                       {"model": "qoder/qmodel_38max", "messages": []})
    body = json.loads(raw)
    check("前缀强制 qoder", body.get("served_by") == "qoder", raw[:200])
    check("前缀在转发前被剥掉", body.get("model") == "qmodel_38max", body)

    calls.clear()
    status, raw = http(base, "/v1/chat/completions", "POST",
                       {"model": "qoder/auto", "messages": []})
    body = json.loads(raw)
    check("前缀可以让同名模型走另一侧",
          body.get("served_by") == "qoder" and body.get("model") == "auto", body)

    # 配置覆盖：把 wb 独有的模型强制指到 qoder（模拟面板里改路由）
    S.set_routing(data_dir, {"overrides": {"deepseek-v4.1-flash": "qoder"}})
    calls.clear()
    status, raw = http(base, "/v1/chat/completions", "POST",
                       {"model": "deepseek-v4.1-flash", "messages": []})
    body = json.loads(raw)
    check("routing.overrides 生效", body.get("served_by") == "qoder", raw[:200])
    S.set_routing(data_dir, {"overrides": {"deepseek-v4.1-flash": ""}})

    # 未知模型兜底默认上游
    calls.clear()
    status, raw = http(base, "/v1/chat/completions", "POST",
                       {"model": "no-such-model", "messages": []})
    body = json.loads(raw)
    check("未知模型兜底到 default_provider", body.get("served_by") == "wb", raw[:200])

    # ---------------- SSE 透传 ----------------
    status, raw = http(base, "/v1/chat/completions", "POST",
                       {"model": "auto", "stream": True, "messages": []})
    check("SSE 状态 200", status == 200, status)
    check("SSE 内容透传", b"data: [DONE]" in raw and b"delta" in raw, raw[:120])

    # ---------------- 挂载 ----------------
    status, raw = http(base, "/wb/")
    text = raw.decode("utf-8", "replace")
    check("wb 挂载 200", status == 200, status)
    check("wb 挂载注入 shim", 'var P = "/wb"' in text, text[:120])
    check("wb 挂载保留原页面", "wb panel" in text, text[:120])
    check("wb 挂载注入返回中枢入口", 'href="/"' in text, text[-200:])

    status, raw = http(base, "/qoder/")
    check("qoder 挂载前缀正确", b'"/qoder"' in raw, raw[:120])

    status, raw = http(base, "/wb/accounts")
    proxied = json.loads(raw)
    check("非 HTML 原样透传（/wb/accounts）",
          status == 200 and proxied["accounts"][0]["uid"] == "wb-u1", proxied)

    status, raw = http(base, "/wb/echo?q=1")
    echo = json.loads(raw)
    check("查询串带过去", echo["path"] == "/echo?q=1", echo)

    status, raw = http(base, "/wb", follow=False)
    check("裸挂载路径 302 到带斜杠", status == 302, status)

    # ---------------- zcode：Anthropic 协议 + 后台页面 ----------------
    status, raw = http(base, "/v1/messages", "POST",
                       {"model": "GLM-5.2", "max_tokens": 16,
                        "messages": [{"role": "user", "content": "hi"}]})
    body = json.loads(raw)
    check("Anthropic 请求按模型路由到 zcode",
          status == 200 and body.get("served_by") == "zcode", raw[:200])

    status, raw = http(base, "/v1/chat/completions", "POST",
                       {"model": "GLM-5.2", "messages": []})
    body = json.loads(raw)
    check("zcode 模型走 chat 端点被明确拒绝",
          status == 400
          and body["error"]["type"] == "unsupported_protocol", raw[:200])
    check("拒绝文案指向 /v1/messages", "/v1/messages" in body["error"]["message"],
          body["error"]["message"])

    status, raw, headers = http3(base, "/zcode/admin")
    check("zcode 302 的 Location 指向 /zcode/admin/login",
          status == 302 and headers.get("Location") == "/zcode/admin/login",
          (status, headers.get("Location")))

    status, raw = http(base, "/zcode/admin/login")
    text = raw.decode("utf-8", "replace")
    check("zcode 页面 200", status == 200, status)
    check("zcode 页面的绝对 href 被改写",
          'href="/zcode/static/css/app.css"' in text, text[:300])
    check("zcode 页面注入了 shim", 'var P = "/zcode"' in text, text[:300])

    status, raw = http(base, "/zcode/static/css/app.css")
    check("zcode 静态资源可经挂载访问", status == 200 and b"color" in raw, status)

    # ---------------- 面板 ----------------
    status, raw = http(base, "/panel/status")
    check("未登录时 panel/status 说 false",
          status == 200 and json.loads(raw)["authenticated"] is False, raw[:120])

    status, raw = http(base, "/api/hub/overview")
    check("无会话访问概览 401", status == 401, status)

    status, raw = http(base, "/panel/login", "POST", {"password": "wrong"})
    check("错误密码 401", status == 401, status)

    status, raw = http(base, "/panel/login", "POST", {"password": "admin"})
    token = json.loads(raw).get("token") if status == 200 else None
    check("默认密码登录成功", status == 200 and bool(token), raw[:120])

    status, raw = http(base, "/api/hub/overview", headers={"X-Panel-Token": token})
    overview = json.loads(raw)
    check("概览 200", status == 200 and overview.get("ok"), raw[:120])
    check("概览有三个上游", len(overview["providers"]) == 3, overview.get("providers"))
    check("概览合并三方账号", len(overview["accounts"]) == 3,
          overview.get("accounts"))
    check("概览账号带 provider 归属",
          {a["provider"] for a in overview["accounts"]}
          == {"wb", "qoder", "zcode"},
          overview.get("accounts"))
    check("概览带网关地址", overview["gateway"]["base_url"].endswith("/v1"),
          overview.get("gateway"))

    status, raw = http(base, "/api/hub/models", headers={"X-Panel-Token": token})
    models = json.loads(raw)
    check("面板模型表 200", status == 200 and models["models"], status)
    check("面板模型表带 route_id",
          all("route_id" in m for m in models["models"]), models["models"][:2])

    status, raw = http(base, "/api/hub/settings", headers={"X-Panel-Token": token})
    settings = json.loads(raw)["settings"]
    check("设置接口含路由与上游",
          "routing" in settings
          and set(settings["providers"]) == {"wb", "qoder", "zcode"},
          settings)

    status, raw = http(base, "/api/hub/settings", "POST",
                       {"routing": {"default_provider": "qoder"}},
                       headers={"X-Panel-Token": token})
    check("保存默认上游",
          status == 200 and json.loads(raw)["settings"]["routing"]["default_provider"]
          == "qoder", raw[:200])
    calls.clear()
    status, raw = http(base, "/v1/chat/completions", "POST",
                       {"model": "unknown-x", "messages": []})
    check("默认上游改动立即影响兜底",
          json.loads(raw).get("served_by") == "qoder", raw[:200])
    S.set_routing(data_dir, {"default_provider": "wb"})

    # ---------------- 当前出口（硬切换） ----------------
    status, raw = http(base, "/api/hub/egress", "POST", {"provider": "qoder"},
                       headers={"X-Panel-Token": token})
    egress = json.loads(raw).get("egress") or {}
    check("切换到 qoder 出口",
          status == 200 and egress.get("provider") == "qoder", raw[:200])

    calls.clear()
    status, raw = http(base, "/v1/chat/completions", "POST",
                       {"model": "deepseek-v4.1-flash", "messages": []})
    body = json.loads(raw)
    check("出口设成 qoder 时，只在 wb 的模型仍正确转发到 wb",
          body.get("served_by") == "wb", raw[:200])

    calls.clear()
    status, raw = http(base, "/v1/chat/completions", "POST",
                       {"model": "wb/deepseek-v4.1-flash", "messages": []})
    check("显式前缀仍是逃生口",
          json.loads(raw).get("served_by") == "wb", raw[:200])

    calls.clear()
    status, raw = http(base, "/v1/chat/completions", "POST",
                       {"model": "auto", "messages": []})
    check("两边都有的模型跟随当前出口",
          json.loads(raw).get("served_by") == "qoder", raw[:200])

    calls.clear()
    status, raw = http(base, "/api/hub/egress", "POST",
                       {"provider": "wb", "realm": "cn"},
                       headers={"X-Panel-Token": token})
    check("切到 wb 并指定国内版",
          status == 200
          and (json.loads(raw).get("egress") or {}).get("realm") == "cn", raw[:200])
    check("区域切换真的发给了上游",
          any(c["path"] == "/realm" and '"cn"' in c["body"] for c in calls), calls)

    # ---------------- 上游推理强度（覆盖客户端带来的 reasoning_effort） ----------------
    status, raw = http(base, "/api/hub/settings", "POST",
                       {"routing": {"upstream_effort": "high"}},
                       headers={"X-Panel-Token": token})
    check("保存推理强度 high", status == 200
          and json.loads(raw)["settings"]["routing"]["upstream_effort"] == "high",
          raw[:200])
    calls.clear()
    status, raw = http(base, "/v1/chat/completions", "POST",
                       {"model": "auto", "reasoning_effort": "low", "messages": []})
    sent = [c for c in calls if c["path"].endswith("/chat/completions")]
    check("转发时覆盖为 high",
          sent and json.loads(sent[-1]["body"]).get("reasoning_effort") == "high",
          sent[-1]["body"][:200] if sent else None)
    status, raw = http(base, "/v1/models")
    check("GET 不受 effort 影响（不带改写）", status == 200, status)
    status, raw = http(base, "/api/hub/settings", "POST",
                       {"routing": {"upstream_effort": "auto"}},
                       headers={"X-Panel-Token": token})
    check("切回 auto", status == 200
          and json.loads(raw)["settings"]["routing"]["upstream_effort"] == "auto",
          raw[:200])
    calls.clear()
    status, raw = http(base, "/v1/chat/completions", "POST",
                       {"model": "auto", "reasoning_effort": "low", "messages": []})
    sent = [c for c in calls if c["path"].endswith("/chat/completions")]
    check("auto 时不改写客户端值",
          sent and json.loads(sent[-1]["body"]).get("reasoning_effort") == "low",
          sent[-1]["body"][:200] if sent else None)

    # 模型清单跟上游走：上游新增模型后，强制刷新应立刻看到
    wb_server.RequestHandlerClass.models = ("auto", "deepseek-v4.1-flash",
                                            "brand-new-model")
    status, raw = http(base, "/api/hub/models?refresh=1",
                       headers={"X-Panel-Token": token})
    ids = [m["id"] for m in json.loads(raw)["models"]]
    check("强制刷新能拿到上游新增的模型", "brand-new-model" in ids, ids)

    # 收回 auto，后面的用例按模型清单派发
    http(base, "/api/hub/egress", "POST", {"provider": "auto"},
         headers={"X-Panel-Token": token})

    # ---------------- 日志尾读（进程内运行：读上游的环形缓冲） ----------------
    for i in range(50):
        hub.providers["wb"]._logbuf.append("line-%02d" % i)
    status, raw = http(base, "/api/hub/logs?provider=wb&tail=5",
                       headers={"X-Panel-Token": token})
    lines = json.loads(raw)["lines"]
    check("日志尾读返回最后 5 行",
          status == 200 and lines == ["line-%02d" % i for i in range(45, 50)],
          lines)

    # ---------------- Key 鉴权 ----------------
    S.set_api_keys(data_dir, [{"id": "k1", "name": "默认",
                               "key": "sk-hub-test", "enabled": True}])
    status, raw = http(base, "/v1/models")
    check("配了 Key 之后无 Key 请求 401", status == 401, status)
    status, raw = http(base, "/v1/models",
                       headers={"Authorization": "Bearer sk-hub-test"})
    check("正确 Key 放行", status == 200, status)
    status, raw = http(base, "/v1/models?key=sk-hub-test")
    check("查询串 Key 也认", status == 200, status)

    hub.stop_event.set()
    server.shutdown()
    print("")
    print("SUMMARY: %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
    print("RESULT: %s" % ("RED" if FAIL else "GREEN"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
