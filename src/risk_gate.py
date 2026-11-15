"""风险三档分类器（CE-4 档一，调研报告《自主性权限与协商机制调研-20261108.md》§四）。

## 它解决什么问题

此前共生的防线是**散装的**：fs 有 DENY 规则、exec 有白名单、webui 有四道校验——
但没有任何一个地方回答"**这个动作危不危险、要不要找人**"。
深度审查证明散装防线必有缝（delete "." 删整库就是缝里漏出来的）。

## 判据（业界三因子，OpenAI Operator 同款）

风险 = 写权限 × 不可逆性 × 影响范围。落到共生的动作上就是三档：

  🟢 low    —— 只读、查账：不过闸，靠既有审计留痕
  🟡 medium —— 可逆写入：自动执行 + 留痕 + 事后可在台账看到
  🔴 high   —— 不可逆/执行代码/改人格配置：**必须老板批准**才放行

## 纪律（防"审批疲劳"）

闸只设在 high：medium 全自动（否则人会对着确认框闭眼点同意）。
deny 类黑名单（core_fs.deny_reason / ROOT 守卫）**永远在闸之外先执行、
优先级更高**——批准了也进不去黑名单。

档一刻意不做动态风险识别（模型自评等）——写死规则可测试、可解释；
动态版是档二的事。
"""
from __future__ import annotations

TIER_LOW = "low"
TIER_MEDIUM = "medium"
TIER_HIGH = "high"

# 高危动作：不可逆（delete）或执行任意代码（exec）。
# exec 即便有白名单也判高危——白名单限制"能跑什么"，
# 不改变"跑了就有外部副作用"这一事实。
HIGH_ACTIONS = ("delete", "exec")

# 高危写入目标：改人格、改配置、改协同规则 = 改"她是谁/她怎么办事"。
# 命中判断用小写正斜杠相对路径的前缀/精确匹配。
SENSITIVE_WRITE_PREFIXES = (
    "config/",           # 全部配置目录
    "docs/鹤辞规则",      # 人格规则文档
)
SENSITIVE_WRITE_NAMES = (
    "personality.json",
    "settings.json",
    "soul.md",
    "identity.md",
)


def classify(action: str, *, path: str | None = None,
             cmd: list | None = None) -> dict:
    """给一个待执行动作判风险档。纯函数，零 IO，可随意测试。

    返回 {"tier": low|medium|high, "reason": 一句话判据}。
    path 传**相对项目根**的原始路径（内部统一成小写正斜杠再判）；
    cmd 是 exec 的命令列表。
    """
    action = str(action or "").lower()

    if action == "exec":
        return {"tier": TIER_HIGH,
                "reason": "执行外部命令（跑代码 = 有外部副作用）"}

    if action == "delete":
        return {"tier": TIER_HIGH,
                "reason": "删除不可逆（文件/目录删了就没了）"}

    if action in ("write", "mkdir"):
        p = str(path or "").strip().replace("\\", "/").lower()
        if not p:
            return {"tier": TIER_MEDIUM, "reason": "常规写入（无明确目标）"}
        for pref in SENSITIVE_WRITE_PREFIXES:
            if p.startswith(pref):
                return {"tier": TIER_HIGH,
                        "reason": f"改配置/规则（{pref}* 属于人格与协同规则区）"}
        name = p.rsplit("/", 1)[-1]
        if name in SENSITIVE_WRITE_NAMES:
            return {"tier": TIER_HIGH,
                    "reason": f"改人格/配置文件（{name}）"}
        return {"tier": TIER_MEDIUM, "reason": "常规文件写入（可逆，git 可回滚）"}

    return {"tier": TIER_LOW, "reason": "只读/非写入动作"}
