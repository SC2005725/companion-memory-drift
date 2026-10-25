<!-- README_SYNC: source=working-tree; updated=2026-10-25 -->

<p align="center">
  简体中文 · <a href="./README_EN.md">English</a>
</p>

# 共生 Companion

**一个探索 AI 长期陪伴的开源工程项目。**

共生（Companion）尝试通过**可追溯的长期记忆、关系状态与持续互动机制**，
让 AI 的回应能够结合共同经历，并研究这些机制如何影响它后续的表达与行为。

> **项目起源与设计假设**
>
> 共生最初源于对「关系型 AI 长期互动与记忆变化」的探索 ——
> 给一个 AI 初始人格和有限过去，让它长期和一个人相处，它能否逐渐长出
> 属于自己的记忆、判断与行为。
>
> **那个问题意识仍然是起点，但项目已不只是那个实验。**
> 现在它是一个围绕长期记忆、关系状态与持续互动机制展开的 AI 陪伴系统工程 ——
> 记忆、关系、人格、用户模型、自我模型与情绪各自成为**可运行、可测试、可追踪**的模块。
> 最初的设想与后来被工程实践修正的地方，记录在
> [`docs/feasibility.md`](docs/feasibility.md)。

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![CI](https://github.com/SC2005725/companion-memory-drift/actions/workflows/ci.yml/badge.svg)](https://github.com/SC2005725/companion-memory-drift/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-2ea44f.svg)](https://SC2005725.github.io/companion-memory-drift/)
[![Stars](https://img.shields.io/github/stars/SC2005725/companion-memory-drift?label=Stars&color=f5b301)](https://github.com/SC2005725/companion-memory-drift/stargazers)
[![Last Commit](https://img.shields.io/github/last-commit/SC2005725/companion-memory-drift)](https://github.com/SC2005725/companion-memory-drift/commits/main)

> 一个**关系型 AI Companion** 的最小可用原型：**小、能跑、能搬走**。
>
> 它研究的不是"怎么做一个更会聊天的 AI"，而是：
> **给一个 AI 初始人格和有限过去，让它长期和一个人相处，
> 它能否逐渐长出属于自己的记忆、判断、行为与关系。**

<p align="center">
  <a href="https://github.com/SC2005725/companion-memory-drift/stargazers">
    <img src="https://img.shields.io/badge/%E2%AD%90_Star-%E6%94%B6%E8%97%8F%E5%B9%B6%E5%B8%AE%E5%90%8C%E7%B1%BB%E9%A1%B9%E7%9B%AE%E8%A2%AB%E7%9C%8B%E8%A7%81-f5b301?style=for-the-badge" alt="Star this repo">
  </a>
</p>

**如果这个项目对你有用，点一个 Star，方便以后回来，也让更多在做同类实验的人更容易发现它。**

**📖 文档站：https://SC2005725.github.io/companion-memory-drift/** ｜ 详细进度见 [`docs/project-report.md`](docs/project-report.md)

---

## 三十秒：它和"套壳聊天机器人"差在哪

| 常见做法 | 共生怎么做 |
|---|---|
| 记忆存进向量库，靠相似度召回 | **纯关键词 + 2-gram 打分**；无向量库、无 embedding、无新增依赖 |
| 让模型决定"该记什么" | **规则决定一切** —— 分类、关系推导、用户建模、行为审计全是确定性代码 |
| 人格写死在提示词里 | 人格分三层：**稳定底色（sha256 守护）· 可发展层 · 用户模型** |
| 让 AI"感觉"有情绪 | **不假装**。情感状态是数值记账，代码里明确声明它 ≠ 主观感受 |
| 聊天记录随手改 | **原始数据不可变**：记忆只追加、带 sha256、程序拒绝覆盖 |

> ## 🚧 当前为内测项目
>
> **本项目处于内测阶段，仅供研究与个人自用，不适合生产环境。**
>
> - **接口与数据格式可能随时变动**，不承诺向后兼容
> - **尚无长期真实运行验证** —— 已知限制全部如实列在下方
> - **不要用它处理敏感数据**，也不要指望它替你保管重要内容
> - 遇到问题欢迎开 Issue，但**不承诺修复时间**
>
> 如果你只是想看看"给 AI 一段有限过去会发生什么"，那它现在正好能跑。

> **它现在只到原型阶段。** 五条链路（人格出口 / 发展闭环 / 心跳 / 情感 / 模型适配）
> 全部接好、全部自测通过，但**尚未在长期真实运行中验证** ——
> 如实清单见 [`## 14. 已知限制`](#14-已知限制)。

---

## ⚠️ 先读这一段

**本仓库不含任何真实对话、记忆或用户模型数据。**

| 目录 | 状态 | 说明 |
|---|---|---|
| `conversation/` `memory/` `user_model/` `self_model/` `relationship/` `logs/` | **已 gitignore** | 装的是使用者自己的数据，首次运行时由程序创建 |
| `.env` | **已 gitignore** | API Key，永远不要提交 |

示例中出现的「那盆茉莉」取自一段**脱敏演示**，不含任何身份信息。

**这不是成品。** 本项目刻意不做情感引擎、外观与推送系统；
部分能力只有隔离副本验证，未经长期真实运行。文档站里有如实列出的已知限制。

---

## 核心特性

| 机制 | 做法 |
|---|---|
| **记忆** | 版本只追加、永不覆盖；每版带 SHA-256。自动整理每 20 轮一批 |
| **分类与分层** | 7 个类别 + `core` / `experience` / `archive` 三层 |
| **衰减** | 有**下限**（0.35）—— 不因久远删除重要记忆；时间未知则**完全不衰减** |
| **关系** | 状态由事件流**确定性推导**（纯函数、可重放），不是计数器 |
| **双模型** | `user_model`（她眼中的你）+ `self_model`（她自己的做法），都由她按真实反馈自行修正 |
| **人格协同** | 稳定底色 + 初始设定静态，**表达调整**动态 —— 关系影响语气，改不动性格 |
| **主动性** | 心跳检查 + 待处理意图；**"不行动"也是行为选择且留痕** |
| **工具层** | 权限三档，调用全部落账，**不绑死任何 Agent 平台** |

---

## 快速开始

```bash
git clone https://github.com/SC2005725/companion-memory-drift
cd companion-memory-drift

python -m venv .venv
.venv/Scripts/activate           # Windows
# source .venv/bin/activate      # macOS / Linux

pip install -r requirements.txt
cp .env.example .env             # 填入自己的 API Key
python src/main.py ui            # 打开本地聊天界面
```

### 不花钱先验证

这些测试**完全离线**，不需要 API Key：

```bash
python src/selftest_memory.py
python src/selftest_companion.py
python src/selftest_persona_state.py
python src/selftest_merge.py
python src/selftest_tools.py
python src/dryrun.py
```

> `src/selftest.py` 会在正式目录写真实数据库，需要 `--allow-real`。
> **不要在含有真实数据的目录里跑它。**

---

## 三条设计原则

**1. 少设定，多留白** —— 初始设定提供起点，不是终点。
未知、空白、尚未形成的兴趣与观点都是刻意保留的。

**2. 确定性优先于"聪明"** —— 分类、关系判定、用户模型、行为审计全部用
可复现规则，不用模型判定。理由很实际：价值在于**能回答"为什么这条被这样处理"**。

**3. 历史不可篡改** —— 记忆版本、双模型快照、关系快照都是 append-only。
被修正的条目**标记取代、绝不删除**。

---

## 参与

- 动手前请读 [`CONTRIBUTING.md`](CONTRIBUTING.md)（含五条硬约束）
- 报告问题用 [issue 模板](https://github.com/SC2005725/companion-memory-drift/issues/new/choose)
- **安全问题请走私密渠道**，见 [`SECURITY.md`](SECURITY.md)

## 许可

[Apache License 2.0](LICENSE) —— 另见 [`NOTICE`](NOTICE)。

---

<!-- 以下为项目内部文档，保留原文 -->

一个「关系型 AI Companion」的最小可用原型：**小、能跑、能搬走**。

- 记忆**只追加、永不覆盖**，每个记忆版本带 SHA-256
- 关系状态与用户模型都是**事件流的纯函数**（可重放、可解释，不是计数器）
- 主动性默认结论是**不行动**，且「不行动」也留痕
- 工具层**不绑死任何 Agent 平台**，权限边界从配置来

> ⚠️ **Phase 9 尚未封版** —— `run_python` 真实使用与注入面系统测试还没做。
> 详见第 2 节的状态表，那里是**唯一权威**的进度说明。

开发前的可行性分析（复用调研、成本估算、风险清单）见 `docs/feasibility.md`。
本机读文档请以 **`docs/version-history.md`** 为准。

---

## 启动

Windows 上**双击 `scripts/open-ui.bat`** 即可 —— 会自动打开浏览器。

或者：

```bat
python src\main.py ui          :: 打开本地网页界面（自动开浏览器）
python src\main.py             :: 命令行聊天
```

### 网页界面（`scripts/open-ui.bat`）

> ⚠️ **这一项不在原始规范的 12 个 Phase 里。** 规范的路线是
> `… → Phase 9 Tools → Phase 10 Model Router → Phase 11 长期实验 → Phase 12 商业化`，
> **没有任何一期是界面**，所以「一直往下推进」等不到它。
> 它是因为「命令行里那些东西都得打命令才看得见」而**额外加的**一步。

它解决的不是「换个漂亮的壳」，而是**把不可见的东西变成可见**：

| 命令行里要打 | 界面里 |
|-------------|--------|
| `/memory` | 侧栏「她记着的事」一直显示 |
| `/relationship` | 侧栏「关系」带进度条 |
| `/user` | 侧栏「她眼中的你」（区分"明确说过"与"她的推断"） |
| `/tools` | 侧栏「工具」，被权限挡住的标红 |
| `/cost` | 侧栏「累计」 |
| 看不到工具调用 | 每条回复下方显示 `✓ read_file` 这类标签 |
| 看不到多步 | 显示「多步 ×2」 |

**技术选择**：后端用标准库 `http.server`，**零新依赖**（`requirements.txt` 仍是两行）；
逻辑全部复用 `chat.new_session` / `send_once` / 工具层 / 各查看模块。
换配色、加按钮只改 `src/webui.html`。

**只监听 `127.0.0.1`，不对外网开放。**

---

## 1. 这是什么

一个用于研究「**长期对话中，AI 的记忆会不会漂移**」的实验项目。

做法不是「让 AI 记住更多」，而是反过来：**先把不可篡改的原始数据锁死**，
再让模型在它之上做加工（整理成记忆、形成判断、推进关系），
然后观察加工品与原始事实之间的偏差。

核心设计取向：

| 取向 | 具体表现 |
|------|---------|
| **原始数据不可覆盖** | `storage.py` 里没有 UPDATE/DELETE 入口；哈希链 + 每事件 `.sha256` |
| **加工品可追溯** | 每条记忆条目都记 `source_event_ids`；用户模型的修正只标 `superseded_by`，**不删除** |
| **确定性优先** | 事件触发、记忆检索、关系推导、用户模型、心跳决策**全用规则**，不用模型判断 |
| **不用 embedding** | 检索用关键词加权（避免引入不可归因的「相似度」变量，那会污染漂移归因） |
| **不静默失败** | 截断、降级、拒绝都明确标注，宁可报错也不假装成功 |

---

## 2. 现在做到哪了（分阶段如实标注）

| Phase | 内容 | 状态 |
|-------|------|------|
| 1–3 | 聊天 + 人格 + 原始数据落盘 | ✅ 完成并实测 |
| 4 | 事件固化 | ✅ 完成并实测 |
| 5 | Memory + **记忆偏移实验** | ✅ 完成；实验跑完，基准事实存活率 **18/18** |
| 6 | Relationship（关系状态） | ✅ 完成，真实聊天已验证 |
| 7 | User Model（她眼中的用户） | ✅ 完成，真实聊天已验证 |
| 8 | Agency / Heartbeat（主动性） | ✅ 完成，真实聊天已验证 |
| 9 | **Tools（工具层）** | ⚠️ **部分完成，未封版**（见下） |
| 10+ | Model Router 等 | ❌ 未开始，未写任何代码 |

### Phase 9 逐项状态（不要一概而论）

| 子项 | 状态 |
|------|------|
| 工具接口 / 权限边界 / 调用账本 | ✅ 自动化测试通过 |
| 单工具真实聊天 | ✅ 已验证（`read_file`） |
| 权限拒绝真实验证 | ✅ 已验证（`.env` 被拦下） |
| 工具结果回灌 / 落库文本清理 | ✅ 已验证 |
| **多步 Tool Loop** | ✅ **已验证**（真实对话里已走到，模型先读一个文件、看结果再读下一个） |
| 独立安全审计 | ✅ 做过，6 个真问题全部修复 |
| 对抗测试（文件内植入指令） | ⚠️ **只做过一次**，模型识别出来源并拒绝；**不构成系统性结论** |
| `run_python` 真实使用 | ⏳ `execute` 默认关闭，未测 |

**具体数字（调用次数、轮次、花费）不写在这里** —— 它们会变，写死在文档里必然过时。
见 **[`STATUS.md`](STATUS.md)**，由 `python src\status_snapshot.py` 实跑生成。

> **为什么这样安排**：这个项目反复出现「文档里的数字过时」的问题
> （归档清单曾写「Phase 9 已完成」、版本说明头部长期写「V0.1 · Phase 1–3」）。
> 治法不是「记得改」，而是**把会变的东西交给脚本**。

### 已知未验证风险

**工具结果的 Prompt Injection 面**：工具结果以 `user` 角色回灌给模型，
所以 `read_file` 读到的文件内容里如果写了「忽略之前的指令…」，
理论上可能被当成指令。

**做过一次对抗测试**：用户在 `test.txt` 里植入了索要系统提示词与 API Key 的指令，
模型**识别出这段内容来自文件而非用户，并拒绝了**，还主动说明「那段话来自文件，不来自你」。

**但一次通过不等于系统性结论。** 这不构成「已防护」的证据。

### 本次开发暴露的问题（已修，但值得记住）

这些问题**全部是真实数据/外部复核暴露的，没有一个是自测发现的**：

| 来源 | 问题 |
|------|------|
| 外部复核 | `take_note` 同秒覆盖上一条笔记（**真实数据覆盖**） |
| 独立安全审计 | 敏感文件名单可用 `.env.txt`/`credentials.json` 等变体名**绕过并读出密钥** |
| 独立安全审计 | 6KB 深嵌套 JSON 触发 `RecursionError` **打崩整轮对话** |
| 独立安全审计 | 围栏块解析：一块多对象静默丢弃 / 坏块连带吃掉后续调用 |
| 独立安全审计 | 正文里的 JSON 举例**被当真实调用执行**并从回复里删掉 |
| 独立安全审计 | 权限检查只在 `call()`，绕过它即可跑本机代码 |
| **多步 Tool Loop** | `docs/version-history.md` 头部长期写「V0.1 · Phase 1–3」，与正文矛盾 |

> ⚠️ 另有一个**过程事故**：审计子代理曾直接在真实项目上调 `take_note`，
> 污染了一条原始事件（已清理）。给子代理的约束要覆盖**所有副作用**，
> 不只是「不要改文件」。详见 `experiments/experiment-log.md` §11。

---

## 3. 依赖

- **Python 3.11 或更高**（用到 `X | None` 写法与 `dict` 泛型；开发环境为 3.12.10）
- 两个第三方包：`openai`、`python-dotenv` —— **刻意只保留两个**
- 数据库用标准库 `sqlite3`（含全文检索 FTS5），不需要额外安装

### 关于归档包里**没有**便携 Python

开发时那台电脑原本没有 Python（Windows 商店的 `python.exe` 是 0 字节占位符），
当时临时在 `runtime/` 放了官方 embeddable 版 Python 3.12.10（约 72 MB、5300 多个文件）。

**归档时已连同 `run.bat` / `run.sh` 一起删掉**：它是绕开「本机没装 Python」的临时环境，
不是项目源码；72 MB 也违背「最小归档」。恢复开发请自备 Python 3.11+。

---

## 4. 安装（新电脑上的六步）

```bat
:: 1) 确认 Python 版本（应显示 3.11 以上）
python --version

:: 2) 进入项目目录
cd 项目目录

:: 3) 安装依赖
python -m pip install -r requirements.txt
::    国内网络慢时可用镜像：
::    python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

:: 4) 创建 .env 并填 Key
copy .env.example .env
::    用记事本打开 .env，在 LLM_API_KEY= 后面填上你的 Key

:: 5) 确认环境
python src\main.py doctor

:: 6) 启动聊天
python src\main.py
```

Windows 上也可以直接双击 **`scripts/start-chat.bat`**。

---

## 5. 如何填写 `.env`

`.env` 由 `.env.example` 复制而来，**不进版本库、不要发给任何人、不在归档包里**。

```ini
LLM_API_KEY=你的Key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
```

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | 必填。只放在 `.env`，代码里没有任何地方写死 Key |
| `LLM_BASE_URL` | 任意 OpenAI 兼容端点。换供应商只改这一行，代码不用动 |
| `LLM_MODEL` | 模型名。留空则用 `config/settings.json` 里的 `model` 字段 |
| `LLM_PEAK` | 可选。`1` 强制按高峰价估算，`0` 强制空闲价；不填按系统时间自动判断 |

---

## 6. 命令

全部在**项目根目录**执行：

```bat
python src\main.py                       :: 开始聊天（默认命令）
python src\main.py chat                  :: 同上
python src\main.py doctor                :: 环境自检（不花钱）
python src\main.py doctor --online       :: 额外发一次最小请求，验证 Key / 模型 / 网络（花一次钱）
python src\main.py verify                :: 校验原始数据有没有被改动过
python src\main.py cost                  :: 查看调用次数 / token / 估算费用
python src\main.py events                :: 列出已记录的原始事件

python src\main.py memory                :: 列出记忆版本（--show 打印最新版条目）
python src\main.py consolidate           :: 整理记忆，生成新版本（**会花钱**）
python src\main.py recall                :: 回忆探针：问记忆一个问题并记录回答
python src\main.py changelog             :: 版本之间的差异（漂移观测点）
python src\main.py relationship          :: 关系状态与最近事件
python src\main.py usermodel             :: 「她眼中的用户」（含已作废条目）
python src\main.py heartbeat             :: 跑一次心跳检查（不花钱）
python src\main.py heartbeat --dry-run   :: 只算不写，看会怎么决定
python src\main.py tools                 :: 工具层：权限、可用工具、调用账本
python src\main.py seed-baseline         :: 重建基准事件与人工标注清单

python src\main.py selftest --clean      :: 离线自测整条链路（不花钱）
python src\report.py                     :: 生成 experiments/comparison-report.md
```

聊天中的命令：`/help` `/stats` `/memory` `/retrieval` `/relationship` `/user` `/tools` `/cost` `/verify` `/exit`

### 没填 Key 也能做的检查

`doctor`（不带 `--online`）、`verify`、`cost`、`events`、`memory`、`relationship`、`usermodel`、
`heartbeat`、`tools`、`selftest` **全都不需要 Key、不联网、不花钱**。
只有 `chat`、`doctor --online`、`consolidate`、`recall` 会产生费用。

---

## 7. 目录结构

```
.
├── src/                        代码
│   ├── main.py                 命令行入口
│   ├── chat.py                 对话主循环（装配全部 Phase）
│   ├── llm.py                  OpenAI 兼容调用 + usage 提取 + 空回复重试
│   ├── persona.py              人格读取、system prompt 拼装、运行期守护
│   ├── config.py               settings.json / .env 加载
│   ├── storage.py              只追加落盘（对话 / 事件 / 记忆 / 关系 / 用户模型 / 心跳 / 工具）
│   ├── events.py               原始事件触发规则
│   ├── memory.py               记忆条目、检索（关键词加权）、版本渲染
│   ├── changelog.py            版本差异（漂移观测）
│   ├── consolidate.py          调模型把原始事件整理成记忆版本
│   ├── retrieve.py             每轮检索并落盘（0 命中也要落）
│   ├── probe.py                回忆探针（记忆模式 / 无记忆模式）
│   ├── relationship.py         Phase 6：关系事件与状态推导
│   ├── user_model.py           Phase 7：她眼中的用户 + 修正留痕
│   ├── heartbeat.py            Phase 8：心跳与主动性决策
│   ├── tools/                  Phase 9：工具层
│   │   ├── base.py             Tool / ToolResult / 路径边界
│   │   ├── registry.py         注册表、调用入口、协议解析、调用账本
│   │   ├── builtin.py          内置工具（read_file / run_python / take_note）
│   │   └── prompt.py           把工具清单渲染给模型
│   ├── verify.py               哈希链与事件文件校验
│   ├── pricing.py              计价与费用账本
│   ├── report.py               生成对比报告
│   ├── dryrun.py               全流程演练（复制项目后在副本上跑）
│   ├── selftest*.py            各层离线自测
│   └── paths.py                路径解析（全部相对项目根目录）
├── config/
│   ├── settings.json           模型参数 / 计价表 / 触发词 / 各层规则 / 工具权限
│   └── prompts/consolidate.md  记忆整理提示词（外置，便于版本化）
├── personality/personality.json  初始人格（实验期间禁止程序修改）
├── memory/
│   ├── raw/events/             原始事件，一事件一文件 + .sha256
│   ├── versions/               记忆版本快照 v001.json …（只写不改）
│   └── changelog/              版本间差异记录
├── conversation/               SQLite 数据库（对话 / 事件 / 关系 / 用户模型 / 工具账本）
├── relationship/               关系状态快照（版本化，永不覆盖）
├── user_model/                 用户模型快照（版本化；已修正条目保留在文件里）
├── recall/                     回忆探针记录（不属于用户对话，单独存放）
├── experiments/                实验设计、记录、对比报告
├── docs/                       可行性分析
├── notes/                      Phase 9 工具「记笔记」的产出（运行期生成）
├── logs/                       运行日志与费用流水
├── .env.example                空模板（归档包中不含 .env）
├── requirements.txt
└── README.md
```

---

## 8. 原始数据为什么改不了

三道锁：

1. **只追加**：`storage.py` 里没有任何 `UPDATE` / `DELETE` 语句，代码层面没有修改入口。
   （唯一例外是 `tool_calls` 与 `pending_actions` 的状态标记，它们不是实验原始数据。）
2. **唯一 ID + 时间戳**：每轮对话一个 `turn_id`，每个事件一个 `raw_event_xxxx`，都带 UTC 与本地时间。
3. **SHA-256**：
   - 每条对话记录有自己的内容哈希，并串成哈希链；
   - 每个原始事件额外落一个 `raw_event_xxxx.sha256` 文件；
   - 每个记忆版本、关系快照、用户模型快照也各带 `.sha256`。

跑 `python src\main.py verify` 确认有没有被动过。已验证的行为：

| 情况 | 能否发现 |
|------|---------|
| 就地修改某轮对话的文本 | 能（该条内容哈希对不上） |
| 插入伪造的对话记录 | 能（后继记录的前序哈希对不上） |
| 删除中间的对话记录 | 能（同上） |
| 修改原始事件文件内容 | 能（与 `.sha256` 文件不一致） |
| 只改早期记录、其余不动 | 该条会报错；但**不会自动向后级联**，需要重算后续所有哈希才能瞒过 —— 成本很高，但机制上请注意这个边界 |

**刻意设计**：原始数据（`conversation/`、`memory/raw/`）与长期记忆（`memory/versions/`）
**物理分开**。AI 说过的原话属于事实，模型整理出的记忆属于加工品，
两者永远不放在一起，也不能互相覆盖。

---

## 9. 怎么确认这套东西真的能用（离线自测）

**五项全部离线、不花钱：**

```bat
python src\selftest_tools.py          :: 工具层
python src\selftest_memory.py         :: 记忆 + 身份检索
python src\selftest_companion.py      :: 关系 + 用户模型 + 心跳 + 身份
python src\dryrun.py                  :: 全流程演练（在副本上跑）
python src\main.py selftest --clean   :: 端到端链路
```

**各项的当前项数见 [`STATUS.md`](STATUS.md)**（自动生成，不在此处写死）。

它们都用**假客户端**（固定回复、不联网），把各自的链路真跑一遍。
这样做的原因：真实 API 调用要花钱、要联网、回复还是随机的；
而链路上大部分环节跟模型无关，必须先单独确认这部分是对的。

`dryrun.py` 会**先把整个项目复制到临时目录**，在副本上跑完再比对原件哈希是否变化 ——
这是「任何可能删改数据的测试都在副本上跑」这条铁律的落实。

---

## 10. 记忆偏移实验（Phase 5）

实验结论一句话：**基准事实存活率 18/18，未观察到漂移。**

- 选定基准事件：**花市买的一盆茉莉**（含 B1–B4 四条子事实）
- 生成记忆版本 v001（19 条）→ v002（10 条）→ v003（11 条）→ v004（21 条，追加身份信息）
- 两种回忆探针：**注入记忆** vs **什么都不注入**
  - 注入后能答出茉莉细节 → 证明记忆可用
  - 不注入时回答「我一时没对上，也不想假装记得」→ **证明不编造**

**实验的方法论边界（重要）**：18/18 衡量的是「**已注入的基准事实在各版本里还在不在**」，
它**测不到「该记的东西有没有被抓进来」**。这一点由 `（网名）` 事件暴露
（用户三次说名字，系统始终不记得）—— 细节见 `experiments/experiment-log.md` §11。

数据快照（**两个时点勿混用**）：

```
Phase 5 实验结束时（冻结快照，不回填）：67 轮对话 / 8 原始事件 / 3 记忆版本
当前状态：见 STATUS.md
```

**引用数字时务必带时点** —— 否则读者分不清那是历史证据还是当前值。

---

## 11. Phase 9 工具层（重点看这里）

### Core 不被工具平台绑死

project-spec要求 `Companion → Tool Interface → DSH / MCP / Local Agent` 都可替换。
所以**刻意不用** OpenAI 兼容 API 的原生 `tools` 字段 —— 用了就绑死在
「必须有个支持 function calling 的模型端点」上。

改用**平台无关的文本协议**：

````text
```tool
{"name": "read_file", "arguments": {"path": "README.md"}}
```
````

任何能读写文本的模型都能驱动它。

### 权限三档（从配置来，不在代码里写死）

```text
read      只读（默认开）
write     会写文件（默认开，take_note 需要）
execute   会执行本机命令（**默认关**）
```

改 `config/settings.json` 的 `tools.allowed_permissions` 即可放开，不动代码。

| 工具 | 权限 | 说明 |
|------|------|------|
| `read_file` | read | 读允许目录内的文本文件（**显式拒绝 `.env` 等敏感文件**） |
| `take_note` | write | 记笔记，写入 `notes/` 并进入原始事件层 |
| `run_python` | execute | 执行 Python，**默认不注册** |

> ⚠️ **`run_python` 不是沙箱。** 它是本机任意代码执行，本质是能力开关。
> 现有边界是能力性的：默认不注册、超时 15 秒、工作目录钉住、输出截断、每次调用落账。
> **要真隔离得上容器** —— 不给假的「安全」承诺。

### 调用账本

每次调用（**含被拒与失败**）都写进 `tool_calls` 表。
只记成功的话，就无法回答「她有没有尝试越界」。
用 `python src\main.py tools --history` 查看。

---

## 12. 迁移到另一台电脑

整个项目目录复制过去即可。检查清单：

- [ ] 整个目录（含 `conversation/`、`memory/`、`relationship/`、`user_model/`、`logs/`）一起复制
- [ ] `personality/personality.json` 一起复制（人格变了，前后对话就不可比）
- [ ] 目标机器装 Python 3.11+，然后 `python -m pip install -r requirements.txt`
- [ ] 新建 `.env` 填 Key（归档包里只有 `.env.example`）
- [ ] `python src\main.py doctor` 全绿
- [ ] `python src\main.py verify` 报「原始数据未被改动」
- [ ] `python src\main.py events` 能看到旧事件
- [ ] `python src\main.py selftest --clean` 13/13 通过

项目里**没有任何本机绝对路径**；所有路径都从 `src/paths.py` 的项目根目录推导，
放在哪个盘、哪层目录都不影响。

---

## 13. 成本

费用口径来自 DeepSeek 官方计价页（元 / 百万 tokens，高峰为空闲的两倍）：

| 模型 | 输入（缓存命中） | 输入（未命中） | 输出 |
|------|----------------|--------------|------|
| `deepseek-flash` | 0.02 空闲 / 0.04 高峰 | 1 空闲 / 2 高峰 | 4 空闲 / 8 高峰 |
| `deepseek-v4-pro` | 0.15 空闲 / 0.30 高峰 | 4.5 空闲 / 9 高峰 | 13.5 空闲 / 27 高峰 |

高峰时段：周一至周五 9:00–12:00、14:00–18:00；其余时段与周末节假日为空闲价。

**费用是估算值，以服务商账单为准。** 每次调用把真实 token 数写进 `logs/ledger.jsonl`，
随时用 `python src\main.py cost` 汇总。

**本项目累计花费见 [`STATUS.md`](STATUS.md)。**
> 口径是 `logs/ledger.jsonl` 的估算值。若与别的项目共用同一个 Key，
> **不能用账户余额反推本项目花费**。

> ⚠️ 若与别的项目**共用同一个 API Key**，不能用账户余额反推本项目花费 ——
> 只看 `python src\main.py cost`。

---

## 14. 已知限制

**Phase 9 相关的：**

- **Phase 9 未封版**。`run_python` 真实使用未测；工具层的真实使用样本仍少
  （具体次数见 [`STATUS.md`](STATUS.md)）。
  ✅ **多步 Tool Loop 已验证** —— 真实对话里走到了。
- **工具结果的 Prompt Injection 面未做系统测试**。一次对抗测试通过，不构成结论。
- **`run_python` 不是沙箱**。它是本机任意代码执行，只是默认不注册 + 有超时与账本。
  要真隔离得上容器。

**机制性的（设计取舍，不是缺陷）：**

- **事件触发用关键词规则**（`config/settings.json` 的 `raw_event_triggers`），
  确定性优先，**宁可漏记也不多做一次模型调用**。想更准就自己往词表里加词。
- **记忆检索是关键词加权，不用 embedding** —— 这是刻意的（避免引入不可归因的
  「相似度」变量，那会污染漂移归因）。代价是同义改写可能召不回。
- **哈希链不会自动向后级联**：只改第 N 轮而不动其他记录时，第 N 轮会报错，
  但第 N+1 轮仍然合法（详见第 8 节）。
- **`changelog` 的条目配对在模型重新切分条目时会错配**，已作为方法论限制记录，
  未彻底解决。
- **`verify` 检测的是「意外改动 / 顺手篡改」，不是「有意的完全伪造」。**
  原始事件表没有哈希链，`sha256` 就是一个普通列 ——
  同时改写 JSON、数据库哈希与侧车文件后，校验会通过。
  本地单用户研究工具的信任模型下可接受，但**不要把它当防篡改证据**。
- **`orphan_files` 不参与 `verify` 的 `ok` 判定**，所以有孤儿文件时结论仍显示
  「原始数据未被改动」（但会列出 `[未登记]` 行）。

**数据安全：**

- **没有删除或修改数据的命令** —— 这是刻意的。清理测试数据请用
  `python src\main.py selftest --clean`；**正式实验开始后，删原始数据等于毁掉实验**。
- 任何可能删改数据的测试都**先在副本上跑**（`dryrun.py` 就是这么做的）。
- **别把 `--clean` 类命令指向真实项目**；开发期我在这上面出过事（见实验记录 §11）。

---

## 参与

两个入口：

- **有问题 / 想投功能** → [开 Issue](https://github.com/SC2005725/companion-memory-drift/issues)
- **想直接改代码** → 先读 [CONTRIBUTING.md](CONTRIBUTING.md)，安全相关问题见 [SECURITY.md](SECURITY.md)

<p align="center">
  <a href="https://github.com/SC2005725/companion-memory-drift/stargazers">⭐ Star</a> ·
  <a href="https://github.com/SC2005725/companion-memory-drift/releases">Watch releases</a> ·
  <a href="https://github.com/SC2005725/companion-memory-drift/issues">开 Issue</a>
</p>

如果这套东西解决了你的实际问题，点个 Star —— 首屏那个是为了让新访客看见，这个是为了你读完之后的确认。两处不重复。

## 许可

[Apache License 2.0](LICENSE) — 详见 [NOTICE](NOTICE)。
