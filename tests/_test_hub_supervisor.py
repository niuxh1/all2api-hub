"""hub_proxy 的进程管理 / 用量统计 / 观测口径（不启动真实上游）。

这里钉住的是「配置怎么变成命令行」「用量怎么读」「状态怎么报」三件事——
它们出错时表现都很隐蔽：命令行少一个目录会让 Qoder 读到 WorkBuddy 的账号；
用量算错会让面板的数字对不上上游自己的看板。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hub_proxy as H          # noqa: E402
import hub_settings as S       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + repr(extra) if extra != "" else ""))


def free_port():
    """拿一个当前没人听的端口：套件不能假设 8788/8790 是空的——开发机上
    很可能正跑着真正的网关，那样「没有上游」的断言会读到别人的 /health。"""
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main():
    data_dir = tempfile.mkdtemp(prefix="hub-supervisor-")

    # ---------------- 目录口径 ----------------
    hub = H.Hub(data_dir, attach=True)
    hub.providers["wb"].port = free_port()
    hub.providers["qoder"].port = free_port()
    hub.providers["zcode"].port = free_port()
    wb = hub.providers["wb"]
    qd = hub.providers["qoder"]
    check("WorkBuddy 账号目录是仓库根 accounts/",
          wb.accounts_dir() == os.path.join(ROOT, "accounts"), wb.accounts_dir())
    check("WorkBuddy 用量目录是仓库根 usage/",
          wb.usage_dir() == os.path.join(ROOT, "usage"), wb.usage_dir())
    check("Qoder 账号目录在 data/qoder/accounts",
          qd.accounts_dir() == os.path.join(ROOT, "data", "qoder", "accounts"),
          qd.accounts_dir())
    check("Qoder 用量目录在 data/qoder/usage",
          qd.usage_dir() == os.path.join(ROOT, "data", "qoder", "usage"),
          qd.usage_dir())

    # ---------------- 启动参数（进程内运行：argv 直接喂给上游 main()） ----------------
    spawned = H.Hub(tempfile.mkdtemp(prefix="hub-cmd-"), attach=False)
    wb_argv = spawned.providers["wb"].argv()
    joined = " ".join(wb_argv)
    check("argv 带脚本路径", "wb_proxy.py" in joined, joined)
    check("argv 带 --host 127.0.0.1", "--host 127.0.0.1" in joined, joined)
    check("argv 带账号目录", "--accounts-dir" in joined, joined)
    check("argv 带用量目录", "--usage-dir" in joined, joined)
    check("argv 带内部密钥", "--api-key" in joined
          and spawned.internal_key in joined, joined)
    check("argv 端口来自配置（8788）",
          wb_argv[wb_argv.index("--port") + 1] == "8788", wb_argv)
    check("argv 不带 python 可执行文件（进程内调用）",
          not wb_argv[0].endswith((".exe", "python")), wb_argv[0])
    qd_argv = " ".join(spawned.providers["qoder"].argv())
    check("Qoder argv 带自己的账号目录",
          os.path.join("data", "qoder", "accounts") in qd_argv, qd_argv)
    check("Qoder argv 端口 8790", "--port 8790" in qd_argv, qd_argv)

    # 面板改端口后 argv 跟着变
    S.set_providers(spawned.data_dir, {"wb": {"port": 9201}})
    wb2 = spawned.providers["wb"]
    wb2.port = 9201
    check("改端口后 argv 跟着变",
          "--port 9201" in " ".join(wb2.argv()), wb2.argv())

    # attach 模式不能注入内部密钥（上游的 Key 中枢不知道）
    check("attach 模式不生成内部密钥", hub.internal_key == "", hub.internal_key)
    check("attach 模式不注入 Authorization 头",
          hub.internal_headers() == {}, hub.internal_headers())

    # ---------------- zcode（第三方 FastAPI 应用） ----------------
    zc = spawned.providers["zcode"]
    check("zcode 工作目录是 vendor/zcode2api",
          zc.cwd() == os.path.join(ROOT, "vendor", "zcode2api"), zc.cwd())
    check("zcode 数据目录是 data/zcode",
          zc.accounts_dir() == os.path.join(ROOT, "data", "zcode"),
          zc.accounts_dir())
    check("zcode 走 uvicorn（不是 wb/qoder 的 argv 形状）",
          zc.spec["kind"] == "zcode", zc.spec)
    check("zcode 依赖探测返回列表", isinstance(zc.missing_deps(), list),
          zc.missing_deps())
    check("wb 的依赖探测恒为空（纯标准库）", spawned.providers["wb"].missing_deps() == [])
    check("zcode 后台鉴权头是 Bearer",
          zc.admin_headers().get("Authorization") == "Bearer zcode",
          zc.admin_headers())

    # ---------------- attach 模式的 start ----------------
    check("attach 模式 start 返回 False", wb.start() is False)
    check("attach 模式状态是 external", wb.state == "external", wb.state)

    # ---------------- 用量统计 ----------------
    usage_dir = tempfile.mkdtemp(prefix="hub-usage-")
    log_path = os.path.join(usage_dir, "usage.jsonl")
    now = time.time()
    yesterday = now - 26 * 3600
    rows = [
        {"at": now, "model": "m1", "total_tokens": 100},
        {"at": now, "model": "m2", "prompt_tokens": 30, "completion_tokens": 20},
        {"at": yesterday, "model": "m3", "total_tokens": 9999},
        {"at": "not-a-time", "total_tokens": 5},
    ]
    with open(log_path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
        fh.write("{ broken json\n")
        fh.write(json.dumps({"at": now, "total_tokens": 7}) + "\n")
    stats = H.UsageStats(log_path)
    today = stats.today()
    check("今日请求数只算今天", today["requests"] == 3, today)
    check("今日 token 用 total_tokens 或 prompt+completion 兜底",
          today["tokens"] == 100 + 50 + 7, today)

    # 增量：追加后只读新行
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": time.time(), "total_tokens": 11}) + "\n")
    again = stats.today()
    check("追加的行被增量读到", again["requests"] == 4 and again["tokens"] == 168,
          again)

    # 轮转/清空：从头重来
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": time.time(), "total_tokens": 3}) + "\n")
    rotated = stats.today()
    check("日志被清空后重新计数", rotated == {"requests": 1, "tokens": 3}, rotated)

    missing = H.UsageStats(os.path.join(usage_dir, "nope.jsonl"))
    check("文件不存在时返回零", missing.today() == {"requests": 0, "tokens": 0},
          missing.today())

    # ---------------- 日志尾读（进程内运行：读环形缓冲，不再有每上游日志文件） ----------------
    for i in range(30):
        wb._logbuf.append("row-%02d" % i)
    tail = wb.tail_log(5)
    check("尾读最后 5 行", tail == ["row-%02d" % i for i in range(25, 30)], tail)
    check("尾读行数上限被夹住", len(wb.tail_log(999999)) == 30, len(wb.tail_log(999999)))
    check("没有日志时返回空列表", qd.tail_log(5) == [], qd.tail_log(5))

    # ---------------- 状态快照 ----------------
    snap = wb.snapshot()
    for key in ("id", "label", "state", "port", "mount", "accounts", "models",
                "today_requests", "today_tokens", "last_error"):
        check("快照字段 %s" % key, key in snap, sorted(snap))
    check("未启动时 state 不是 running", snap["state"] != "running", snap["state"])
    check("挂载路径带尾斜杠", snap["mount"] == "/wb/", snap["mount"])

    health = hub.health()
    check("没有上游时 health.ok 为 False", health["ok"] is False, health)
    check("health 仍列出全部上游",
          set(health["providers"]) == {"wb", "qoder", "zcode"},
          health["providers"])
    check("health 的模型数是缓存值（不触发刷新）",
          health["gateway"]["models"] == 0, health["gateway"])

    # ---------------- 网关地址 ----------------
    hub.server_port = 8787
    check("base_url 用 Host 头",
          hub.base_url("192.168.1.9:8787") == "http://192.168.1.9:8787/v1",
          hub.base_url("192.168.1.9:8787"))
    check("没有 Host 时回落到监听端口",
          hub.base_url("") == "http://127.0.0.1:8787/v1", hub.base_url(""))

    # ---------------- 模型解析的兜底 ----------------
    index = hub.models
    S.set_routing(hub.data_dir, {"default_provider": "qoder"})
    check("空模型名走默认上游", index.resolve("") == ("qoder", ""), index.resolve(""))
    check("未知模型走默认上游",
          index.resolve("no-such") == ("qoder", "no-such"), index.resolve("no-such"))
    check("前缀强制优先于默认上游",
          index.resolve("wb/whatever") == ("wb", "whatever"),
          index.resolve("wb/whatever"))

    # ---------------- 单实例探测 ----------------
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    unused_port = sock.getsockname()[1]
    sock.close()
    check("空端口探测返回 None",
          H._probe_existing(unused_port, timeout=0.5) is None)

    print("")
    print("SUMMARY: %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
    print("RESULT: %s" % ("RED" if FAIL else "GREEN"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
