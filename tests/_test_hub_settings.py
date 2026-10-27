"""hub_settings 的单元测试：面板密码、网关 Key、路由、上游开关、会话。

这些是「配置层」的不变量，跑起来不碰网络也不起服务：
- 默认值必须在没有 settings.json 时成立（新装直接能用）；
- 损坏的 settings.json 只能退化成默认值，不能把中枢拖死；
- Key 的软删除/替换/增量三种语义必须分清（面板与脚本走的是不同语义）；
- 路由覆盖要能加、能清，非法值一律回退默认。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hub_settings as S  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + repr(extra) if extra != "" else ""))


def fresh():
    return tempfile.mkdtemp(prefix="hub-settings-")


def main():
    # ---------------- 默认值 ----------------
    d = fresh()
    check("没有 settings.json 时 load 返回空字典", S.load(d) == {}, S.load(d))
    check("默认面板密码是 admin", S.panel_password_is_default(d))
    check("默认能验证 admin", S.verify_panel_password(d, "admin"))
    check("默认拒绝别的密码", not S.verify_panel_password(d, "nope"))
    check("没有 Key 时不强制鉴权", not S.auth_required(d))
    check("默认上游是 wb", S.routing(d)["default_provider"] == "wb", S.routing(d))
    providers = S.providers(d)
    check("默认三个上游都启用",
          set(providers) == {"wb", "qoder", "zcode"}
          and all(p["enabled"] for p in providers.values()),
          providers)
    check("默认端口 8788 / 8790 / 8791",
          (providers["wb"]["port"], providers["qoder"]["port"],
           providers["zcode"]["port"]) == (8788, 8790, 8791), providers)
    check("zcode 标记为 anthropic 协议",
          providers["zcode"]["protocol"] == "anthropic"
          and providers["zcode"]["kind"] == "zcode", providers["zcode"])
    check("zcode 后台密钥默认 zcode",
          S.zcode_admin_key(d) == "zcode", S.zcode_admin_key(d))
    S.set_option(d, "zcode", "admin_key", "changed")
    check("zcode 后台密钥可改", S.zcode_admin_key(d) == "changed",
          S.zcode_admin_key(d))

    # ---------------- 存取 ----------------
    S.save(d, {"hello": "world"})
    check("save/load 往返", S.load(d)["hello"] == "world", S.load(d))
    with open(S.settings_path(d), "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    check("损坏的 settings.json 退化成默认值（不抛异常）", S.load(d) == {}, S.load(d))
    check("损坏时面板密码仍按默认 admin 处理",
          S.verify_panel_password(d, "admin"))
    merged = S.deep_merge({"a": {"x": 1, "y": 2}, "b": 1}, {"a": {"y": 9, "z": 3}})
    check("deep_merge 递归且保留兄弟键",
          merged == {"a": {"x": 1, "y": 9, "z": 3}, "b": 1}, merged)

    # ---------------- 面板密码 ----------------
    d = fresh()
    S.set_panel_password(d, "s3cret")
    check("自定义密码生效", S.verify_panel_password(d, "s3cret"))
    check("自定义后旧默认密码失效", not S.verify_panel_password(d, "admin"))
    check("自定义后不再是默认密码", not S.panel_password_is_default(d))
    S.set_panel_password(d, "admin")
    check("改回 admin 后重新标记为默认", S.panel_password_is_default(d))
    try:
        S.set_panel_password(d, "")
        check("空密码被拒绝", False)
    except ValueError:
        check("空密码被拒绝", True)

    # ---------------- 网关 Key ----------------
    d = fresh()
    rows = S.set_api_keys(d, [
        {"id": "k1", "name": "甲", "key": "sk-aaa", "enabled": True},
        {"id": "k2", "name": "乙", "key": "sk-bbb", "enabled": False},
    ])
    check("保存两把 Key", len(rows) == 2, rows)
    check("保存后必须鉴权", S.auth_required(d))
    check("启用的 Key 能匹配",
          (S.match_api_key(d, "sk-aaa") or {}).get("id") == "k1")
    check("停用的 Key 不匹配", S.match_api_key(d, "sk-bbb") is None)
    check("未知 Key 不匹配", S.match_api_key(d, "sk-zzz") is None)
    check("空 Key 不匹配", S.match_api_key(d, "") is None)
    check("命令行 Key 作为额外来源",
          (S.match_api_key(d, "sk-cli", extra_keys=("sk-cli",)) or {}).get("id")
          == "cli")
    check("created_at 自动补", bool(rows[0]["created_at"]), rows[0])

    # 空密钥 = 沿用旧值（面板不回显）
    rows = S.set_api_keys(d, [{"id": "k1", "name": "甲改名", "key": "",
                               "enabled": True}], delete_ids=["k1"])
    check("空密钥沿用旧密钥", rows[0]["key"] == "sk-aaa", rows)
    check("空密钥可以改名", rows[0]["name"] == "甲改名", rows)

    # 增量语义：没提到的 Key 不能被退休
    S.set_api_keys(d, [{"id": "k1", "name": "甲", "key": "sk-aaa", "enabled": True},
                       {"id": "k3", "name": "丙", "key": "sk-ccc", "enabled": True}])
    rows = S.set_api_keys(d, [{"id": "k1", "name": "甲", "key": "", "enabled": True}],
                          delete_ids=[])
    check("增量语义：未提及的 k3 还在",
          {r["id"] for r in rows} == {"k1", "k3"}, rows)
    check("增量语义：k3 的密钥没被动", S.match_api_key(d, "sk-ccc") is not None)

    # 替换语义：提交里没有的都要退休
    rows = S.set_api_keys(d, [{"id": "k1", "name": "甲", "key": "", "enabled": True}])
    check("替换语义：未提交的 k3 被退休",
          {r["id"] for r in rows} == {"k1"}, rows)
    check("退休后旧密钥不能再登录", S.match_api_key(d, "sk-ccc") is None)
    retired = S.api_keys(d, include_deleted=True)
    check("退休行仍在磁盘上（历史用量可读名字）",
          any(r["id"] == "k3" and r.get("deleted_at") for r in retired), retired)
    check("退休行的密钥已清空",
          all(not r["key"] for r in retired if r.get("deleted_at")), retired)

    # 同一把密钥重复提交时沿用旧 id（历史不被拆成两段）
    d = fresh()
    S.set_api_keys(d, [{"id": "k9", "name": "老", "key": "sk-same", "enabled": True}])
    rows = S.set_api_keys(d, [{"id": "", "name": "新", "key": "sk-same",
                               "enabled": True}])
    check("同密钥重复提交沿用旧 id", rows[0]["id"] == "k9", rows)

    # 停用全部 Key 后不再强制鉴权
    S.set_api_keys(d, [{"id": "k9", "name": "老", "key": "", "enabled": False}])
    check("全部停用后不强制鉴权", not S.auth_required(d))

    # ensure_key：局域网首次启动
    d = fresh()
    key, generated = S.ensure_key(d)
    check("ensure_key 生成新 Key", generated and key.startswith("sk-hub-"), key)
    check("生成的 Key 立即能匹配", S.match_api_key(d, key) is not None)
    again, generated2 = S.ensure_key(d)
    check("ensure_key 幂等（第二次不再生成）",
          again == key and generated2 is False, (again, generated2))

    # ---------------- 路由 ----------------
    d = fresh()
    check("非法默认上游回退 wb",
          S.set_routing(d, {"default_provider": "nope"})["default_provider"] == "wb")
    routing = S.set_routing(d, {"overrides": {"m1": "qoder", "m2": "bad"}})
    check("合法覆盖写入、非法覆盖丢弃",
          routing["overrides"] == {"m1": "qoder"}, routing)
    routing = S.set_routing(d, {"overrides": {"m1": ""}})
    check("空 provider 清除覆盖", routing["overrides"] == {}, routing)
    routing = S.set_routing(d, {"default_provider": "qoder"})
    check("默认上游可改", routing["default_provider"] == "qoder", routing)

    # 当前出口（硬切换）：auto / 具体上游 + 区域
    check("出口默认 auto", S.routing(d)["active_provider"] == "auto", S.routing(d))
    check("区域默认空", S.routing(d)["active_realm"] == "", S.routing(d))
    routing = S.set_routing(d, {"active_provider": "zcode"})
    check("出口可设为具体上游", routing["active_provider"] == "zcode", routing)
    routing = S.set_routing(d, {"active_provider": "nope"})
    check("非法出口被忽略", routing["active_provider"] == "zcode", routing)
    routing = S.set_routing(d, {"active_realm": "cn"})
    check("区域可设", routing["active_realm"] == "cn", routing)
    routing = S.set_routing(d, {"active_realm": "mars"})
    check("非法区域被忽略", routing["active_realm"] == "cn", routing)
    routing = S.set_routing(d, {"active_provider": "auto", "active_realm": ""})
    check("可切回 auto", routing["active_provider"] == "auto"
          and routing["active_realm"] == "", routing)

    # ---------------- 上游开关 ----------------
    d = fresh()
    providers = S.set_providers(d, {"wb": {"port": 9100, "enabled": False}})
    check("上游端口可改", providers["wb"]["port"] == 9100, providers)
    check("上游可停用", providers["wb"]["enabled"] is False, providers)
    check("没提到的上游保持默认", providers["qoder"]["port"] == 8790, providers)
    providers = S.set_providers(d, {"wb": {"port": "not-a-port"}})
    check("非法端口被忽略（保留旧值）", providers["wb"]["port"] == 9100, providers)
    providers = S.set_providers(d, {"unknown": {"port": 1}})
    check("未知上游被忽略", set(providers) == {"wb", "qoder", "zcode"}, providers)

    # ---------------- 会话 ----------------
    sessions = S.PanelSessions(ttl=60)
    token = sessions.create()
    check("新会话有效", sessions.valid(token))
    check("空 token 无效", not sessions.valid(""))
    sessions.revoke(token)
    check("撤销后无效", not sessions.valid(token))
    short = S.PanelSessions(ttl=-1)
    expired = short.create()
    check("过期会话无效", not short.valid(expired))

    print("")
    print("SUMMARY: %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
    print("RESULT: %s" % ("RED" if FAIL else "GREEN"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
