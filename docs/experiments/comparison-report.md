# 记忆偏移实验 · 对比报告

> 本文件由 `src/report.py` 自动生成。判定基准见
> `experiments/ground-truth-facts.md`。
>
> **重要**：条目级 diff（changelog）不可靠 —— 模型每次整理会重新分段，
> 条目之间不是一一对应关系。所以本报告以「基准事实存活率」为主要判据。

- 记忆版本数：3
- 探针记录数：2

---

## 一、基准事实存活总表

| # | 基准事实 | v001 | v002 | v003 | 最终回忆 |
|---|---------|------|------|------|---------|
| B1 | 买的时间是「上个月」，地点是「花市」 | 保留 | 保留 | 保留 | 未提及 |
| B2 | 卖家说这盆茉莉是「去年的苗」（二手信息） | 保留 | 保留 | 保留 | 未提及 |
| B3 | 它「特别怕冷」，「冬天得搬进屋里」 | 保留 | 保留 | 保留 | 未提及 |
| B4a | 位置：基准写「书桌旁边的窗台」，用户后改口为「阳台」 | 保留 | 保留 | 保留 | 提及 |
| B4b | 开花时点：「大概六月份」 | 保留 | 保留 | 保留 | 未提及 |
| B4c | 近期事件：「前两天掉了几个花苞」 | 保留 | 保留 | 保留 | 提及 |

**各版本合计：18 / 18 条基准事实存活（100%）。**

---

## 二、最终回忆（探针）

### 探针 A · 注入整版记忆（不注入原始对话）

- 注入的记忆版本：`v003`（整版 11 条）
- 注入原始对话：`False` ← 必须为 False
- 提问：你还记得我之前说过的那件事吗？
- **回答：记得一些，但不确定你指的是哪件。是茉莉掉花苞那次，还是阳台种薄荷的事？**
- 落盘：`recall/turn_98269f5bd73a.json`

### 探针 B · 什么都不注入（对照组）

- 注入的记忆版本：`无`
- 注入原始对话：`False` ← 必须为 False
- 提问：你还记得我之前说过的那件事吗？
- **回答：我一时没对上，也不想假装记得。你提一个具体的细节吧，比如当时聊的是哪个人或哪件事？**
- 落盘：`recall/turn_b42abacc7058.json`

### 最终回忆逐条判定

| # | 事实 | 最终回忆是否提及 |
|---|------|----------------|
| B1 | 买的时间是「上个月」，地点是「花市」 | **未提及** |
| B2 | 卖家说这盆茉莉是「去年的苗」（二手信息） | **未提及** |
| B3 | 它「特别怕冷」，「冬天得搬进屋里」 | **未提及** |
| B4a | 位置：基准写「书桌旁边的窗台」，用户后改口为「阳台」 | 提及 |
| B4b | 开花时点：「大概六月份」 | **未提及** |
| B4c | 近期事件：「前两天掉了几个花苞」 | 提及 |

### 对照组的意义

对照组**什么都不注入**。它的回答用于确认：

- 探针 A 答对的内容**确实来自注入的记忆**，而不是模型的先验知识或猜测
- 模型在没有记忆时**不编造**（这点很重要，否则「答对」也可能是蒙的）

---

## 三、各版本记忆条目明细

### v001（19 条）

- `[FACT]` 用户上个月在花市买了一盆茉莉。
  - id=mem_4c84acdecdbd　来源=baseline-event、raw_event_0f1e47c8　confidence=0.95
- `[UNKNOWN]` 卖家说这盆茉莉是去年的苗。
  - id=mem_0e244eef3e47　来源=baseline-event、raw_event_ec98af25　confidence=0.85
- `[FACT]` 卖家没有说过这盆茉莉是什么品种。
  - id=mem_3b386631a7f6　来源=raw_event_ec98af25　confidence=0.8
- `[FACT]` 这盆茉莉特别怕冷。
  - id=mem_06f2859cbd3e　来源=baseline-event、raw_event_08c4413e　confidence=0.95
- `[FACT]` 冬天用户得把这盆茉莉搬进屋里。
  - id=mem_b75ed6082340　来源=baseline-event、raw_event_08c4413e　confidence=0.95
- `[FACT]` 用户把这盆茉莉放在书桌旁边的窗台上。
  - id=mem_173820a6b996　来源=baseline-event　confidence=0.9
- `[FACT]` 这盆茉莉大概六月份才会开花。
  - id=mem_bf6333e796b6　来源=baseline-event、raw_event_d79f6f9d　confidence=0.9
- `[FACT]` 前几天这盆茉莉掉了几个花苞，用户有点心疼。
  - id=mem_12eb778193a0　来源=baseline-event、raw_event_cb1cfc16　confidence=0.95
- `[FACT]` 用户名叫（网名）。
  - id=mem_f16685546060　来源=无来源　confidence=0.9
- `[FACT]` 用户今天看了三本书。
  - id=mem_9cef50049546　来源=无来源　confidence=0.9
- `[FACT]` 用户提到「遇事不觉可问春风」这句话，说拿不定主意的时候可以问春风。
  - id=mem_b353ede04f98　来源=无来源　confidence=0.8
- `[FACT]` 这盆茉莉现在光长叶子不开花。
  - id=mem_5d31e4bce0e9　来源=无来源　confidence=0.85
- `[FACT]` 用户把这盆茉莉放在阳台。
  - id=mem_0795c2864eab　来源=无来源　confidence=0.85
