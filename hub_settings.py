#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中枢（hub）自己的设置：客户端 Key、面板密码、上游开关、模型路由。

融合仓里有三个上游（WorkBuddy / Qoder / ZCode），它们各自维护自己的
`<data>/accounts/settings.json`——账号、护栏、面板密码都在那边。中枢是**新增的
一层**，它需要自己的一组东西：

    data/hub/settings.json
      panel_password_*   面板登录密码（PBKDF2，与两个上游同一套口径）
      api_keys           中枢 /v1 的客户端 Key（上游那套 Key 不共用：两边额度口径不同）
      providers          上游启停与端口
      routing            默认上游 + 逐模型覆盖

为什么不去复用 `wb_settings`：那个模块的读写函数都以「账号目录」为参数、并且和
WorkBuddy 的字段名（realm/models/...）绑在一起。中枢的 Key 没有区域绑定（区域
由被选中的上游决定），硬塞进去会让两边互相污染。这里只抄它验证过的那几个**语义**
（PBKDF2 哈希、软删除、按 mtime 缓存），不抄它的数据结构。

仅使用 Python 标准库。
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

DEFAULT_PANEL_PASSWORD = "admin"
PBKDF2_ROUNDS = 120_000
SESSION_TTL = 12 * 3600          # 面板会话有效期（秒）

# 上游清单是代码常量：它决定「哪个脚本、哪个默认端口、哪个挂载前缀、走什么协议」，
# 属于程序结构而不是用户配置。settings.json 里只能覆盖 enabled / port。
#
# kind 决定「怎么拉起、怎么探活、怎么读账号」：
#   proxy  —— 本仓自带的 stdlib 网关（wb_proxy / qoder_proxy），/health + /accounts；
#   zcode  —— vendor/ 下的第三方 FastAPI 应用（zcode2api），/meta + /admin/api/accounts，
#             只有 Anthropic Messages 协议。
PROVIDER_SPECS = {
    "wb": {
        "label": "WorkBuddy",
        "kind": "proxy",
        "protocol": "openai",
        "script": "wb_proxy.py",
        "port": 8788,
        "mount": "/wb",
        "data_dir": None,          # None = 用仓库根下的 accounts/ · usage/
        "health_path": "/health",
        "models_path": "/v1/models",
    },
    "qoder": {
        "label": "Qoder",
        "kind": "proxy",
        "protocol": "openai",
        "script": "qoder_proxy.py",
        "port": 8790,
        "mount": "/qoder",
        "data_dir": os.path.join("data", "qoder"),
        "health_path": "/health",
        "models_path": "/v1/models",
    },
    "zcode": {
        "label": "ZCode",
        "kind": "zcode",
        "protocol": "anthropic",
        # 第三方应用：main.py serve --port N，工作目录必须是 vendor/zcode2api
        # （它按包名 import app.*，也和自己的 captcha_node/ 相对定位）。
        "script": os.path.join("vendor", "zcode2api", "main.py"),
        "cwd": os.path.join("vendor", "zcode2api"),
        "port": 8791,
        "mount": "/zcode",
        "data_dir": os.path.join("data", "zcode"),
        "health_path": "/meta",
        "models_path": "/v1/models",
        "requirements": os.path.join("vendor", "zcode2api", "requirements.txt"),
        "admin_key_default": "zcode",
    },
}
PROVIDER_IDS = tuple(PROVIDER_SPECS)
DEFAULT_PROVIDER = "wb"

_lock = threading.RLock()

# settings.json 的解析结果按 (path, mtime, size) 缓存：概览接口每次刷新要读好几次
# （Key、路由、上游开关各一次），而写入一律走 save() 的 os.replace，mtime/size
# 必变，所以缓存不会读到旧值。返回的是顶层浅拷贝，调用方照旧可以随便改。
_load_cache = {"key": None, "value": {}}


# ---------------------------------------------------------------------------
# 存取
# ---------------------------------------------------------------------------
def settings_path(data_dir):
    return os.path.join(data_dir, "settings.json")


