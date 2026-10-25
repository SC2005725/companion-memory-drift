"""项目路径解析。

硬性要求（任务书十一、十三节）：
- 全部使用相对路径，任何路径都从项目根目录推导；
- 不允许出现 C:\\Users\\某人\\... 这类本机绝对路径；
- 换电脑后只要整个目录一起复制，路径依然有效。
"""

from __future__ import annotations

try:
    from pathlib import Path
except Exception:  # pragma: no cover
    raise

ROOT = Path(__file__).resolve().parents[1]

CONFIG_DIR = ROOT / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
PROMPTS_DIR = CONFIG_DIR / "prompts"

PERSONALITY_FILE = ROOT / "personality" / "personality.json"
# Stage 2「Companion Generation」的产物：小满的**初始角色模型**。
#
# 与 PERSONALITY_FILE 分开存，理由：
#   personality.json  稳定底色（性格/价值观/边界/表达风格）—— 手工维护、sha256 守护
#   profile.json      初始设定（身份/人生史/欲望/恐惧/自我认知）—— 一次性生成后基本不动
# 混在一起会让「人格漂移」与「初始设定变化」无法区分。
PROFILE_FILE = ROOT / "personality" / "profile.json"
# Stage 2 生成用的外置提示词（与 consolidate.md 同一思路：可版本化、可对照）。
IDENTITY_PROMPT_FILE = CONFIG_DIR / "prompts" / "identity.md"

CONVERSATION_DIR = ROOT / "conversation"
DB_FILE = CONVERSATION_DIR / "conversations.db"

MEMORY_DIR = ROOT / "memory"
RAW_DIR = MEMORY_DIR / "raw"
RAW_EVENTS_DIR = RAW_DIR / "events"
# Phase 5 使用：记忆版本快照（v001.json / v002.json …），只写不改、永不覆盖。
VERSIONS_DIR = MEMORY_DIR / "versions"
# Phase 5 使用：版本之间的差异记录（v001__v002.json）。
CHANGELOG_DIR = MEMORY_DIR / "changelog"
# Phase 5 使用：外置提示词（consolidate.md 等），便于版本化与对照。
CONSOLIDATE_PROMPT_FILE = CONFIG_DIR / "prompts" / "consolidate.md"
# Phase 5 使用：回忆探针记录（一探针一 JSON + .sha256）。
# 与 conversation/ 分开存 —— 探针是对「记忆状态」的观测记录，不是用户对话。
RECALL_DIR = ROOT / "recall"
# Phase 6 使用：关系状态快照（版本化，永不覆盖）。
# 真值在 relationship_events 表里，这里只是留痕便于人工查看与对比。
RELATIONSHIP_DIR = ROOT / "relationship"
# Phase 7 使用：用户模型快照（她眼中的用户）。
# 版本化、永不覆盖；已修正的条目保留在文件里，只标记被谁取代。
USER_MODEL_DIR = ROOT / "user_model"
# 小满的自我模型 —— 「小满眼中的自己」。
#
# ## 为什么与 USER_MODEL_DIR 分开（双模型）
#
#   self_model/  → 小满对自己：稳定底色之外的**自我认识与适配做法**（小满自己维护）
#   user_model/  → 小满眼中的我：小满对**用户**的理解（小满自己维护）
#
# 两者都是「小满自己会改的」，但**改的是不同的东西**，混存会让
# 「我改了自己的做法」和「我改变了对他的理解」无法区分。
SELF_MODEL_DIR = ROOT / "self_model"

# Phase D 新增：情感状态的版本化快照目录（只追加，沿用 snapshots 约定）。
# 与 relationship / self_model / user_model 并列 —— 同一种存储形态，不另造体系。
EMOTION_DIR = ROOT / "emotion"
# Phase 9 使用：工具「记笔记」写下的文件。
# 刻意放在项目内 —— 工具的权限边界就是把所有路径钉在项目根目录内。
NOTES_DIR = ROOT / "notes"

# 用户分享的图片。**内容寻址**：文件名由 sha256 算出，绝不接受用户输入 ——
# 这样不需要去重逻辑，也从根上排除了路径穿越。见 `src/images.py`。
# 与记忆/关系/情绪不同：图片**不进数据库**，只落盘；
# 它进入系统的方式是把引用拼进用户消息（多模态）并记一句到事件里。
UPLOADS_DIR = ROOT / "uploads"

EXPERIMENTS_DIR = ROOT / "experiments"
LOGS_DIR = ROOT / "logs"
LEDGER_FILE = LOGS_DIR / "ledger.jsonl"
RUN_LOG_FILE = LOGS_DIR / "run.log"

ENV_FILE = ROOT / ".env"
ENV_EXAMPLE_FILE = ROOT / ".env.example"


def ensure_dirs() -> None:
    """建立运行期需要的目录。只创建目录，不写入任何数据。"""
    for d in (
        CONFIG_DIR,
        PROMPTS_DIR,
        ROOT / "personality",
        CONVERSATION_DIR,
        RAW_EVENTS_DIR,
        VERSIONS_DIR,
        CHANGELOG_DIR,
        RECALL_DIR,
        RELATIONSHIP_DIR,
        USER_MODEL_DIR,
        SELF_MODEL_DIR,
        EMOTION_DIR,          # ← Phase D 新增时**漏加过**，导致 save_state 写文件失败
        NOTES_DIR,
        EXPERIMENTS_DIR,
        LOGS_DIR,
        UPLOADS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


def display(path) -> str:
    """把路径显示为项目内相对路径，便于日志和报告保持可迁移。"""
    p = Path(path)
    try:
        rel = p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()
    return rel if rel and rel != "." else "(项目根目录)"
