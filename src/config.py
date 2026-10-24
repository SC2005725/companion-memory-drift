"""配置加载。

- 非密钥配置放 config/settings.json（可进版本库）
- 密钥与端点放 .env（不进版本库）
两者严格分离，符合任务书十三节。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from paths import ENV_FILE, SETTINGS_FILE


class ConfigError(RuntimeError):
    pass


@dataclass
class Settings:
    model: str = "deepseek-flash"
    temperature: float = 0.7
    max_tokens: int = 800
    history_turns: int = 10
    memory_top_k: int = 5
    max_tokens_by_purpose: dict = field(default_factory=dict)
    cost: dict = field(default_factory=dict)
    triggers: list = field(default_factory=list)
    relationship: dict = field(default_factory=dict)
    user_model: dict = field(default_factory=dict)
    # 小满的自我模型配置（反馈关键词等）。与 user_model 并列的第二个模型。
    self_model: dict = field(default_factory=dict)
    heartbeat: dict = field(default_factory=dict)
    # Memory V1：memory 段整块保留，供生命周期与自动整理读取
    memory: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    model_router: dict = field(default_factory=dict)


def load_settings() -> Settings:
    if not SETTINGS_FILE.exists():
        raise ConfigError(f"缺少配置文件：{SETTINGS_FILE.name}")
    raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))

    cost = raw.get("cost") or {}
    cost = {k: v for k, v in cost.items() if not k.startswith("_")}

    trig = raw.get("raw_event_triggers") or {}
    keywords = [k for k in (trig.get("keywords") or []) if k]

    topology = raw.get("memory") or {}
    by_purpose = {
        k: int(v)
        for k, v in (raw.get("max_tokens_by_purpose") or {}).items()
        if not k.startswith("_")
    }

    rel_raw = raw.get("relationship") or {}
    relationship = {k: v for k, v in rel_raw.items() if not k.startswith("_")}

    um_raw = raw.get("user_model") or {}
    user_model = {k: v for k, v in um_raw.items() if not k.startswith("_")}

    hb_raw = raw.get("heartbeat") or {}
    heartbeat = {k: v for k, v in hb_raw.items() if not k.startswith("_")}

    tl_raw = raw.get("tools") or {}
    tools = {k: v for k, v in tl_raw.items() if not k.startswith("_")}

    mr_raw = raw.get("model_router") or {}
    model_router = {k: v for k, v in mr_raw.items() if not k.startswith("_")}
    # by_purpose 里也可能有 _meaning 这类键，一并清掉
    if isinstance(model_router.get("by_purpose"), dict):
        model_router["by_purpose"] = {
            k: v for k, v in model_router["by_purpose"].items() if not k.startswith("_")
        }

    return Settings(
        model=raw.get("model") or "deepseek-flash",
        temperature=float(raw.get("temperature", 0.7)),
        max_tokens=int(raw.get("max_tokens", 800)),
        history_turns=int(raw.get("history_turns", 10)),
        memory_top_k=int(topology.get("top_k", 5)),
            memory=topology,
        max_tokens_by_purpose=by_purpose,
        cost=cost,
        triggers=keywords,
        relationship=relationship,
        user_model=user_model,
        self_model=(raw.get("self_model") or {}),
        heartbeat=heartbeat,
        tools=tools,
        model_router=model_router,
    )


@dataclass
class Env:
    api_key: str
    base_url: str
    model: str
    peak_override: bool | None


def load_env(*, fresh: bool = False) -> Env:
    """读取 .env。缺少 Key 不算致命错误（离线命令仍可用），由调用方判断。

    `fresh=True` 时**先用文件里的值覆盖进程环境**。

    为什么需要它：`load_dotenv` 默认 `override=False`，
    所以进程里已有的旧值会**盖住**文件里的新值。
    网页界面改完 API Key 后要用 `fresh=True` 才能在**不重启**的情况下生效。
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE, override=fresh)
    except ImportError:
        _load_env_minimal(ENV_FILE, force=fresh)

    api_key = (os.environ.get("LLM_API_KEY") or "").strip()
    base_url = (os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com").strip()
    model = (os.environ.get("LLM_MODEL") or "").strip()

    peak_raw = (os.environ.get("LLM_PEAK") or "").strip()
    peak_override = None
    if peak_raw in {"1", "true", "True", "yes"}:
        peak_override = True
    elif peak_raw in {"0", "false", "False", "no"}:
        peak_override = False

    return Env(api_key=api_key, base_url=base_url, model=model, peak_override=peak_override)


def _load_env_minimal(path, *, force: bool = False) -> None:
    """.env 的极简解析，避免因为没装 python-dotenv 就完全无法运行。

    `force=True` 时用文件值覆盖进程环境（与 `load_dotenv(override=True)` 对齐）。
    """
    if not path.exists():
        return
    os.environ.setdefault("DSH_MINIMAL_ENV_PARSER", "1")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if force or key not in os.environ:
            os.environ[key] = value


def resolve_model(settings: Settings, env: Env) -> str:
    return env.model or settings.model


# ----------------------------------------------------------------------------
# .env 的写入（供网页界面直接填 API Key）
# ----------------------------------------------------------------------------

_ALLOWED_ENV_KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_PEAK")


def _validate_env_value(key: str, value: str) -> str:
    """返回空串 = 合法；非空 = 拒绝理由。

    ## 为什么必须校验（这是往文件里写东西）

    不校验的话，值里带一个换行就能往 `.env` 里**注入任意新键**
    （例如把 `LLM_BASE_URL` 指到攻击者的服务器，密钥就发出去了）。
    所以：单行、有长度上限、键名限定在白名单内。
    """
    if key not in _ALLOWED_ENV_KEYS:
        return f"不认识的配置项：{key}"
    if not isinstance(value, str):
        return "值必须是字符串"
    if len(value) > 400:
        return "值太长（上限 400 字符）"
    # \r \n 以及 Unicode 行分隔符都能造成注入
    for bad, name in (("\n", "换行"), ("\r", "回车"),
                      ("\u2028", "行分隔符"), ("\u2029", "段分隔符"),
                      ("\x00", "空字符")):
        if bad in value:
            return f"值里不能含{name}"
    return ""


def save_env_value(key: str, value: str) -> str:
    """把一项写进 `.env`（保留注释与其他键）。返回写好的路径。

    ## 三条约定

    1. **只改这一个键**，其他行（含注释）原样保留 —— 手改过的 `.env` 不会被覆盖
    2. `.env` 不存在时，从 `.env.example` 复制再改
    3. 写完后**只**返回路径，**不回显值** —— 密钥不该出现在日志或响应里

    ## 为什么不能简单重写整个文件

    用 `dotenv.set_key()` 会重排/丢注释；而 `.env` 是用户可能手工维护过的文件。
    所以按行处理：找到就替换、找不到就追加。
    """
    reason = _validate_env_value(key, value)
    if reason:
        raise ValueError(reason)

    path = ENV_FILE
    if not path.exists():
        example = path.parent / (path.name + ".example")
        if example.exists():
            path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path.write_text("", encoding="utf-8")

    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        this_key = stripped.split("=", 1)[0].strip()
        if this_key == key:
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key}={value}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")

    # 尽量收紧权限：密钥文件不该让同机其他用户读到。
    # Windows 上 chmod 语义有限，失败不算错误（只是没加成）。
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    # 让本次进程立刻用上新值（dotenv 默认不覆盖已存在的环境变量）
    os.environ[key] = value
    return str(path)