def load(data_dir):
    """读中枢设置；文件不存在 / 解析失败都退化成空字典（fail-open）。"""
    path = settings_path(data_dir)
    try:
        info = os.stat(path)
        key = (path, info.st_mtime, info.st_size)
    except OSError:
        key = (path, None, None)
    with _lock:
        if _load_cache["key"] == key:
            return dict(_load_cache["value"])
    data = {}
    try:
        with open(path, encoding="utf-8") as fh:
            parsed = json.load(fh)
        if isinstance(parsed, dict):
            data = parsed
    except FileNotFoundError:
        pass
    except Exception:
        # 损坏的 settings.json 不该让整个中枢起不来：面板会显示默认值，用户
        # 改一次就覆盖回去了。故意不打印内容（里面可能有 Key）。
        data = {}
    with _lock:
        _load_cache.update({"key": key, "value": data})
    return dict(data)


def save(data_dir, data):
    """原子写：先写 .tmp 再 os.replace，崩在中间也不会留下半个 JSON。"""
    with _lock:
        os.makedirs(data_dir, exist_ok=True)
        path = settings_path(data_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        _load_cache["key"] = None      # 主动失效，不依赖文件系统 mtime 精度
        return path


def deep_merge(base, patch):
    """递归合并：面板只提交它管的键，手写的/未来新增的兄弟键要活下来。"""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# 面板密码（口径与 wb_settings / qoder_settings 一致：PBKDF2-HMAC-SHA256）
# ---------------------------------------------------------------------------
def _digest(password, salt_hex, rounds=PBKDF2_ROUNDS):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), rounds
    ).hex()


def panel_password_is_default(data_dir):
    data = load(data_dir)
    if not data.get("panel_password_hash"):
        return True
    return data.get("panel_password_default") is True


def verify_panel_password(data_dir, password):
    """True 表示这串密码能开面板。"""
    password = password or ""
    data = load(data_dir)
    stored = data.get("panel_password_hash")
    if not stored:
        return password == DEFAULT_PANEL_PASSWORD
    if data.get("panel_password_default") is True:
        return password == DEFAULT_PANEL_PASSWORD
    salt = data.get("panel_password_salt")
    if not salt:
        return False
    rounds = int(data.get("panel_password_rounds") or PBKDF2_ROUNDS)
    try:
        given = _digest(password, salt, rounds)
    except Exception:
        return False
    return hmac.compare_digest(given, stored)


def set_panel_password(data_dir, password):
    """写面板密码。设成默认值 admin 时只留一个「还是默认」标记，不存哈希。"""
    password = password or ""
    if len(password) < 1:
        raise ValueError("password must not be empty")
    with _lock:
        data = load(data_dir)
        if password == DEFAULT_PANEL_PASSWORD:
            data.pop("panel_password_salt", None)
            data.pop("panel_password_rounds", None)
            data["panel_password_hash"] = ""
            data["panel_password_default"] = True
        else:
            salt = secrets.token_hex(16)
            data["panel_password_salt"] = salt
            data["panel_password_rounds"] = PBKDF2_ROUNDS
            data["panel_password_hash"] = _digest(password, salt)
            data["panel_password_default"] = False
        save(data_dir, data)


# ---------------------------------------------------------------------------
# 网关 Key
# ---------------------------------------------------------------------------
def _clean_key_entry(raw):
    """把面板提交的一行洗成存储形状；认不出来的行返回 None。"""
    if not isinstance(raw, dict):
        return None
    key = str(raw.get("key") or "").strip()
    name = str(raw.get("name") or "").strip()
    entry_id = str(raw.get("id") or "").strip()
    enabled = raw.get("enabled", True)
    if not key and not entry_id:
        # 既没有密钥也没有 id：这行不可能对应任何东西，直接丢掉。
        return None
    entry = {
        "id": entry_id or ("k-" + secrets.token_hex(4)),
        "name": name or "默认",
        "key": key,
        "enabled": bool(enabled) and not raw.get("deleted_at"),
        "created_at": str(raw.get("created_at") or "").strip(),
    }
    # 软删除标记要原样带回来：api_keys(include_deleted=True) 靠它把退休行和
    # 现役行分开，丢了它就会把「已删除」当成「一把空密钥的现役 Key」。
    if raw.get("deleted_at"):
        entry["deleted_at"] = str(raw["deleted_at"])
    return entry


