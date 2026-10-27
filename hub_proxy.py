#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy × Qoder × ZCode 融合中枢（hub）——一个进程、一个端口，管三个上游。

它做三件事：

1. **托管**：按配置拉起并守护 `wb_proxy.py`（WorkBuddy，默认 8788）与
   `qoder_proxy.py`（Qoder，默认 8790）与 `vendor/zcode2api`（ZCode，默认 8791）。
   三个上游只绑 127.0.0.1，对外只暴露中枢
   这一个端口；任一方崩了，中枢按退避重启它并把原因写进面板。
2. **合并网关**：`/v1/*` 按请求体里的 `model` 选上游——带 `wb/` / `qoder/` 前缀
   强制指定，`routing.overrides` 点名覆盖，同名模型按默认上游，只有一个上游有的
   模型直接走它。响应（含 SSE 流）原样透传，中枢不改写上游的协议细节。
3. **统一看板**：`/` 是融合看板；`/wb/`、`/qoder/`、`/zcode/` 把三个上游的**原始看板**挂到
   同一端口下——HTML 里注入一段 shim，把看板自己的绝对路径请求自动补上挂载
   前缀，所以两个看板的代码一行都不用改。

为什么用子进程而不是把几个 Handler 塞进同一个进程：每个上游各自有 main() 里的
一整套启动逻辑（账号池、调度器、目录、单实例探测、控制台关闭回调），把它们抽成
「可被 import 调用」的库是一次伤筋动骨的重构，而子进程天然带来崩溃隔离——
Qoder 那边把内存吃满时，WorkBuddy 的对话链路不该跟着一起断。

仅使用 Python 标准库。

    python hub_proxy.py                       # 127.0.0.1:8787，同时拉起三个上游
    python hub_proxy.py --port 9000           # 换中枢端口
    python hub_proxy.py --no-spawn            # 只当网关，上游自己另开
    python hub_proxy.py --lan                 # 监听所有网卡（强制要 Key）
"""
import argparse
import atexit
import http.client
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import hub_settings

VERSION = "1.8.0"
ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8787
MAX_PAYLOAD_BYTES = int(os.environ.get("HUB_MAX_PAYLOAD_BYTES",
                                       32 * 1024 * 1024))
# HTML 注入要整份读进内存再改写：两个看板分别是 1.0MB / 320KB，留 12MB 上限既
# 够用又能挡住「上游回了几个 G 的 HTML」这种病态情况。
MAX_INJECT_BYTES = 12 * 1024 * 1024
# 聊天请求可以跑很久（长上下文 / 长思考），中枢这一跳必须比上游更耐心，否则
# 上游还在生成、中枢先把连接掐了。
UPSTREAM_TIMEOUT = float(os.environ.get("HUB_UPSTREAM_TIMEOUT") or 900)
STARTED_AT = time.time()


class PayloadTooLarge(Exception):
    """请求体超过 HUB_MAX_PAYLOAD_BYTES：回 413，别把内存吃穿。"""


def now_iso(stamp=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp or time.time()))


def human_uptime(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd%02dh" % (seconds // 86400, (seconds % 86400) // 3600)

# 两个上游的 main() 都从 sys.argv 读参数，而 sys.argv 是进程全局的：
# 启动期间用这把锁串行化，避免 A 的参数被 B 读走。
_ARGV_LOCK = threading.RLock()

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def normalize_proxy_env():
    """洗一遍 no_proxy：把 `[::1]` 这种带方括号的 IPv6 写法去掉括号。

    为什么需要：zcode2api 用 httpx，而 httpx 把 no_proxy 的每一项当 URL 解析，
    `[::1]` 会被读成「主机 + 端口 :1]」并抛 `Invalid port: ':1]'`——系统代理工具
    （Clash 等）经常这么写，结果就是「ZCode 授权登录初始化失败（HTTP 502）」。
    urllib（wb/qoder 用它）对这种写法是宽容的，所以只洗不删，两边都能用。
    """
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if not raw:
            continue
        parts = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if item.startswith("[") and item.endswith("]"):
                item = item[1:-1]
            parts.append(item)
        cleaned = ",".join(parts)
        if cleaned != raw:
            os.environ[name] = cleaned


def http_json(port, method, path, payload=None, headers=None, timeout=5.0):
    """对 127.0.0.1:port 发一次请求，返回 (status, body_bytes)。

    只在内部聚合（/health、/accounts、/v1/models）用：这些响应都很小，一次读完
    没有内存风险；聊天那种可能很长的响应走 _proxy_to 的流式路径。
    """
    body = None
    hdrs = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def port_alive(port, timeout=0.6):
    """端口上有没有人在听（不做协议判断，只用于挑一个没被占的端口）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def prefix_url(url, mount):
    """把上游给的站内绝对路径补上挂载前缀。

    上游的看板/后台是**独立应用**写的，重定向与静态资源都用 `/admin`、`/static`
    这种根路径。挂到 `/zcode/` 下以后，不补前缀就会打到中枢根上（404），所以
    Location 头和 HTML 里的 href/src/action 都要过一遍。
    """
    if not isinstance(url, str) or not mount:
        return url
    if not url.startswith("/") or url.startswith("//"):
        return url
    if url == mount or url.startswith(mount + "/"):
        return url
    return mount + url


_ATTR_RE = re.compile(
    rb'(?i)\b(href|src|action)=(["\'])(/(?!/)[^"\']*)\2')


def rewrite_absolute_attrs(body, mount):
    """改写 HTML 里的根路径属性（只动 href/src/action，不碰其它字节）。"""
    prefix = mount.encode("utf-8")

    def _sub(match):
        attr, quote, url = match.group(1), match.group(2), match.group(3)
        if url == prefix or url.startswith(prefix + b"/"):
            return match.group(0)
        return attr + b"=" + quote + prefix + url + quote

    return _ATTR_RE.sub(_sub, body)


# 看板注入的 shim：把上游看板的绝对路径请求补上挂载前缀。
# 三个上游的界面都只用 fetch（没有 XHR / EventSource），另有一个 window.open 弹
# 日志窗，所以这三处补全就够；末尾的 <a> 是「回中枢」的返回入口。
_SHIM_TEMPLATE = """<script>
(function(){
  var P = %(prefix)s;
  function fix(u){
    if (typeof u !== 'string' || u.charAt(0) !== '/') return u;
    if (u === P || u.indexOf(P + '/') === 0) return u;
    return P + u;
  }
  var of = window.fetch;
  if (of) window.fetch = function(input, init){
    try {
      if (typeof input === 'string') return of.call(this, fix(input), init);
      if (input && typeof input.url === 'string') {
        return of.call(this, new Request(fix(input.url), input), init);
      }
    } catch (e) {}
    return of.call(this, input, init);
  };
  var oo = window.open;
  if (oo) window.open = function(url){
    if (arguments.length) { try { arguments[0] = fix(url); } catch (e) {} }
    return oo.apply(window, arguments);
  };
  document.addEventListener('click', function(ev){
    var a = ev.target && ev.target.closest ? ev.target.closest('a[href]') : null;
    if (!a) return;
    var href = a.getAttribute('href');
    if (href && href.charAt(0) === '/' && href.indexOf(P + '/') !== 0) {
      ev.preventDefault();
      window.location.href = P + href;
    }
  }, true);
})();
</script>
<a href="/" title="返回融合中枢" style="position:fixed;left:14px;bottom:14px;z-index:2147483000;
   display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border-radius:999px;
   background:rgba(20,24,34,.82);color:#fff;font:600 12px/1 -apple-system,'Segoe UI',sans-serif;
   text-decoration:none;box-shadow:0 6px 20px rgba(0,0,0,.28);backdrop-filter:blur(6px);
   opacity:.55;transition:opacity .15s" onmouseover="this.style.opacity=1"
   onmouseout="this.style.opacity=.55">← 中枢</a>
"""