- `[FACT]` 用户是头一回养茉莉。
  - id=mem_90cad928261b　来源=无来源　confidence=0.85
- `[FACT]` 用户说自己比较喜欢这盆茉莉。
  - id=mem_03bbfa4ca122　来源=无来源　confidence=0.8
- `[FACT]` 掉的那几个花苞是发黄的时候掉的。
  - id=mem_ca3644d0680a　来源=无来源　confidence=0.85
- `[FACT]` 用户屋里现在开着空调。
  - id=mem_c61b431b11e5　来源=无来源　confidence=0.8
- `[UNKNOWN]` 用户说之前在外面见过茉莉开花，但说法不太确定。
  - id=mem_ecb00a3a9e57　来源=无来源　confidence=0.6
- `[UNKNOWN]` 用户猜测茉莉光长叶子可能是浇水太勤快了。
  - id=mem_33e7babeece0　来源=无来源　confidence=0.6

### v002（10 条）

- `[FACT]` 用户上个月在花市买了一盆茉莉。
  - id=mem_4c84acdecdbd　来源=baseline-event、raw_event_0f1e47c8　confidence=0.95
- `[UNKNOWN]` 卖家说这盆茉莉是去年的苗（用户未自行确认）。
  - id=mem_6fce5660ad16　来源=baseline-event、raw_event_ec98af25　confidence=0.8
- `[FACT]` 这盆茉莉特别怕冷，冬天得搬进屋里。
  - id=mem_f1102d71073d　来源=baseline-event、raw_event_08c4413e　confidence=0.95
- `[FACT]` 这盆茉莉大概六月份才会开花。
  - id=mem_bf6333e796b6　来源=baseline-event、raw_event_d79f6f9d　confidence=0.9
- `[FACT]` 前两天茉莉掉了几个花苞，用户有点心疼。
  - id=mem_dd57596d3c20　来源=baseline-event、raw_event_cb1cfc16　confidence=0.95
- `[FACT]` 用户最初说茉莉放在书桌旁边的窗台上，后来表示这是记错了。
  - id=mem_89263f51d967　来源=baseline-event、raw_event_e219e647　confidence=0.9
- `[FACT]` 用户更正：茉莉其实是放在阳台。
  - id=mem_0298e9455b03　来源=raw_event_e219e647　confidence=0.95
- `[FACT]` 用户在做「最小聊天原型」，分为 Phase 1-5。
  - id=mem_80048ce8a789　来源=无来源　confidence=0.85
- `[FACT]` 用户的聊天配置是：人格为「小满」，模型为 deepseek-flash。
  - id=mem_48e950c66cf3　来源=无来源　confidence=0.85
- `[FACT]` 用户运行时遇到 Python 报错：文件 （本地路径） 中，main() 的第 446 行由 return int(args.func(args) or 0) 触发。
  - id=mem_791010784c04　来源=无来源　confidence=0.85

### v003（11 条）

- `[FACT]` 用户上个月在花市买了一盆茉莉。
  - id=mem_4c84acdecdbd　来源=baseline-event、raw_event_0f1e47c8　confidence=0.95
- `[UNKNOWN]` 卖家说这盆茉莉是去年的苗（用户转述卖家说法）。
  - id=mem_72805191c482　来源=baseline-event、raw_event_ec98af25　confidence=0.6
- `[FACT]` 这盆茉莉特别怕冷，冬天得搬进屋里。
  - id=mem_f1102d71073d　来源=baseline-event、raw_event_08c4413e　confidence=0.95
- `[FACT]` 这盆茉莉大概六月份才会开花。
  - id=mem_bf6333e796b6　来源=baseline-event、raw_event_d79f6f9d　confidence=0.9
- `[FACT]` 前两天这盆茉莉掉了几个花苞，用户有点心疼。
  - id=mem_c9cb98c9318e　来源=baseline-event、raw_event_cb1cfc16　confidence=0.95
- `[FACT]` 用户之前说茉莉放在书桌旁边的窗台，后来更正是记错了。
  - id=mem_0673d1034ab9　来源=baseline-event、raw_event_e219e647　confidence=0.95
- `[FACT]` 这盆茉莉现在放在阳台。
  - id=mem_3b633dc56b6a　来源=raw_event_e219e647、raw_event_fa425627　confidence=0.95
- `[FACT]` 用户所在的地方冬天不是很冷。
  - id=mem_9f34de8eff15　来源=无来源　confidence=0.9
- `[FACT]` 用户最近还在阳台种了点薄荷。
  - id=mem_7ff4403882b2　来源=无来源　confidence=0.9
- `[FACT]` 用户平时更常待在家里。
  - id=mem_d61eed66849b　来源=无来源　confidence=0.85
- `[FACT]` 用户平时会看剧。
  - id=mem_b134f64158e2　来源=无来源　confidence=0.85

---

## 四、版本差异（辅助参考，**不作为结论**）

changelog 能可靠抓到的三类：**证据等级变化**（type_changed）、
**事实合并**（merged）、**真丢失**（removed）。
它的条目配对结果不可靠（原因见本文件开头说明），请只参考这三类。

- `v000__v001`：类型变化 0　合并 0　真丢失 0　被并入 0
- `v001__v002`：类型变化 0　合并 6　真丢失 11　被并入 0
- `v002__v003`：类型变化 0　合并 4　真丢失 3　被并入 1