def api_keys(data_dir, include_deleted=False):
    """全部 Key（按存储顺序）。软删除的行默认不返回。"""
    data = load(data_dir)
    stored = data.get("api_keys")
    if not isinstance(stored, list):
        return []
    out = []
    seen_ids = set()
    seen_secrets = set()
    for raw in stored:
        entry = _clean_key_entry(raw)
        if entry is None:
            continue
        # id 去重：手改出来的重复 id 会让「按 id 删除」误删两行。
        base_id = entry["id"]
        suffix = 2
        while entry["id"] in seen_ids:
            entry["id"] = "%s-%d" % (base_id, suffix)
            suffix += 1
        # 密钥去重：同一个密钥出现两次时，第二行永远匹配不到（match 只认第一行）。
        if entry["key"]:
            if entry["key"] in seen_secrets:
                continue
            seen_secrets.add(entry["key"])
        seen_ids.add(entry["id"])
        out.append(entry)
    if not include_deleted:
        out = [e for e in out if not e.get("deleted_at")]
    return out


def _retired_key_row(old):
    """软删除行：名字与 id 留着（历史用量按 id 找名字），密钥清空。"""
    return {
        "id": old["id"],
        "name": old.get("name") or "默认",
        "key": "",
        "enabled": False,
        "created_at": old.get("created_at") or "",
        "deleted_at": time.strftime("%Y/%m/%d %H:%M"),
    }


def set_api_keys(data_dir, keys, delete_ids=None):
    """保存 Key 列表。

    - `delete_ids is None`：替换语义，提交里没有的现存 Key 全部退休；
    - `delete_ids` 是列表：只退休点名的 id，没提到的保持原样（面板用这个，
      因为面板内存里的行可能不是最新的一份）。
    退休是软删除：密钥清空、名字与 id 留着，历史用量仍然能显示「这是谁花的」。
    """
    with _lock:
        previous = api_keys(data_dir, include_deleted=True)
        live = [e for e in previous if not e.get("deleted_at")]
        retired = [e for e in previous if e.get("deleted_at")]
        upsert = delete_ids is not None
        drop = {str(i) for i in (delete_ids or [])}
        by_id = {e["id"]: e for e in live}
        # 同一把密钥再次提交（面板丢了 id 的情况）时沿用旧 id，否则这把 Key 的
        # 历史会被拆成两段。
        by_secret = {}
        for old in live:
            if old["key"]:
                by_secret.setdefault(old["key"], old)

        saved = []
        used_ids = set()
        for raw in keys or []:
            entry = _clean_key_entry(raw)
            if entry is None:
                continue
            if not entry["key"]:
                # 面板不回显密钥：空密钥 = 沿用旧值（按 id，其次按名字）。
                old = by_id.get(entry["id"])
                if old is None:
                    for candidate in live:
                        if candidate["name"] == entry["name"]:
                            old = candidate
                            break
                if old is None or not old.get("key"):
                    continue
                entry["key"] = old["key"]
                entry["id"] = old["id"]
                entry["created_at"] = entry["created_at"] or old.get("created_at") or ""
            else:
                twin = by_secret.get(entry["key"])
                if twin is not None:
                    entry["id"] = twin["id"]
                    entry["created_at"] = (entry["created_at"]
                                           or twin.get("created_at") or "")
            if entry["id"] in used_ids:
                continue
            used_ids.add(entry["id"])
            if not entry["created_at"]:
                entry["created_at"] = time.strftime("%Y/%m/%d %H:%M")
            saved.append(entry)

        if upsert:
            # 只退休点名的 id；提交里漏掉的现役 Key 保持原样。
            newly_retired = [e for e in live
                             if e["id"] in drop and e["id"] not in used_ids]
        else:
            newly_retired = [e for e in live if e["id"] not in used_ids]

        rows = list(saved)
        # 增量语义下「没提交、也没点名删除」的现役 Key 必须原样保留——面板内存里
        # 的行可能只覆盖它正在编辑的那一条，漏掉这一步会把别人的 Key 悄悄弄丢。
        retired_ids = {e["id"] for e in newly_retired}
        for old in live:
            if old["id"] in used_ids or old["id"] in retired_ids:
                continue
            rows.append(old)
        rows.extend(_retired_key_row(old) for old in newly_retired)
        rows.extend(retired)            # 历史退休行原样保留，排在末尾

        data = load(data_dir)
        data["api_keys"] = rows
        save(data_dir, data)
        return api_keys(data_dir)