# ---------------------------------------------------------------------------
# 用量统计：直接读两个上游各自的 usage.jsonl
# ---------------------------------------------------------------------------
class UsageStats(object):
    """按天累计「请求数 / token 数」，增量读 JSONL。

    为什么读文件而不是调上游的 /usage：两个上游的 /usage 响应形状是各自面板的
    内部契约（字段还在演进），而 JSONL 每行是一条独立记录，`at` + `total_tokens`
    这两个字段是写日志的硬约束。读文件还顺带避开了「上游没起来时面板一片空白」。
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._offset = 0
        self._buffer = b""
        self._day = ""
        self._requests = 0
        self._tokens = 0

    def _reset(self):
        self._requests = 0
        self._tokens = 0

    def _roll_day(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._reset()

    def _consume(self, raw_lines):
        for raw in raw_lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            at = row.get("at")
            try:
                stamp = float(at)
            except (TypeError, ValueError):
                continue
            if time.strftime("%Y-%m-%d", time.localtime(stamp)) != self._day:
                continue
            self._requests += 1
            total = row.get("total_tokens")
            if not isinstance(total, (int, float)):
                total = 0
                for field in ("prompt_tokens", "completion_tokens"):
                    value = row.get(field)
                    if isinstance(value, (int, float)):
                        total += value
            if isinstance(total, (int, float)) and total > 0:
                self._tokens += int(total)

    def _read_new(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self._offset:
            # 日志被轮转/清空：从头重来，否则会漏掉新文件的前半段。
            self._offset = 0
            self._buffer = b""
            self._reset()
        if size == self._offset:
            return
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(size - self._offset)
        except OSError:
            return
        self._offset = size
        self._buffer += chunk
        *lines, self._buffer = self._buffer.split(b"\n")
        self._consume(lines)
        # 只留最后一行不完整的尾巴；正常行已经消费掉了。
        if len(self._buffer) > 4 * 1024 * 1024:
            self._buffer = b""

    def today(self):
        with self._lock:
            self._roll_day()
            self._read_new()
            return {"requests": self._requests, "tokens": self._tokens}


# ---------------------------------------------------------------------------
# 上游进程
# ---------------------------------------------------------------------------
class ProviderRuntime(object):
    """一个上游的运行时：**在本进程里**跑，不再起子进程。

    用户要的是「一个反代工具」：一个 `python hub_proxy.py` 就同时提供
    WorkBuddy / Qoder / ZCode，任务管理器里只有一个进程，对外只有一个端口。
    代价是失去崩溃隔离（上游的异常会直接影响中枢），换来的是没有孤儿进程、
    没有端口打架、没有三份日志文件。

    两种上游的启动方式：
      proxy（wb/qoder）：import 模块 → 临时改 sys.argv → 线程里调用它的 main()。
        main() 内部建 server 并阻塞在 serve_forever；这里用一个记录型的 server
        子类捕获实例，停止时从外部调 shutdown()。上游代码一行都不用改。
      zcode：uvicorn 在后台线程里跑 vendor/zcode2api 的 ASGI app。
    """

    def __init__(self, hub, pid):
        self.hub = hub
        self.pid = pid
        self.spec = hub_settings.PROVIDER_SPECS[pid]
        self.state = "stopped"
        self.port = self.spec["port"]
        self.started_at = 0.0
        self.restarts = 0
        self.last_error = ""
        self._stop_requested = False
        self._lock = threading.RLock()
        self._health_at = 0.0
        self._health = {}
        self._key_cache = None
        self._panel_token = ""
        self._panel_token_at = 0.0
        self._panel_error = ""
        self._server = None
        self._uvicorn = None
        self._thread = None
        self._module = None
        self._capture_installed = False
        # 进程内运行后没有「每个上游一个日志文件」了：面板的「运行日志」改成读
        # 这个环形缓冲（上游的 log()/uvicorn 日志都会写进来）。
        self._logbuf = deque(maxlen=2000)

    # ---- 目录 ----
    def data_root(self):
        if self.spec["data_dir"]:
            return os.path.join(ROOT, self.spec["data_dir"])
        return ROOT

    def accounts_dir(self):
        if self.spec["kind"] == "zcode":
            # zcode2api 用一个 SQLite（accounts.db）存全部账号，没有「每账号一个
            # 文件」的目录；数据目录本身就是账号目录。
            return self.data_root()
        if self.spec["data_dir"]:
            return os.path.join(self.data_root(), "accounts")
        return os.path.join(ROOT, "accounts")

    def usage_dir(self):
        if self.spec["kind"] == "zcode":
            return self.data_root()
        if self.spec["data_dir"]:
            return os.path.join(self.data_root(), "usage")
        return os.path.join(ROOT, "usage")

    def cwd(self):
        """zcode2api 必须在自己目录里跑（import app.* 与相对定位 captcha_node）。"""
        if self.spec.get("cwd"):
            return os.path.join(ROOT, self.spec["cwd"])
        return ROOT

    def panel_key(self):
        """上游自己配置的一把可用 Key（中枢拿它去访问上游的 /v1 与 /accounts）。

        为什么需要：wb/qoder 的鉴权规则是「面板里配了 Key 之后，只认面板 Key」——
        命令行 --api-key 会被忽略（防止启动脚本里遗留的弱 Key 在面板锁死后仍然
        可用）。所以中枢不能只靠内部密钥：必须优先用上游自己的 Key，只有在它
        一把都没配时才回落到 --api-key。

        读 settings.json 而不是 import 上游模块：中枢不该把 wb_settings /
        qoder_settings 拖进来（它们的目录、缓存、线程模型都是上游自己的）。
        """
        path = os.path.join(self.accounts_dir(), "settings.json")
        try:
            info = os.stat(path)
            stamp = (info.st_mtime, info.st_size)
        except OSError:
            stamp = None
        cached = self._key_cache
        if cached and cached[0] == stamp:
            return cached[1]
        key = ""
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
        if isinstance(data, dict):
            rows = data.get("api_keys")
            if isinstance(rows, list):
                live = [entry for entry in rows
                        if isinstance(entry, dict) and entry.get("enabled")
                        and entry.get("key") and not entry.get("deleted_at")]
                # 优先「无限制」的 Key：上游会按 Key 的 models / realm 做二次校验，
                # 随便挑一把带限制的会让中枢的**所有**流量都继承那份限制
                # （表现是「明明模型目录里有，却 400 说这个 Key 不允许」）。
                for entry in live:
                    if not (entry.get("models") or entry.get("realm")):
                        key = str(entry["key"])
                        break
                if not key and live:
                    key = str(live[0]["key"])
            if not key and data.get("api_key_set") and data.get("api_key"):
                key = str(data["api_key"])       # 老版本的单 Key 形状
        self._key_cache = (stamp, key)
        return key

    def panel_session(self, force=False):
        """登录上游面板拿一个会话 token（中枢读 /accounts 用）。

        为什么必须登录：wb/qoder 把管理接口（/accounts、/usage、/settings）放在
        **面板会话**闸门后面，API Key 只能进 /v1。中枢拿不到会话就看不到账号表，
        所以这里用上游自己的面板密码（默认 admin，可在 hub 设置里逐上游覆盖）
        登一次，token 缓存到失效为止。

        登录失败不是错误：用户可能改了上游面板密码。此时账号表为空，概览里会
        带一句提示，不影响网关转发。
        """
        if self.spec["kind"] == "zcode":
            return ""
        with self._lock:
            token = self._panel_token
            if token and not force and time.time() < self._panel_token_at:
                return token
        password = str(hub_settings.option(
            self.hub.data_dir, self.pid, "panel_password",
            hub_settings.DEFAULT_PANEL_PASSWORD) or "")
        token = ""
        try:
            status, raw = http_json(self.port, "POST", "/panel/login",
                                    payload={"password": password}, timeout=5.0)
            if status == 200:
                payload = json.loads(raw.decode("utf-8", "replace"))
                token = str(payload.get("token") or "")
        except Exception:
            token = ""
        with self._lock:
            self._panel_token = token
            # 上游重启会清空会话；10 分钟主动换一次，避免「重启后账号表一直空」。
            self._panel_token_at = time.time() + (600 if token else 0)
            if token:
                self._panel_error = ""
            else:
                self._panel_error = ("上游面板密码不是默认值？在 data/hub/settings.json "
                                     "的 providers.%s.panel_password 里配置后重启中枢"
                                     % self.pid)
        return token

    def admin_headers(self):
        """访问 zcode 后台 API 的鉴权头（它的后台走 Bearer，不走面板会话）。"""
        if self.spec["kind"] != "zcode":
            return {}
        return {"Authorization": "Bearer "
                + hub_settings.zcode_admin_key(self.hub.data_dir)}

    # ---- 启动 ----
    def argv(self):
        """给 wb/qoder 的 main() 用的命令行（sys.argv 会被临时替换成它）。"""
        cfg = hub_settings.providers(self.hub.data_dir)[self.pid]
        script = os.path.join(ROOT, self.spec["script"])
        cmd = [script,
               "--host", "127.0.0.1",
               "--port", str(self.port if self.port is not None else cfg["port"]),
               "--accounts-dir", self.accounts_dir(),
               "--usage-dir", self.usage_dir()]
        if self.hub.internal_key:
            cmd += ["--api-key", self.hub.internal_key]
        return cmd

    def missing_deps(self):
        """zcode2api 的 Python 依赖是否齐（本仓其它部分只用标准库）。"""
        if self.spec["kind"] != "zcode":
            return []
        import importlib.util
        missing = []
        for module in ("fastapi", "uvicorn", "httpx", "dotenv"):
            try:
                if importlib.util.find_spec(module) is None:
                    missing.append(module)
            except Exception:
                missing.append(module)
        return missing

    def _load_module(self):
        if self._module is not None:
            return self._module
        if self.pid == "wb":
            import wb_proxy as module
        elif self.pid == "qoder":
            import qoder_proxy as module
        else:
            raise RuntimeError("no module for %s" % self.pid)
        self._module = module
        return module

    def _install_log_capture(self):
        """把上游 log() 的输出同时收进环形缓冲（面板「运行日志」用）。"""
        if self._capture_installed:
            return
        module = self._load_module()
        original = getattr(module, "log", None)
        if original is None:
            return
        buf = self._logbuf

        def _capture(message, *args, **kwargs):
            try:
                buf.append("%s %s" % (time.strftime("%H:%M:%S"), message))
            except Exception:
                pass
            return original(message, *args, **kwargs)

        module.log = _capture
        self._capture_installed = True

    def _start_proxy(self):
        module = self._load_module()
        self._install_log_capture()
        server_attr = "GatewayServer" if self.pid == "wb" else "ThreadingHTTPServer"
        original_cls = getattr(module, server_attr)
        captured = {}
        state = {"error": None}

        class _Tracked(original_cls):
            def __init__(self, *args, **kwargs):
                original_cls.__init__(self, *args, **kwargs)
                captured["server"] = self

        def _run():
            try:
                module.main()
            except SystemExit:
                pass
            except BaseException as exc:      # noqa: BLE001 - 线程里不能静默丢
                state["error"] = exc

        setattr(module, server_attr, _Tracked)
        argv = self.argv()
        try:
            with _ARGV_LOCK:
                saved = sys.argv
                sys.argv = argv
                try:
                    thread = threading.Thread(target=_run, daemon=True,
                                              name="hub-%s" % self.pid)
                    thread.start()
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if "server" in captured or state["error"] is not None:
                            break
                        if not thread.is_alive():
                            break
                        time.sleep(0.05)
                finally:
                    sys.argv = saved
        finally:
            setattr(module, server_attr, original_cls)

        if state["error"] is not None:
            raise RuntimeError("上游启动异常：%s" % state["error"])
        server = captured.get("server")
        if server is None:
            raise RuntimeError("上游没有创建 HTTP server（端口被占用或启动逻辑提前返回）")
        self._server = server
        self._thread = thread
        # 端口 0 时真实端口由内核分配，必须从 socket 读回来。
        try:
            self.port = server.server_address[1]
        except Exception:
            pass

    def _start_zcode(self):
        vendor = self.cwd()
        if vendor not in sys.path:
            sys.path.insert(0, vendor)
        os.environ["ZCODE_HOST"] = "127.0.0.1"
        os.environ["ZCODE_DATA_DIR"] = self.accounts_dir()
        os.environ["ZCODE_ADMIN_KEY"] = hub_settings.zcode_admin_key(
            self.hub.data_dir)
        import uvicorn
        from app.main import app as zapp
        config = uvicorn.Config(zapp, host="127.0.0.1",
                                port=int(self.port or 0),
                                log_level="info", access_log=False)
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True,
                                  name="hub-zcode")
        thread.start()
        deadline = time.time() + 40
        while time.time() < deadline and not getattr(server, "started", False):
            if not thread.is_alive():
                break
            time.sleep(0.05)
        if not getattr(server, "started", False):
            raise RuntimeError("zcode2api 没能在 40 秒内启动（看 uvicorn 日志）")
        self._uvicorn = server
        self._thread = thread
        try:
            self.port = server.servers[0].sockets[0].getsockname()[1]
        except Exception:
            pass

    def start(self, reason="manual"):
        with self._lock:
            if self.hub.attach_mode:
                self.state = "external"
                return False
            if self._thread is not None and self._thread.is_alive():
                return False
            cfg = hub_settings.providers(self.hub.data_dir)[self.pid]
            if not cfg.get("enabled", True):
                self.state = "disabled"
                return False
            missing = self.missing_deps()
            if missing:
                self.state = "failed"
                self.last_error = (
                    "缺少 Python 依赖（%s）。安装：pip install -r %s"
                    % ("、".join(missing), self.spec.get("requirements")))
                # WARN 而不是 ERROR：没装 ZCode 依赖的用户只是少一个上游，
                # 中枢本身是好的，不该在日志里看起来像启动失败。
                self.hub.log("[%s] %s" % (self.pid, self.last_error), level="WARN")
                return False
            port = cfg["port"]
            if port and port_alive(port):
                # 进程内运行不再需要固定端口；被占用（常见：用户还开着独立的
                # wb_proxy）就改用内核分配的临时端口，避免整个中枢起不来。
                self.hub.log("[%s] 端口 %d 已被占用，改用临时端口"
                             % (self.pid, port), level="WARN")
                port = 0
            self.port = port
            self.state = "starting"
            self.last_error = ""
            self._stop_requested = False
            self.started_at = time.time()
            try:
                if self.spec["kind"] == "zcode":
                    self._start_zcode()
                else:
                    self._start_proxy()
            except Exception as exc:
                self.state = "failed"
                self.last_error = "启动失败：%s" % exc
                self.hub.log("[%s] %s" % (self.pid, self.last_error), level="ERROR")
                return False
            self.hub.log("[%s] 已在本进程启动，端口 %d（%s）"
                         % (self.pid, self.port, reason))
        self._ready_thread = threading.Thread(
            target=self._wait_ready, name="hub-ready-%s" % self.pid, daemon=True)
        self._ready_thread.start()
        return True

    def _wait_ready(self):
        deadline = time.time() + self.hub.startup_timeout
        while time.time() < deadline:
            if self._stop_requested:
                return
            if self._thread is not None and not self._thread.is_alive():
                self.state = "failed"
                self.last_error = "上游线程退出，详见中枢日志"
                self.hub.log("[%s] %s" % (self.pid, self.last_error), level="ERROR")
                return
            health = self.health(force=True)
            if health:
                with self._lock:
                    self.state = "running"
                self.hub.log("[%s] 就绪：%s"
                             % (self.pid, health.get("version") or "?"))
                return
            time.sleep(0.4)
        with self._lock:
            if self.state == "starting":
                self.state = "failed"
                self.last_error = "%d 秒内没等到探活响应" % int(self.hub.startup_timeout)
        self.hub.log("[%s] %s" % (self.pid, self.last_error), level="ERROR")

    def stop(self, grace=10.0):
        with self._lock:
            self._stop_requested = True
            server = self._server
            uvicorn_server = self._uvicorn
            thread = self._thread
            self._server = None
            self._uvicorn = None
            self._thread = None
        if uvicorn_server is not None:
            try:
                uvicorn_server.should_exit = True
            except Exception:
                pass
        if server is not None:
            try:
                # shutdown() 会等 serve_forever 退出，必须从**别的线程**调用。
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=grace)
        self.state = "stopped"
        self.hub.log("[%s] 已停止" % self.pid)
        return True

    def restart(self):
        self.stop()
        time.sleep(0.3)
        self.restarts += 1
        return self.start(reason="restart")

    # ---- 观测 ----
    def health(self, force=False, ttl=3.0):
        now = time.time()
        if not force and now - self._health_at < ttl:
            return self._health
        data = None
        probe = self.spec.get("health_path") or "/health"
        try:
            status, raw = http_json(self.port, "GET", probe, timeout=2.5)
            if status == 200:
                parsed = json.loads(raw.decode("utf-8", "replace"))
                # 本仓的 /health 带 "ok": true；zcode2api 的 /meta 只回版本号，
                # 所以判据是「200 + JSON 对象 + 没有明确的 ok:false」。
                if isinstance(parsed, dict) and parsed.get("ok") is not False:
                    data = parsed
        except Exception:
            data = None
        self._health = data or {}
        self._health_at = now
        if data and self.state in ("starting", "failed", "stopped", "external"):
            self.state = "running"
        return self._health

    def snapshot(self):
        health = self.health()
        cfg = hub_settings.providers(self.hub.data_dir)[self.pid]
        usage = self.hub.usage_stats(self.pid).today()
        models = self.hub.models.count_for(self.pid)
        state = self.state
        if self.hub.attach_mode and health:
            state = "running"
        accounts = health.get("accounts") or 0
        ready = health.get("accounts_ready") or 0
        version = health.get("version") or ""
        if not version and self._module is not None:
            # wb 的 /health 不带版本号、也没有 VERSION 常量（只有 Handler.server_version
            # 这个 "wb-proxy/1.7.33" 形式）；进程内运行时模块就在手边，直接读。
            version = str(getattr(self._module, "VERSION", "") or "")
            if not version:
                handler = getattr(self._module, "Handler", None)
                raw = str(getattr(handler, "server_version", "") or "")
                version = raw.split("/", 1)[-1] if "/" in raw else ""
        extra = {}
        if self.spec["kind"] == "zcode":
            # zcode2api 的 /meta 不带账号信息，账号池要问它的后台 API；
            # 用量也只有累计调用数（没有按天的 usage.jsonl）。
            stats = self.hub.zcode_stats()
            accounts = stats.get("total") or 0
            ready = stats.get("active") or 0
            extra = {"total_requests": stats.get("calls"),
                     "failed_requests": stats.get("fail")}
        snapshot = {
            "id": self.pid,
            "label": self.spec["label"],
            "kind": self.spec["kind"],
            "protocol": self.spec["protocol"],
            "state": state,
            "port": self.port if self.port is not None else cfg["port"],
            "mount": self.spec["mount"] + "/",
            "version": version,
            "realm": health.get("realm") or "",
            "accounts": accounts,
            "accounts_ready": ready,
            "models": models,
            "today_requests": usage["requests"],
            "today_tokens": usage["tokens"],
            "restarts": self.restarts,
            "last_error": self.last_error or None,
            "uptime_s": int(time.time() - self.started_at) if self.started_at else 0,
        }
        snapshot.update(extra)
        return snapshot

    def tail_log(self, lines=200):
        try:
            rows = list(self._logbuf)
        except Exception:
            return []
        return rows[-max(1, min(int(lines or 200), 2000)):]


class ModelIndex(object):
    """三个上游的模型清单合并成一张表，并负责「模型名 -> 上游」的解析。

    同名模型的处理：按 PROVIDER_IDS 的顺序，先到先得拿裸名字，后到的加
    `<provider>/` 前缀（`auto` 两边都有，所以 Qoder 的 auto 会以 `qoder/auto`
    出现）。这样「原本就在用的模型名」不会因为融合而失效——老客户端配置一行
    都不用改。
    """

    TTL = 60.0

    def __init__(self, hub):
        self.hub = hub
        self._lock = threading.RLock()
        self._entries = []
        self._by_id = {}
        self._at = 0.0
        self._refreshing = False

    def refresh(self, force=False):
        with self._lock:
            if not force and self._at and time.time() - self._at < self.TTL:
                return self._entries
            if self._refreshing:
                return self._entries
            self._refreshing = True
        try:
            entries = []
            seen = set()
            for pid in hub_settings.PROVIDER_IDS:
                runtime = self.hub.provider(pid)
                if not runtime.health():
                    continue
                try:
                    status, raw = http_json(
                        runtime.port, "GET",
                        runtime.spec.get("models_path") or "/v1/models",
                        headers=self.hub.provider_credentials(pid),
                        timeout=8.0)
                    if status != 200:
                        continue
                    payload = json.loads(raw.decode("utf-8", "replace"))
                except Exception as exc:
                    self.hub.log("[models] 读取 %s 模型失败：%s" % (pid, exc),
                                 level="WARN")
                    continue
                rows = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(rows, list):
                    continue
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    mid = str(item.get("id") or "").strip()
                    if not mid:
                        continue
                    exposed = mid
                    if exposed in seen:
                        exposed = "%s/%s" % (pid, mid)
                    if exposed in seen:
                        continue
                    seen.add(exposed)
                    entries.append({
                        "id": exposed,
                        "route_id": mid,
                        "provider": pid,
                        "protocol": runtime.spec["protocol"],
                        "realm": str(item.get("realm") or ""),
                        "owned_by": str(item.get("owned_by")
                                        or runtime.spec["label"]),
                    })
            with self._lock:
                self._entries = entries
                self._by_id = {e["id"]: e for e in entries}
                self._at = time.time()
            return entries
        finally:
            with self._lock:
                self._refreshing = False

    def entries(self):
        # 每次取都过一遍 refresh()：它内部有 TTL 与「正在刷新」两个闸门，
        # 这样上游加了新模型后最多 TTL 秒就能出现在 /v1/models，不必等后台循环
        # 或者重启（用户的要求：模型列表要和客户端里的最新清单一致）。
        self.refresh()
        return self._entries

    def count(self):
        """已缓存的模型数——**不触发刷新**。

        /health 会被启动脚本、监控、面板每几秒打一次，如果它顺手去拉两个上游的
        模型清单，第一次探活就会卡上十几秒（看起来像「起不来」）。
        """
        return len(self._entries)

    def count_for(self, pid):
        return len([e for e in self._entries if e["provider"] == pid])

    def providers_for(self, model):
        """这个模型在哪些上游的目录里（用于「当前出口没有它」的提前拒绝）。"""
        model = str(model or "").strip()
        out = set()
        if not model:
            return out
        for entry in self.entries():
            if entry["id"] == model or entry["route_id"] == model:
                out.add(entry["provider"])
        return out

    def public(self):
        """面板要的形状：把 routing 的覆盖算进 provider，客户端看到的就是实际去向。"""
        routing = hub_settings.routing(self.hub.data_dir)
        out = []
        for entry in self.entries():
            provider = entry["provider"]
            if entry["id"] in routing["overrides"]:
                provider = routing["overrides"][entry["id"]]
            elif entry["route_id"] in routing["overrides"]:
                provider = routing["overrides"][entry["route_id"]]
            item = dict(entry)
            item["provider"] = provider
            out.append(item)
        return out

    def resolve(self, model):
        """模型名 -> (provider, 转发时用的真实模型名)。

        规则顺序（与 docs/hub-api.md 一致）：
          1. `wb/` / `qoder/` 前缀强制指定；
          2. routing.overrides 点名；
          3. 合并表里精确命中；
          4. 裸 route_id 命中（客户端直接用上游真实名）；
          5. 兜底给 default_provider，让上游自己去报「没有这个模型」。
        """
        model = str(model or "").strip()
        routing = hub_settings.routing(self.hub.data_dir)
        default = routing["default_provider"]
        if not model:
            return default, model
        for pid in hub_settings.PROVIDER_IDS:
            prefix = pid + "/"
            if model.startswith(prefix) and len(model) > len(prefix):
                return pid, model[len(prefix):]
        if model in routing["overrides"]:
            return routing["overrides"][model], model
        entries = self.entries()
        owners = [e for e in entries
                  if e["id"] == model or e["route_id"] == model]
        # 当前出口（偏好）：只决定「多家都有 / 目录里查不到」的模型走哪家。
        # 只在一家出现的模型必须仍然走那一家——否则「用 Qoder 的模型、出口配了 wb」
        # 会变成一个必然失败的组合，正确转发优先于出口偏好。
        active = routing.get("active_provider") or "auto"
        if active != "auto":
            if not owners:
                return active, model
            for entry in owners:
                if entry["provider"] == active:
                    return active, entry["route_id"]
            return owners[0]["provider"], owners[0]["route_id"]
        entry = self._by_id.get(model)
        if entry is not None:
            provider = entry["provider"]
            if entry["id"] in routing["overrides"]:
                provider = routing["overrides"][entry["id"]]
            elif entry["route_id"] in routing["overrides"]:
                provider = routing["overrides"][entry["route_id"]]
            return provider, entry["route_id"]
        for candidate in entries:
            if candidate["route_id"] == model:
                return candidate["provider"], model
        return default, model


# ---------------------------------------------------------------------------
# 中枢本体
# ---------------------------------------------------------------------------
class Hub(object):
    def __init__(self, data_dir, cli_key="", lan=False, attach=False,
                 auto_restart=True, startup_timeout=45.0):
        self.data_dir = data_dir
        normalize_proxy_env()
        self.logs_dir = os.path.join(data_dir, "logs")
        self.cli_key = (cli_key or "").strip()
        self.lan = lan
        self.attach_mode = attach
        self.auto_restart = auto_restart
        self.startup_timeout = startup_timeout
        # 中枢 -> 上游的内部密钥：上游只认它，客户端那把 Key 由中枢校验。
        self.internal_key = "" if attach else ("hub-internal-" + secrets.token_hex(16))
        self.sessions = hub_settings.PanelSessions()
        self.providers = {pid: ProviderRuntime(self, pid)
                          for pid in hub_settings.PROVIDER_IDS}
        self.models = ModelIndex(self)
        self._usage = {}
        self._accounts_cache = {}
        self._zcode_cache = None
        self._accounts_lock = threading.RLock()
        self.stop_event = threading.Event()
        self._log_lock = threading.Lock()
        self._log_seq = 0
        self._log_ring = deque(maxlen=2000)
        self._monitor = None
        os.makedirs(self.logs_dir, exist_ok=True)
        os.makedirs(data_dir, exist_ok=True)

    # ---- 基础 ----
    def log(self, message, level="INFO"):
        line = "[%s] %s %s" % (time.strftime("%H:%M:%S"), level, message)
        try:
            print(line, flush=True)
        except Exception:
            pass
        # console_tail.py 通过 /logs 轮询这个缓冲（pythonw 静默启动时 stdout
        # 可能是 None，日志文件里什么都收不到，HTTP 轮询才是可靠通道）。
        try:
            with self._log_lock:
                self._log_seq += 1
                self._log_ring.append({
                    "id": self._log_seq,
                    "time": time.strftime("%H:%M:%S"),
                    "level": level,
                    "tag": "",
                    "msg": str(message),
                })
        except Exception:
            pass

    def provider(self, pid):
        return self.providers.get(pid)

    def internal_headers(self):
        if not self.internal_key:
            return {}
        return {"Authorization": "Bearer " + self.internal_key}

    def provider_credentials(self, pid):
        """中枢访问某个上游时要带的鉴权头（可能为空）。

        顺序：上游自己面板里配的 Key > 本次运行生成的内部密钥 > 不带。
        zcode 不参与：它的网关 Key 由它自己的设置管，中枢留空（只绑本机），
        后台 API 的 Bearer 是浏览器直接带给它的，中枢不能覆盖。
        """
        runtime = self.providers[pid]
        if runtime.spec["kind"] == "zcode":
            return {}
        key = runtime.panel_key()
        if key:
            return {"Authorization": "Bearer " + key}
        return self.internal_headers()

    def usage_stats(self, pid):
        if pid not in self._usage:
            runtime = self.providers[pid]
            self._usage[pid] = UsageStats(
                os.path.join(runtime.usage_dir(), "usage.jsonl"))
        return self._usage[pid]

    # ---- 鉴权 ----
    def auth_required(self):
        return hub_settings.auth_required(self.data_dir, self.cli_key)

    def match_key(self, supplied):
        return hub_settings.match_api_key(self.data_dir, supplied,
                                          extra_keys=(self.cli_key,))

    def panel_ok(self, token):
        return self.sessions.valid(token)

    # ---- 生命周期 ----
    def start_all(self):
        for pid in hub_settings.PROVIDER_IDS:
            runtime = self.providers[pid]
            cfg = hub_settings.providers(self.data_dir)[pid]
            if not cfg.get("enabled", True):
                runtime.state = "disabled"
                continue
            if self.attach_mode:
                runtime.port = cfg["port"]
                runtime.state = "external"
                continue
            runtime.start(reason="boot")
        self._monitor = threading.Thread(target=self._monitor_loop,
                                         name="hub-monitor", daemon=True)
        self._monitor.start()
        # 模型目录要有人在后台保温：/health 与启动脚本都只读缓存（不触发刷新），
        # 没有这个循环的话，/health 里的 models 会一直是 0，直到有人打开面板。
        threading.Thread(target=self._models_loop,
                         name="hub-models", daemon=True).start()

    def _models_loop(self):
        if self.stop_event.wait(5.0):      # 给上游一点启动时间
            return
        while True:
            try:
                if any(self.providers[pid].health()
                       for pid in hub_settings.PROVIDER_IDS):
                    self.models.refresh(force=True)
            except Exception as exc:
                self.log("[models] 刷新失败：%s" % exc, level="WARN")
            # 45 秒一轮：模型清单变得很慢（上游按天变），但面板要能尽快看到
            # 新加的账号带出来的新模型。
            if self.stop_event.wait(45.0):
                return

    def _monitor_loop(self):
        while not self.stop_event.wait(2.0):
            for pid in hub_settings.PROVIDER_IDS:
                runtime = self.providers[pid]
                thread = runtime._thread
                if thread is None or runtime._stop_requested:
                    continue
                if thread.is_alive():
                    continue
                if self.attach_mode:
                    continue
                runtime.state = "failed"
                runtime.last_error = "上游线程退出"
                self.log("[%s] 上游线程退出" % pid, level="WARN")
                if (self.auto_restart and not self.stop_event.is_set()
                        and runtime.restarts < 5):
                    runtime.restarts += 1
                    delay = min(30, 2 ** runtime.restarts)
                    self.log("[%s] %d 秒后自动重启（第 %d 次）"
                             % (pid, delay, runtime.restarts), level="WARN")
                    threading.Thread(target=self._delayed_restart,
                                     args=(runtime, delay), daemon=True).start()

    def _delayed_restart(self, runtime, delay):
        if self.stop_event.wait(delay):
            return
        runtime.start(reason="auto-restart")

    def stop_all(self):
        self.stop_event.set()
        for pid in hub_settings.PROVIDER_IDS:
            try:
                self.providers[pid].stop()
            except Exception as exc:
                self.log("[%s] 停止失败：%s" % (pid, exc), level="WARN")

    # ---- 聚合 ----
    def zcode_stats(self, ttl=10.0):
        """zcode2api 的账号池概览（total/active/exhausted/... + calls/fail）。

        它没有 /health 那样的公开汇总，账号池只在后台 API 里，所以要带后台密钥
        去问；密钥对不上时安静地返回空字典（面板显示 0，不影响其它上游）。
        """
        now = time.time()
        with self._accounts_lock:
            cached = self._zcode_cache
            if cached and now - cached[0] < ttl:
                return cached[1]
        stats = {}
        runtime = self.providers.get("zcode")
        if runtime is not None and runtime.health():
            try:
                status, raw = http_json(
                    runtime.port, "GET", "/admin/api/accounts",
                    headers={"Authorization": "Bearer "
                             + hub_settings.zcode_admin_key(self.data_dir)},
                    timeout=8.0)
                if status == 200:
                    payload = json.loads(raw.decode("utf-8", "replace"))
                    if isinstance(payload, dict):
                        stats = payload.get("stats") if isinstance(
                            payload.get("stats"), dict) else {}
            except Exception as exc:
                self.log("[zcode] 读取账号池失败：%s" % exc, level="WARN")
        with self._accounts_lock:
            self._zcode_cache = (now, stats)
        return stats

    def fetch_accounts(self, pid, ttl=10.0):
        runtime = self.providers[pid]
        if not runtime.health():
            return []
        now = time.time()
        with self._accounts_lock:
            cached = self._accounts_cache.get(pid)
            if cached and now - cached[0] < ttl:
                return cached[1]
        rows = []
        try:
            if runtime.spec["kind"] == "zcode":
                status, raw = http_json(
                    runtime.port, "GET", "/admin/api/accounts",
                    headers={"Authorization": "Bearer "
                             + hub_settings.zcode_admin_key(self.data_dir)},
                    timeout=8.0)
            else:
                # ?realm=all：两个上游的 /accounts 默认只回「当前区域」的账号，
                # 中枢要的是**全部**账号（看板按上游分组展示，不跟着区域切换变）。
                # 管理接口在面板会话闸门后面，所以带 X-Panel-Token；会话过期
                # （上游重启）时重新登录一次再试。
                status, raw = 0, b""
                for attempt in (0, 1):
                    token = runtime.panel_session(force=attempt > 0)
                    if not token:
                        break
                    status, raw = http_json(
                        runtime.port, "GET", "/accounts?realm=all",
                        headers={"X-Panel-Token": token}, timeout=8.0)
                    if status == 200:
                        break
            if status == 200:
                payload = json.loads(raw.decode("utf-8", "replace"))
                rows = payload.get("accounts") if isinstance(payload, dict) else []
                if not isinstance(rows, list):
                    rows = []
        except Exception as exc:
            self.log("[%s] 读取账号失败：%s" % (pid, exc), level="WARN")
            rows = []
        with self._accounts_lock:
            self._accounts_cache[pid] = (now, rows)
        return rows

    def merged_accounts(self):
        out = []
        for pid in hub_settings.PROVIDER_IDS:
            kind = hub_settings.PROVIDER_SPECS[pid]["kind"]
            for row in self.fetch_accounts(pid):
                if not isinstance(row, dict):
                    continue
                if kind == "zcode":
                    # zcode 的账号视图：id/name/status/quota/use_count/last_error
                    status = str(row.get("status") or "")
                    out.append({
                        "provider": pid,
                        "uid": str(row.get("id") or ""),
                        "nickname": row.get("name") or "",
                        "realm": row.get("provider") or "",
                        "enabled": bool(row.get("enabled", True)),
                        "token_expired": status in ("invalid", "exhausted"),
                        "in_cooldown": status == "cooling",
                        "last_error": row.get("last_error") or (
                            "" if status in ("active", "") else status),
                        "weight": None,
                        "note": row.get("plan") or "",
                    })
                    continue
                out.append({
                    "provider": pid,
                    "uid": row.get("uid") or "",
                    "nickname": row.get("nickname") or row.get("name") or "",
                    "realm": row.get("realm") or "",
                    "enabled": bool(row.get("enabled", True)),
                    "token_expired": bool(row.get("tokenExpired")
                                          or row.get("needRelogin")),
                    "in_cooldown": bool(row.get("inCooldown")),
                    "last_error": row.get("lastError") or "",
                    "weight": row.get("weight"),
                    "note": row.get("note") or "",
                })
        return out


    # ---- 统一账号管理（登录 / 启停 / 删除 / 导入） ----
    def _provider_or_raise(self, pid):
        runtime = self.providers.get(str(pid or ""))
        if runtime is None:
            raise ValueError("未知上游：%s" % pid)
        if not runtime.health():
            raise ValueError("%s 上游未就绪（state=%s）"
                             % (runtime.spec["label"], runtime.state))
        return runtime

    def account_login_start(self, pid, realm=""):
        """发起某个上游的账号登录，返回 {state, auth_url}。

        wb/qoder 走它们自己的设备授权流（/accounts/login/start），zcode 走它后台
        的 Z.AI OAuth（/admin/api/login/start）。两者形状不同，这里统一成
        `state`（轮询用）+ `auth_url`（给用户点）。
        """
        runtime = self._provider_or_raise(pid)
        if runtime.spec["kind"] == "zcode":
            status, raw = http_json(runtime.port, "POST", "/admin/api/login/start",
                                    headers=runtime.admin_headers(), timeout=40)
            if status != 200:
                detail = ""
                try:
                    data = json.loads(raw.decode("utf-8", "replace"))
                    detail = str(data.get("detail") or data.get("error") or "")
                except Exception:
                    detail = ""
                raise ValueError(
                    "zcode 授权登录初始化失败：%s。"
                    "如果本机访问不了 z.ai，可以在同一个弹窗里改用「粘贴凭证」"
                    "（JWT 或 API Key）直接导入。"
                    % (detail or ("HTTP %s" % status)))
            data = json.loads(raw.decode("utf-8", "replace"))
            return {"provider": pid, "state": data.get("flow_id") or "",
                    "auth_url": data.get("authorize_url") or ""}
        token = runtime.panel_session()
        if not token:
            raise ValueError(runtime._panel_error or "无法登录上游面板")
        payload = {"platform": "CLI"}
        if realm in ("intl", "cn"):
            payload["realm"] = realm
        status, raw = http_json(runtime.port, "POST", "/accounts/login/start",
                                payload=payload,
                                headers={"X-Panel-Token": token}, timeout=40)
        if status != 200:
            raise ValueError("%s 登录初始化失败（HTTP %s）"
                             % (runtime.spec["label"], status))
        data = json.loads(raw.decode("utf-8", "replace"))
        return {"provider": pid, "state": data.get("state") or "",
                "auth_url": data.get("authUrl") or data.get("url") or ""}

    def account_login_poll(self, pid, state):
        runtime = self._provider_or_raise(pid)
        state = str(state or "").strip()
        if not state:
            raise ValueError("缺少 state")
        if runtime.spec["kind"] == "zcode":
            status, raw = http_json(
                runtime.port, "GET", "/admin/api/login/poll/%s"
                % urllib.parse.quote(state, safe=""),
                headers=runtime.admin_headers(), timeout=40)
            if status == 404:
                return {"status": "expired", "message": "登录会话不存在或已过期"}
            if status != 200:
                raise ValueError("zcode 轮询失败（HTTP %s）" % status)
            data = json.loads(raw.decode("utf-8", "replace"))
            return {"status": data.get("status") or "pending",
                    "message": data.get("message") or "",
                    "account": data.get("account")}
        token = runtime.panel_session()
        status, raw = http_json(
            runtime.port, "GET", "/accounts/login/poll?state=%s"
            % urllib.parse.quote(state, safe=""),
            headers={"X-Panel-Token": token}, timeout=40)
        if status != 200:
            raise ValueError("%s 轮询失败（HTTP %s）"
                             % (runtime.spec["label"], status))
        data = json.loads(raw.decode("utf-8", "replace"))
        return {"status": data.get("status") or "pending",
                "message": data.get("message") or "",
                "account": data.get("account")}

    def account_login_cancel(self, pid, state):
        runtime = self._provider_or_raise(pid)
        if runtime.spec["kind"] == "zcode":
            return  # zcode 没有取消接口：轮询超时后它的 flow 会自己过期
        token = runtime.panel_session()
        http_json(runtime.port, "POST", "/accounts/login/cancel",
                  payload={"state": state},
                  headers={"X-Panel-Token": token}, timeout=10)

    def account_set(self, pid, uid, enabled=None):
        runtime = self._provider_or_raise(pid)
        uid = str(uid or "").strip()
        if not uid:
            raise ValueError("缺少 uid")
        if runtime.spec["kind"] == "zcode":
            status, raw = http_json(
                runtime.port, "POST", "/admin/api/accounts/%s/enabled"
                % urllib.parse.quote(uid, safe=""),
                payload={"enabled": bool(enabled)},
                headers=runtime.admin_headers(), timeout=20)
            if status != 200:
                raise ValueError("zcode 设置失败（HTTP %s）" % status)
            return
        token = runtime.panel_session()
        status, raw = http_json(runtime.port, "POST", "/accounts/set",
                                payload={"uid": uid, "enabled": bool(enabled)},
                                headers={"X-Panel-Token": token}, timeout=20)
        if status != 200:
            raise ValueError("%s 设置失败（HTTP %s）"
                             % (runtime.spec["label"], status))

    def account_delete(self, pid, uid):
        runtime = self._provider_or_raise(pid)
        uid = str(uid or "").strip()
        if not uid:
            raise ValueError("缺少 uid")
        if runtime.spec["kind"] == "zcode":
            status, raw = http_json(runtime.port, "DELETE", "/admin/api/accounts",
                                    payload=[uid],
                                    headers=runtime.admin_headers(), timeout=20)
            if status != 200:
                raise ValueError("zcode 删除失败（HTTP %s）" % status)
            return
        token = runtime.panel_session()
        status, raw = http_json(runtime.port, "POST", "/accounts/delete",
                                payload={"uid": uid},
                                headers={"X-Panel-Token": token}, timeout=20)
        if status != 200:
            raise ValueError("%s 删除失败（HTTP %s）"
                             % (runtime.spec["label"], status))

    def account_import(self, pid, rows):
        """批量导入凭证。wb/qoder 走 /accounts/import，zcode 走后台 /admin/api/accounts。

        rows 统一是 [{"name": "...", "secret": "..."}]；wb/qoder 的导入接口认的是
        它们自己的账号文档形状，所以这里只支持 zcode 的「粘贴 JWT / API Key」，
        其余上游请用 OAuth 登录。
        """
        runtime = self._provider_or_raise(pid)
        if runtime.spec["kind"] != "zcode":
            raise ValueError("只有 ZCode 支持粘贴凭证导入；%s 请用 OAuth 登录"
                             % runtime.spec["label"])
        secrets = []
        name = ""
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            secret = str(row.get("secret") or row.get("token") or "").strip()
            if not secret:
                continue
            secrets.append(secret)
            if not name and row.get("name"):
                name = str(row["name"]).strip()
        if not secrets:
            raise ValueError("没有可导入的凭证")
        payload = {"provider": "zai", "tokens": secrets}
        if name:
            payload["name"] = name
        status, raw = http_json(runtime.port, "POST", "/admin/api/accounts",
                                payload=payload,
                                headers=runtime.admin_headers(), timeout=60)
        if status != 200:
            raise ValueError("zcode 导入失败（HTTP %s）" % status)
        data = json.loads(raw.decode("utf-8", "replace"))
        return {"added": data.get("count") or 0, "ids": data.get("ids") or []}

    # ---- 当前出口（provider + realm） ----
    def provider_realms(self):
        realms = {}
        for pid in ("wb", "qoder"):
            health = self.providers[pid].health()
            realms[pid] = health.get("realm") or ""
        return realms

    def egress(self):
        routing = hub_settings.routing(self.data_dir)
        return {
            "provider": routing.get("active_provider") or "auto",
            "realm": routing.get("active_realm") or "",
            "default_provider": routing.get("default_provider"),
            "provider_realms": self.provider_realms(),
        }

    def _switch_provider_realm(self, pid, realm):
        """把某个上游的区域切过去（wb/qoder 都支持 POST /realm）。

        wb 要求面板会话（切区域会影响所有没绑区域的 Key），所以带 X-Panel-Token；
        qoder 不强制，带了也无害。
        """
        runtime = self.providers.get(pid)
        if runtime is None or not runtime.health():
            return False
        headers = {}
        if pid in ("wb", "qoder"):
            token = runtime.panel_session()
            if token:
                headers["X-Panel-Token"] = token
        try:
            status, _raw = http_json(runtime.port, "POST", "/realm",
                                     payload={"realm": realm},
                                     headers=headers, timeout=10)
            if status == 200:
                # 面板的「当前出口」读的是各上游 /health 里的 realm；探活有 3 秒缓存，
                # 不主动失效的话刚切完还会显示旧区域（看起来像没生效）。
                runtime._health_at = 0
                runtime.health(force=True)
            return status == 200
        except Exception as exc:
            self.log("[%s] 切换区域失败：%s" % (pid, exc), level="WARN")
            return False

    def set_egress(self, provider, realm=""):
        provider = str(provider or "").strip().lower()
        realm = str(realm or "").strip().lower()
        if provider not in ("auto",) + hub_settings.PROVIDER_IDS:
            raise ValueError("未知出口：%s" % provider)
        if realm and realm not in ("intl", "cn"):
            raise ValueError("未知区域：%s" % realm)
        hub_settings.set_routing(self.data_dir, {"active_provider": provider,
                                                 "active_realm": realm})
        if realm and provider in ("wb", "qoder"):
            self._switch_provider_realm(provider, realm)
        return self.egress()

    def overview(self, host_header=""):
        routing = hub_settings.routing(self.data_dir)
        models = self.models.entries()
        gateway = {
            "base_url": self.base_url(host_header),
            "models": len(models),
            "auth_required": self.auth_required(),
            "keys": len([k for k in hub_settings.api_keys(self.data_dir)
                         if k.get("enabled")]),
        }
        return {
            "ok": True,
            "hub": {
                "version": VERSION,
                "pid": os.getpid(),
                "started_iso": now_iso(STARTED_AT),
                "uptime_s": int(time.time() - STARTED_AT),
                "lan": self.lan,
            },
            "gateway": gateway,
            "providers": [self.providers[pid].snapshot()
                          for pid in hub_settings.PROVIDER_IDS],
            "routing": routing,
            "egress": self.egress(),
            "accounts": self.merged_accounts(),
            # 账号表拿不到时给一句人话（通常是上游面板密码改过）。
            "accounts_hint": "；".join(
                rt._panel_error for rt in self.providers.values()
                if rt._panel_error and not rt._panel_token) or None,
        }

    def base_url(self, host_header=""):
        host = (host_header or "").split(",")[0].strip()
        if not host:
            host = "127.0.0.1:%d" % self.server_port
        return "http://%s/v1" % host

    server_port = DEFAULT_PORT

    def health(self):
        providers = {}
        ok = False
        for pid in hub_settings.PROVIDER_IDS:
            runtime = self.providers[pid]
            health = runtime.health()
            state = runtime.state
            if self.attach_mode and health:
                state = "running"
            providers[pid] = {
                "state": state,
                "ok": bool(health),
                "accounts": health.get("accounts") or 0,
                "accounts_ready": health.get("accounts_ready") or 0,
                "realm": health.get("realm") or "",
            }
            ok = ok or bool(health)
        return {
            "ok": ok,
            "service": "wb-hub",
            "version": VERSION,
            "pid": os.getpid(),
            "started_iso": now_iso(STARTED_AT),
            "gateway": {"models": self.models.count(),
                        "auth_required": self.auth_required()},
            "providers": providers,
        }


# ---------------------------------------------------------------------------
# HTTP 处理器
# ---------------------------------------------------------------------------
class HubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wb-hub/" + VERSION

    # ---- 基础设施 ----
    def log_message(self, fmt, *args):
        # 默认实现会把每一行打到 stderr；中枢的 stdout 是给启动摘要用的，
        # 逐请求日志在这里只会淹没真正重要的行。
        return

    @property
    def hub(self):
        return self.server.hub

    def _json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status, message, err_type="invalid_request_error"):
        self._json(status, {"error": {"message": message, "type": err_type}})

    def _panel_error(self, message):
        self._json(401, {"ok": False, "error": message})

    def _read_body(self):
        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in encoding:
            chunks = []
            total = 0
            while True:
                line = self.rfile.readline(65536).strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(65536)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                total += size
                if total > MAX_PAYLOAD_BYTES:
                    raise PayloadTooLarge()
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            size = int(length)
        except ValueError:
            return b""
        if size <= 0:
            return b""
        if size > MAX_PAYLOAD_BYTES:
            raise PayloadTooLarge()
        return self.rfile.read(size)

    def _json_body(self):
        raw = self._read_body()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    # ---- 鉴权 ----
    def _supplied_key(self):
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        if params.get("key"):
            return (params["key"][0] or "").strip()
        return (self.headers.get("X-API-Key") or "").strip()

    def _panel_token(self):
        return (self.headers.get("X-Panel-Token") or "").strip()

    def _panel_ok(self):
        """面板接口的准入：面板会话，或一把中枢 Key（脚本 / 自动化用）。"""
        if self.hub.panel_ok(self._panel_token()):
            return True
        key = self._supplied_key()
        return bool(key and self.hub.match_key(key))

    def _require_panel(self):
        if self._panel_ok():
            return True
        self._panel_error("面板未登录或会话已过期")
        return False

    def _api_key_ok(self):
        if not self.hub.auth_required():
            return True
        key = self._supplied_key()
        return bool(key and self.hub.match_key(key))

    # ---- 转发 ----
    def _proxy_to(self, pid, path, body=None, method=None, inject_shim=False):
        """把当前请求转给某个上游，响应原样流回客户端。"""
        runtime = self.hub.providers[pid]
        if not runtime.health():
            return self._error(
                502, "%s 上游未就绪（state=%s）"
                % (runtime.spec["label"], runtime.state),
                "upstream_unavailable")
        method = method or self.command
        port = runtime.port
        headers = {}
        for name, value in self.headers.items():
            if name.lower() in HOP_BY_HOP or name.lower() in ("host", "content-length"):
                continue
            headers[name] = value
        credentials = self.hub.provider_credentials(pid)
        if credentials:
            # 先删掉客户端带来的任何大小写形态的 Authorization，再写上游凭证：
            # 两份同名头会让上游按第一份（客户端那把）鉴权，中枢的校验就白做了。
            for name in [k for k in headers if k.lower() == "authorization"]:
                del headers[name]
            headers.update(credentials)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=UPSTREAM_TIMEOUT)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers.items():
                conn.putheader(name, value)
            conn.putheader("Host", "127.0.0.1:%d" % port)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
        except Exception as exc:
            try:
                conn.close()
            except Exception:
                pass
            return self._error(502, "%s 上游连接失败：%s"
                               % (runtime.spec["label"], exc),
                               "upstream_unavailable")

        try:
            ctype = (resp.getheader("Content-Type") or "").lower()
            if inject_shim and "text/html" in ctype:
                self._send_injected(resp, pid, runtime.spec["mount"])
            else:
                self._stream_response(resp, runtime.spec["mount"])
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _stream_response(self, resp, mount=""):
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            lower = name.lower()
            if lower in HOP_BY_HOP or lower == "content-length":
                continue
            if lower == "location":
                value = prefix_url(value, mount)
            self.send_header(name, value)
        declared = resp.getheader("Content-Length")
        # 上游声明了长度就照抄；没声明（SSE / chunked）就转成 chunked 回给
        # 客户端——HTTP/1.1 下「既没有长度也没有 chunked」会让客户端一直等。
        chunked = declared is None and resp.status not in (204, 304)
        if declared is not None:
            self.send_header("Content-Length", declared)
        elif chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if resp.status in (204, 304):
            return
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_injected(self, resp, pid, mount):
        raw = resp.read(MAX_INJECT_BYTES + 1)
        if len(raw) > MAX_INJECT_BYTES:
            # 超过注入上限（正常看板只有 0.3~1MB，走到这里说明上游回了一份
            # 异常大的文档）：不截断内容，退回原样流式透传。已经读出来的那部分
            # 先写出去，剩下的继续从上游读。
            self.send_response(resp.status)
            for name, value in resp.getheaders():
                lower_name = name.lower()
                if lower_name in HOP_BY_HOP or lower_name == "content-length":
                    continue
                self.send_header(name, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for chunk in (raw,):
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        body = rewrite_absolute_attrs(raw, mount)
        shim = (_SHIM_TEMPLATE % {"prefix": json.dumps(mount)}).encode("utf-8")
        lower = body.lower()
        index = lower.rfind(b"</body>")
        if index >= 0:
            body = body[:index] + shim + body[index:]
        else:
            body = body + shim
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            lower_name = name.lower()
            if lower_name in HOP_BY_HOP or lower_name == "content-length":
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- 路由 ----
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            return self._route(method, path, parsed)
        except PayloadTooLarge:
            return self._error(413, "请求体超过 %d 字节上限"
                               % MAX_PAYLOAD_BYTES, "payload_too_large")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self.hub.log("处理 %s %s 失败：\n%s"
                         % (method, path, traceback.format_exc()), level="ERROR")
            try:
                self._error(500, "中枢内部错误，详见日志", "internal_error")
            except Exception:
                pass

    def _route(self, method, path, parsed):
        hub = self.hub
        # ---- 静态 / 健康 ----
        if path in ("/", "/dashboard", "/ui", "/index.html"):
            return self._serve_dashboard()
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/health":
            return self._json(200, hub.health())

        # ---- 面板会话 ----
        if path == "/panel/login" and method == "POST":
            return self._panel_login()
        if path == "/panel/logout" and method == "POST":
            hub.sessions.revoke(self._panel_token())
            return self._json(200, {"ok": True})
        if path == "/logs" and method == "GET":
            # 与 wb_proxy 的 /logs 同形状：console_tail.py 轮询 since_id 增量。
            if not self._panel_ok():
                return self._json(401, {"error": "panel password required"})
            params = urllib.parse.parse_qs(parsed.query)
            try:
                since = int((params.get("since_id") or ["0"])[0])
            except ValueError:
                since = 0
            try:
                limit = min(int((params.get("limit") or ["300"])[0]), 500)
            except ValueError:
                limit = 300
            with hub._log_lock:
                rows = [row for row in hub._log_ring if row["id"] > since][:limit]
            return self._json(200, {"logs": rows})

        if path == "/panel/status" and method == "GET":
            return self._json(200, {
                "ok": True,
                "authenticated": self._panel_ok(),
                "panel_password_default": hub_settings.panel_password_is_default(
                    hub.data_dir),
            })

        # ---- 中枢管理接口 ----
        if path.startswith("/api/hub/"):
            return self._hub_api(method, path, parsed)

        # ---- 合并网关 ----
        if path == "/v1/models" and method == "GET":
            return self._v1_models()
        if path.startswith("/v1/models/") and method == "GET":
            return self._v1_model(path.rsplit("/", 1)[-1])
        if path.startswith("/v1/"):
            return self._v1_forward(method, path)

        # ---- 上游看板挂载 ----
        for pid in hub_settings.PROVIDER_IDS:
            mount = hub_settings.PROVIDER_SPECS[pid]["mount"]
            if path == mount:
                self.send_response(302)
                self.send_header("Location", mount + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith(mount + "/"):
                rest = path[len(mount):]
                if parsed.query:
                    rest += "?" + parsed.query
                body = None
                if method in ("POST", "PUT", "DELETE", "PATCH"):
                    body = self._read_body()
                return self._proxy_to(pid, rest, body=body, method=method,
                                      inject_shim=True)

        return self._error(404, "not found: %s" % path, "not_found")

    # ---- 面板文件 ----
    def _serve_dashboard(self):
        path = os.path.join(ROOT, "hub_dashboard.html")
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            return self._error(500, "hub_dashboard.html 不可用：%s" % exc,
                               "internal_error")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _panel_login(self):
        payload = self._json_body()
        if payload is None:
            return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
        password = str(payload.get("password") or "")
        if not hub_settings.verify_panel_password(self.hub.data_dir, password):
            return self._json(401, {"ok": False, "error": "密码错误"})
        token = self.hub.sessions.create()
        return self._json(200, {"ok": True, "token": token})

    # ---- 中枢 API ----
    def _hub_api(self, method, path, parsed):
        if not self._require_panel():
            return
        hub = self.hub
        if path == "/api/hub/overview" and method == "GET":
            return self._json(200, hub.overview(self.headers.get("Host") or ""))

        if path == "/api/hub/models" and method == "GET":
            params = urllib.parse.parse_qs(parsed.query)
            if (params.get("refresh") or [""])[0] in ("1", "true", "yes"):
                hub.models.refresh(force=True)
            return self._json(200, {
                "ok": True,
                "routing": hub_settings.routing(hub.data_dir),
                "models": hub.models.public(),
            })

        if path == "/api/hub/settings":
            if method == "GET":
                return self._json(200, {
                    "ok": True,
                    "settings": {
                        "routing": hub_settings.routing(hub.data_dir),
                        "providers": hub_settings.providers(hub.data_dir),
                        "auth_required": hub.auth_required(),
                    },
                })
            payload = self._json_body()
            if payload is None:
                return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
            restart_required = False
            if isinstance(payload.get("routing"), dict):
                hub_settings.set_routing(hub.data_dir, payload["routing"])
            if isinstance(payload.get("providers"), dict):
                before = hub_settings.providers(hub.data_dir)
                after = hub_settings.set_providers(hub.data_dir, payload["providers"])
                for pid in hub_settings.PROVIDER_IDS:
                    if (before[pid]["port"] != after[pid]["port"]
                            or before[pid]["enabled"] != after[pid]["enabled"]):
                        restart_required = True
                        runtime = hub.providers[pid]
                        if not after[pid]["enabled"]:
                            runtime.stop()
                            runtime.state = "disabled"
                        else:
                            runtime.port = after[pid]["port"]
                            # 重启要等旧进程退出，最长十几秒；放后台做，别让
                            # 面板这一次保存请求一直挂着（前端会以为保存失败）。
                            threading.Thread(
                                target=runtime.restart,
                                name="hub-restart-%s" % pid, daemon=True).start()
            hub.models.refresh(force=True)
            return self._json(200, {
                "ok": True,
                "settings": {
                    "routing": hub_settings.routing(hub.data_dir),
                    "providers": hub_settings.providers(hub.data_dir),
                    "auth_required": hub.auth_required(),
                },
                "restart_required": restart_required,
            })

        if path == "/api/hub/keys":
            if method == "GET":
                return self._json(200, self._keys_payload())
            payload = self._json_body()
            if payload is None:
                return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
            rows = payload.get("keys")
            if not isinstance(rows, list):
                return self._json(400, {"ok": False, "error": "keys 必须是数组"})
            delete_ids = payload.get("delete_ids")
            if delete_ids is not None and not isinstance(delete_ids, list):
                return self._json(400, {"ok": False,
                                        "error": "delete_ids 必须是数组"})
            hub_settings.set_api_keys(hub.data_dir, rows, delete_ids=delete_ids)
            return self._json(200, self._keys_payload())

        if path == "/api/hub/panel-password" and method == "POST":
            payload = self._json_body()
            if payload is None:
                return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
            password = str(payload.get("password") or "")
            if not password:
                return self._json(400, {"ok": False, "error": "密码不能为空"})
            hub_settings.set_panel_password(hub.data_dir, password)
            hub.sessions.revoke(self._panel_token())
            return self._json(200, {"ok": True})

        if path == "/api/hub/logs" and method == "GET":
            params = urllib.parse.parse_qs(parsed.query)
            pid = (params.get("provider") or ["wb"])[0]
            if pid not in hub.providers:
                return self._json(404, {"ok": False, "error": "未知上游：%s" % pid})
            try:
                tail = int((params.get("tail") or ["200"])[0])
            except ValueError:
                tail = 200
            return self._json(200, {
                "ok": True,
                "provider": pid,
                "lines": hub.providers[pid].tail_log(tail),
            })

        # ---- 统一账号管理 ----
        if path == "/api/hub/accounts/login/start" and method == "POST":
            payload = self._json_body() or {}
            try:
                data = hub.account_login_start(payload.get("provider"),
                                               payload.get("realm") or "")
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                return self._json(502, {"ok": False, "error": str(exc)})
            return self._json(200, dict(data, ok=True))

        if path == "/api/hub/accounts/login/poll" and method == "GET":
            params = urllib.parse.parse_qs(parsed.query)
            try:
                data = hub.account_login_poll((params.get("provider") or [""])[0],
                                              (params.get("state") or [""])[0])
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                return self._json(502, {"ok": False, "error": str(exc)})
            return self._json(200, dict(data, ok=True))

        if path == "/api/hub/accounts/login/cancel" and method == "POST":
            payload = self._json_body() or {}
            try:
                hub.account_login_cancel(payload.get("provider"),
                                         payload.get("state") or "")
            except Exception as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True})

        if path == "/api/hub/accounts/set" and method == "POST":
            payload = self._json_body() or {}
            try:
                hub.account_set(payload.get("provider"), payload.get("uid"),
                                payload.get("enabled"))
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                return self._json(502, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True})

        if path == "/api/hub/accounts/delete" and method == "POST":
            payload = self._json_body() or {}
            try:
                hub.account_delete(payload.get("provider"), payload.get("uid"))
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                return self._json(502, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True})

        if path == "/api/hub/accounts/import" and method == "POST":
            payload = self._json_body() or {}
            try:
                data = hub.account_import(payload.get("provider"),
                                          payload.get("rows") or [])
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                return self._json(502, {"ok": False, "error": str(exc)})
            return self._json(200, dict(data, ok=True))

        if path == "/api/hub/egress":
            if method == "GET":
                return self._json(200, {"ok": True, "egress": hub.egress()})
            payload = self._json_body()
            if payload is None:
                return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
            try:
                data = hub.set_egress(payload.get("provider"),
                                      payload.get("realm") or "")
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True, "egress": data})

        match = re.match(r"^/api/hub/providers/([a-z]+)/(start|stop|restart)$", path)
        if match and method == "POST":
            pid, action = match.group(1), match.group(2)
            runtime = hub.providers.get(pid)
            if runtime is None:
                return self._json(404, {"ok": False, "error": "未知上游：%s" % pid})
            if action == "start":
                runtime.start(reason="panel")
            elif action == "stop":
                runtime.stop()
            else:
                runtime.restart()
            time.sleep(0.2)
            return self._json(200, {"ok": True, "provider": runtime.snapshot()})

        return self._json(404, {"ok": False, "error": "未知接口：%s" % path})

    def _keys_payload(self):
        rows = []
        for entry in hub_settings.api_keys(self.hub.data_dir):
            secret = str(entry.get("key") or "")
            masked = secret
            if len(secret) > 10:
                masked = secret[:7] + "…" + secret[-4:]
            rows.append({
                "id": entry["id"],
                "name": entry.get("name") or "默认",
                "key": secret,
                "masked": masked,
                "enabled": bool(entry.get("enabled")),
                "created_at": entry.get("created_at") or "",
            })
        return {"ok": True, "auth_required": self.hub.auth_required(),
                "keys": rows}

    # ---- 网关数据面 ----
    def _v1_models(self):
        if not self._api_key_ok():
            return self._error(401, "invalid api key - send it as "
                                    "'Authorization: Bearer <key>' or '?key=<key>'")
        data = []
        for entry in self.hub.models.entries():
            data.append({
                "id": entry["id"],
                "object": "model",
                "created": 0,
                "owned_by": entry["owned_by"],
                "provider": entry["provider"],
                # 客户端据此判断该模型要用哪个协议端点：zcode 只有 /v1/messages。
                "protocol": entry.get("protocol") or "openai",
            })
        return self._json(200, {"object": "list", "data": data})

    def _v1_model(self, model_id):
        if not self._api_key_ok():
            return self._error(401, "invalid api key")
        model_id = urllib.parse.unquote(model_id)
        for entry in self.hub.models.entries():
            if entry["id"] == model_id:
                return self._json(200, {
                    "id": entry["id"], "object": "model", "created": 0,
                    "owned_by": entry["owned_by"], "provider": entry["provider"],
                    "protocol": entry.get("protocol") or "openai",
                })
        return self._error(404, "model not found: %s" % model_id, "not_found")

    def _v1_forward(self, method, path):
        if not self._api_key_ok():
            return self._error(401, "invalid api key - send it as "
                                    "'Authorization: Bearer <key>' or '?key=<key>'")
        body = self._read_body()
        payload = None
        if body:
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except Exception:
                payload = None
        model = ""
        if isinstance(payload, dict):
            model = str(payload.get("model") or "")
        pid, route_id = self.hub.models.resolve(model)
        runtime = self.hub.providers[pid]

        # 协议闸门：zcode2api 只实现 Anthropic Messages。把它能服务的请求放过去，
        # 不能服务的**在这里就说清楚**，而不是让它回一个 404/405 让用户猜。
        if (runtime.spec["protocol"] == "anthropic"
                and path not in ("/v1/messages", "/v1/messages/count_tokens")):
            return self._error(
                400,
                "模型 %s 由 %s 提供，而它只支持 Anthropic Messages 协议："
                "请把请求发到 POST /v1/messages（Anthropic SDK / Claude Code 的"
                " base URL 用 http://<host>:<port>，不要带 /v1）。"
                % (model or "(未指定)", runtime.spec["label"]),
                "unsupported_protocol")
        if isinstance(payload, dict) and route_id and route_id != model:
            payload["model"] = route_id
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if not runtime.health():
            return self._error(502, "%s 上游未就绪（state=%s）"
                               % (runtime.spec["label"], runtime.state),
                               "upstream_unavailable")
        return self._proxy_to(pid, path, body=body, method=method)


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address, handler, hub):
        self.hub = hub
        ThreadingHTTPServer.__init__(self, address, handler)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def _probe_existing(port, timeout=1.5):
    try:
        status, raw = http_json(port, "GET", "/health", timeout=timeout)
        if status != 200:
            return None
        payload = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(payload, dict) and payload.get("service") == "wb-hub":
            return payload
    except Exception:
        return None
    return None


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="WorkBuddy × Qoder × ZCode 融合中枢：一个端口托管三个上游并合并 /v1")
    ap.add_argument("--host", default=os.environ.get("HUB_HOST") or "127.0.0.1")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("HUB_PORT") or DEFAULT_PORT))
    ap.add_argument("--lan", action="store_true",
                    help="监听所有网卡（隐含 --host 0.0.0.0，并强制要求 API Key）")
    ap.add_argument("--api-key", default=os.environ.get("HUB_API_KEY") or None,
                    help="中枢 /v1 的客户端密钥（也可在面板里管理）")
    ap.add_argument("--panel-password", default=None,
                    help="启动时设置面板密码（默认 admin）")
    ap.add_argument("--wb-port", type=int, default=None,
                    help="WorkBuddy 上游端口（默认 8788）")
    ap.add_argument("--qoder-port", type=int, default=None,
                    help="Qoder 上游端口（默认 8790）")
    ap.add_argument("--zcode-port", type=int, default=None,
                    help="ZCode 上游端口（默认 8791；需要先装 vendor/zcode2api 的依赖）")
    ap.add_argument("--no-spawn", action="store_true",
                    help="不拉起上游，只当网关（上游需要自己另外启动）")
    ap.add_argument("--no-auto-restart", action="store_true",
                    help="上游崩溃后不自动重启")
    ap.add_argument("--startup-timeout", type=float,
                    default=float(os.environ.get("HUB_STARTUP_TIMEOUT") or 45),
                    help="等待上游 /health 就绪的秒数（默认 45）")
    ap.add_argument("--data-dir", default=None,
                    help="中枢数据目录（默认 data/hub）")
    ap.add_argument("--pid-file", default=None,
                    help="写入进程 PID，供 stop 脚本精确停止")
    ap.add_argument("--probe-only", action="store_true",
                    help="只探测端口上有没有中枢在跑，然后退出"
                         "（0 = 已在运行，3 = 端口空闲；启动脚本用）")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    data_dir = os.path.abspath(args.data_dir
                               or os.environ.get("HUB_DATA_DIR")
                               or os.path.join(ROOT, "data", "hub"))
    os.makedirs(data_dir, exist_ok=True)

    if args.lan and args.host == "127.0.0.1":
        args.host = "0.0.0.0"

    existing = _probe_existing(args.port)
    if args.probe_only:
        # 与 wb_proxy 的约定一致：0 = 已经有一个在跑，3 = 端口空闲可以起。
        if existing:
            print("  [已在运行] %d 端口上已有一个融合中枢（版本 %s）"
                  % (args.port, existing.get("version") or "?"))
            return 0
        return 3
    if existing:
        print()
        print("  [已有一个融合中枢在 %d 端口运行，无需重复启动]" % args.port)
        print("  版本: %s · 启动于: %s" % (existing.get("version") or "?",
                                           existing.get("started_iso") or "?"))
        print("  看板: http://127.0.0.1:%d/" % args.port)
        print()
        return 0

    cli_key = (args.api_key or "").strip()
    if args.lan and not cli_key and not hub_settings.api_keys(data_dir):
        cli_key, generated = hub_settings.ensure_key(data_dir)
        if generated:
            print()
            print("  [局域网模式] 已生成中枢 API Key（保存在 %s）：" % data_dir)
            print("    %s" % cli_key)
            print("  客户端请求请带上：Authorization: Bearer <key>")
            print()

    if args.panel_password:
        hub_settings.set_panel_password(data_dir, args.panel_password)

    # 端口覆盖写回设置（面板/文档都以 settings 为准，命令行是「这一次」的口径）。
    overrides = {}
    if args.wb_port:
        overrides["wb"] = {"port": args.wb_port}
    if args.qoder_port:
        overrides["qoder"] = {"port": args.qoder_port}
    if args.zcode_port:
        overrides["zcode"] = {"port": args.zcode_port}
    if overrides:
        hub_settings.set_providers(data_dir, overrides)

    hub = Hub(data_dir, cli_key=cli_key, lan=args.lan,
              attach=args.no_spawn, auto_restart=not args.no_auto_restart,
              startup_timeout=args.startup_timeout)
    hub.server_port = args.port

    if args.pid_file:
        try:
            with open(args.pid_file, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))

            def _drop_pid(path=args.pid_file):
                try:
                    os.remove(path)
                except OSError:
                    pass
            atexit.register(_drop_pid)
        except OSError as exc:
            hub.log("pid 文件写入失败：%s" % exc, level="WARN")

    hub.log("数据目录 : %s" % data_dir)
    hub.log("面板密码 : %s"
            % ("默认 admin（请尽快修改）"
               if hub_settings.panel_password_is_default(data_dir)
               else "已自定义"))
    if hub.auth_required():
        hub.log("网关鉴权 : on（%d 把 Key）"
                % len([k for k in hub_settings.api_keys(data_dir)
                       if k.get("enabled")]))
    else:
        hub.log("网关鉴权 : off（本机自用；局域网部署请设 --lan 或加 Key）")

    hub.start_all()
    try:
        server = HubServer((args.host, args.port), HubHandler, hub)
    except OSError as exc:
        print()
        print("  [ERROR] 无法监听 %s:%d - %s" % (args.host, args.port, exc))
        print("          端口被占用，请换一个端口：python hub_proxy.py --port %d"
              % (args.port + 1))
        print()
        hub.stop_all()
        return 1

    hub.log("中枢     : http://%s:%d/  (API: /v1)" % (args.host, args.port))
    for pid in hub_settings.PROVIDER_IDS:
        cfg = hub_settings.providers(data_dir)[pid]
        hub.log("上游 %-5s: 本进程内，端口 %s  →  %s/"
                % (pid, cfg["port"] if cfg["port"] else "自动分配",
                   hub_settings.PROVIDER_SPECS[pid]["mount"]))
    hub.log("按 Ctrl+C 停止（会一并停掉三个上游）")

    def _shutdown(signum=None, frame=None):
        hub.stop_all()
        try:
            server.shutdown()
        except Exception:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError, AttributeError):
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop_all()
        try:
            server.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
