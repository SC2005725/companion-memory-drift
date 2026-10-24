# 贡献指南

感谢你有兴趣。这个项目首先是一个**实验**，所以比"加功能"更看重"改动可解释"。

## 环境

```bash
python -m venv .venv
.venv/Scripts/activate      # Windows
pip install -r requirements.txt
cp .env.example .env        # 填入自己的 API Key（.env 已被 gitignore）
```

## 动手前先读

- [`docs/project-spec.md`](docs/project-spec.md) —— 项目定位与硬约束
- [`README.md`](README.md) —— 模块地图与已知限制

## 五条硬约束（改动若违反会被请回）

1. **记忆版本只追加、永不覆盖** —— 每个版本带 SHA-256，历史必须可追溯。
2. **`personality/personality.json` 程序不得修改** —— 它有运行期 sha256 守护，
   改了会中止会话。这是为了让"人格漂移"与"关系变化"可分别归因。
3. **不引入向量库 / 新框架 / 新服务** —— 检索刻意只用关键词 + 2-gram。
4. **所有模型调用必须经 `call_model()`** —— 否则账本对不上。
5. **确定性优先** —— 分类、关系、用户模型判定都用可复现的规则，
   不用模型判定。理由：必须能回答"为什么这条被这样处理"。

## 提交前跑一遍

离线测试，**不需要 API Key、不花钱**：

```bash
python src/selftest_memory.py
python src/selftest_companion.py
python src/selftest_persona_state.py
python src/selftest_merge.py
python src/selftest_tools.py
python src/dryrun.py
```

> `src/selftest.py` 在正式目录会写真实数据库，需要 `--allow-real`。
> **请不要在含有真实数据的目录里跑它。**

## 提交信息

用祈使句，说明**为什么**而不只是**做了什么**：

```
修复关系快照从不落盘：save_snapshot 只在 CLI 里被调用

chat.send_once 从未调用它，导致关系状态推进到「信任」而磁盘上
永远停在初识那一版。注入走实时派生所以行为正确，但关系演化无历史可查。
```

## Pull Request

- 一个 PR 只做一件事
- 说明**改动前的实际行为**与**改动后的实际行为**，不要写"应该会"
- 如果改了行为，附上可复现的命令与输出
- 不要提交 `conversation/`、`memory/`、`.env` 等运行期数据

## 不接受的改动

- 为了"更智能"而把确定性规则换成模型判定
- 让模型自动改写核心人格 / 价值观 / 边界
- 新增每次对话都要额外付费的调用
- 在没有反例证据的情况下扩大设定