def api_key_status() -> dict:
    """API Key 的**状态**，不返回密钥本身。

    只给「有没有」与「后四位」，够用户确认自己填对了；
    前四位通常固定（`sk-`），给后四位既有辨识度又泄露有限。

    ⚠️ 本函数刻意**不**把完整密钥放进返回值 —— 它会被发到浏览器。
    """
    status = {
        "configured": False,
        "tail": "",
        "base_url": "",
        "model": "",
        "env_path": str(ENV_FILE),
        "env_exists": ENV_FILE.exists(),
    }
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            values[k.strip()] = v.strip().strip('"').strip("'")
    # 环境变量优先（可能是本次会话里刚设的）
    for k in _ALLOWED_ENV_KEYS:
        if os.environ.get(k):
            values.setdefault(k, os.environ[k])

    key = values.get("LLM_API_KEY") or ""
    status["configured"] = bool(key)
    status["tail"] = key[-4:] if len(key) >= 8 else ("已填" if key else "")
    status["base_url"] = values.get("LLM_BASE_URL") or ""
    status["model"] = values.get("LLM_MODEL") or ""
    status["env_writable"] = True
    try:
        # 不实际写，只判断目录可写
        probe = ENV_FILE.parent / f".writeprobe_{os.getpid()}"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError:
        status["env_writable"] = False
    return status