def match_api_key(data_dir, supplied, extra_keys=()):
    """返回这次请求用的 Key 条目，或 None。`extra_keys` 给 --api-key 用。"""
    supplied = (supplied or "").strip()
    if not supplied:
        return None
    for entry in api_keys(data_dir):
        if not entry.get("enabled"):
            continue
        if hmac.compare_digest(str(entry.get("key") or ""), supplied):
            return entry
    for extra in extra_keys or ():
        extra = str(extra or "").strip()
        if extra and hmac.compare_digest(extra, supplied):
            return {"id": "cli", "name": "命令行 --api-key", "key": extra,
                    "enabled": True, "created_at": ""}
    return None


def auth_disabled(data_dir):
    return load(data_dir).get("auth_disabled") is True


def auth_required(data_dir, cli_key=""):
    """中枢 /v1 是否必须带 Key。"""
    if auth_disabled(data_dir):
        return False
    if any(entry.get("enabled") for entry in api_keys(data_dir)):
        return True
    return bool(cli_key)


def ensure_key(data_dir, name="局域网默认"):
    """没有可用 Key 时生成一把并落盘，返回明文（局域网模式首次启动用）。"""
    existing = [e for e in api_keys(data_dir) if e.get("enabled") and e.get("key")]
    if existing:
        return existing[0]["key"], False
    key = "sk-hub-" + secrets.token_urlsafe(24)
    set_api_keys(data_dir, [{"id": "k-" + secrets.token_hex(4),
                             "name": name, "key": key, "enabled": True}])
    return key, True


# ---------------------------------------------------------------------------
# 上游开关 / 端口 / 路由
# ---------------------------------------------------------------------------
def option(data_dir, pid, name, default=None):
    """读某个上游的附加选项（目前只有 zcode 的后台密钥）。

    单独留一个口子而不是给 providers() 加一堆特例字段：这些选项只有对应
    kind 的实现才认识，混进通用结构里会让「未知键要不要保留」变得难说清。
    """
    stored = load(data_dir).get("providers")
    if not isinstance(stored, dict):
        return default
    raw = stored.get(pid)
    if not isinstance(raw, dict):
        return default
    value = raw.get(name, default)
    return default if value is None else value


def set_option(data_dir, pid, name, value):
    with _lock:
        data = load(data_dir)
        stored = data.get("providers")
        stored = stored if isinstance(stored, dict) else {}
        item = stored.get(pid)
        item = item if isinstance(item, dict) else {}
        item[name] = value
        stored[pid] = item
        data["providers"] = stored
        save(data_dir, data)
    return value


def zcode_admin_key(data_dir):
    """zcode2api 的后台密钥：中枢用它读账号池（面板里的 ZCode 账号数）。"""
    default = PROVIDER_SPECS["zcode"]["admin_key_default"]
    return str(option(data_dir, "zcode", "admin_key", default) or default)


def providers(data_dir):
    """上游配置：默认值 + settings.json 的覆盖，永远返回全部上游的完整条目。"""
    stored = load(data_dir).get("providers")
    stored = stored if isinstance(stored, dict) else {}
    out = {}
    for pid, spec in PROVIDER_SPECS.items():
        raw = stored.get(pid) if isinstance(stored.get(pid), dict) else {}
        port = raw.get("port", spec["port"])
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = spec["port"]
        out[pid] = {
            "id": pid,
            "label": spec["label"],
            "kind": spec["kind"],
            "protocol": spec["protocol"],
            "script": spec["script"],
            "mount": spec["mount"],
            "enabled": bool(raw.get("enabled", True)),
            "port": port,
        }
    return out


def set_providers(data_dir, patch):
    """局部更新上游开关/端口；返回更新后的完整清单。"""
    if not isinstance(patch, dict):
        return providers(data_dir)
    clean = {}
    for pid, raw in patch.items():
        if pid not in PROVIDER_SPECS or not isinstance(raw, dict):
            continue
        item = {}
        if "enabled" in raw:
            item["enabled"] = bool(raw["enabled"])
        if "port" in raw:
            try:
                item["port"] = int(raw["port"])
            except (TypeError, ValueError):
                pass
        clean[pid] = item
    with _lock:
        data = load(data_dir)
        data["providers"] = deep_merge(data.get("providers") or {}, clean)
        save(data_dir, data)
    return providers(data_dir)


REALMS = ("intl", "cn")
# 上游推理强度：auto = 跟随客户端；其余值会覆盖客户端带来的 reasoning_effort。
EFFORTS = ("none", "minimal", "low", "medium", "high", "max")


def routing(data_dir):
    stored = load(data_dir).get("routing")
    stored = stored if isinstance(stored, dict) else {}
    default = str(stored.get("default_provider") or DEFAULT_PROVIDER)
    if default not in PROVIDER_SPECS:
        default = DEFAULT_PROVIDER
    # 当前出口：auto = 按模型清单自动派发；具体上游 = **硬切换**，全部流量走它。
    # 这是「今天想烧哪一家的额度」的开关，和 default_provider（只在多义时才用）
    # 是两件事——共用一个字段会让「默认上游」这个说法名不副实。
    active = str(stored.get("active_provider") or "auto").strip().lower()
    if active != "auto" and active not in PROVIDER_SPECS:
        active = "auto"
    realm = str(stored.get("active_realm") or "").strip().lower()
    if realm not in REALMS:
        realm = ""
    effort = str(stored.get("upstream_effort") or "auto").strip().lower()
    if effort not in EFFORTS:
        effort = "auto"
    overrides = {}
    raw_overrides = stored.get("overrides")
    if isinstance(raw_overrides, dict):
        for model, provider in raw_overrides.items():
            provider = str(provider or "").strip()
            model = str(model or "").strip()
            if model and provider in PROVIDER_SPECS:
                overrides[model] = provider
    return {"default_provider": default, "active_provider": active,
            "active_realm": realm, "upstream_effort": effort,
            "overrides": overrides}


def set_routing(data_dir, patch):
    """局部更新路由：default_provider 与 overrides（overrides 按模型名合并）。"""
    if not isinstance(patch, dict):
        return routing(data_dir)
    current = routing(data_dir)
    if "default_provider" in patch:
        default = str(patch.get("default_provider") or "").strip()
        if default in PROVIDER_SPECS:
            current["default_provider"] = default
    if "active_provider" in patch:
        active = str(patch.get("active_provider") or "").strip().lower()
        if active == "auto" or active in PROVIDER_SPECS:
            current["active_provider"] = active
    if "active_realm" in patch:
        realm = str(patch.get("active_realm") or "").strip().lower()
        if realm in REALMS or realm == "":
            current["active_realm"] = realm
    if "upstream_effort" in patch:
        effort = str(patch.get("upstream_effort") or "").strip().lower()
        if effort == "auto" or effort in EFFORTS:
            current["upstream_effort"] = effort
    if isinstance(patch.get("overrides"), dict):
        for model, provider in patch["overrides"].items():
            model = str(model or "").strip()
            provider = str(provider or "").strip()
            if not model:
                continue
            if provider in PROVIDER_SPECS:
                current["overrides"][model] = provider
            elif provider in ("", "auto", "default"):
                current["overrides"].pop(model, None)
    with _lock:
        data = load(data_dir)
        data["routing"] = current
        save(data_dir, data)
    return routing(data_dir)


# ---------------------------------------------------------------------------
# 面板会话（内存态：重启即登出，和两个上游一致）
# ---------------------------------------------------------------------------
class PanelSessions(object):
    """登录成功后发放的 bearer token；不落盘，重启就失效。"""

    def __init__(self, ttl=SESSION_TTL):
        self.ttl = ttl
        self._tokens = {}
        self._lock = threading.RLock()

    def create(self):
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens[token] = time.time() + self.ttl
        return token

    def valid(self, token):
        if not token:
            return False
        with self._lock:
            expiry = self._tokens.get(token)
            if not expiry:
                return False
            if expiry < time.time():
                self._tokens.pop(token, None)
                return False
            return True

    def revoke(self, token):
        if not token:
            return
        with self._lock:
            self._tokens.pop(token, None)
