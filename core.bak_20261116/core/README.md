# Jev 打标方案使用说明

> 最后更新：2026-11-16，基于 100 条抽样实测（B 方案，tau=0.6）

## 文件结构

```
core/
├── schemes.py       # ⭐ 所有方案的统一入口（7种方案）
├── jev_client.py    # API 客户端（支持 --platform 多平台配置）
├── jev_eval.py      # 评测模块（准确率/召回率、细分表、汇总表、报告）
├── jev_rules.py     # ⭐ 共享规则（粗筛/问题措辞/token 估算/证据回搜/情绪派生）
├── platforms.json   # 平台配置文件（多平台 API key）
├── jev_prompts.py   # 兼容入口（产物无 scheme 字段，日常请用 schemes.py）
└── README.md        # 本文件
```

> **规则只写一处**：粗筛 `screen_label`、问题措辞 `q_instruction`、token 估算 `est_tokens`
> 全部在 `jev_rules.py`，`schemes.py` 与 `jev_prompts.py` 都从这里 import。
> 2026-11-16 之前两个文件的粗筛和措辞各写了一份且已经跑偏（一个查排除词一个不查、同义词给 3 个 vs 6 个）。

---

## 一、100 条实测结论（先看这个）

用当前数据跑出来的真实数字（`tau=0.6`，对齐 LLM 初稿金标准）：

| 指标 | 值 |
| --- | --- |
| 金标准标签数 | 1,963 |
| 预测标签数 | 1,368 |
| 命中 TP / 标错 FP / 漏标 FN | 677 / 691 / 1,286 |
| **准确率 P** | **49.5%** |
| **召回率 R** | **34.5%** |
| F1 | 0.406 |

两个问题要分开看，否则会归错原因：

| 问题 | 规模 | 归属 |
| --- | --- | --- |
| 准确率只有 49.5% | 691 个假阳性 | Jev 在被问到的标签上判错，或金标准口径过宽 |
| 召回率只有 34.5% | 1,286 个漏标，其中 **1,234 个(96%)粗筛根本没送问** | 方案前置筛选的天花板，**不是 Jev 判错** |

**B 方案的召回天花板是 65.5%**：金标准 1,963 个标签里，只有 1,286 个能在原文找到词库关键词的字面形式；
剩下 677 个是同义改写/隐含表达，纯字面粗筛永远捞不到。**所以 B 方案在选型上不可能达到 95%。**

> 结论用法：B 方案可以定位成"低成本预筛层"，但对外汇报准确率必须写 49.5%（不是旧报告的 63.2%）。

---

## 二、方案对比（S001 实测规模）

| 方案 | 问题数/帖 | est_tokens/帖 | 相对A2 | 实现状态 | 适用场景 |
|------|----------|--------------|--------|---------|---------|
| **A2** | 638 | 48,449 | 基准 | ✅ 可用 | **效果基准**：测试阶段 |
| A1 | 638 | 26,059 | 节省46% | ✅ 可用 | 测 Jev 裸语义上限 |
| **B** | 2~86（均 22.6，100 条实测） | 均 2,204 | **节省~95%** | ✅ 可用 | 低成本预筛；**召回天花板 65.5%** |
| **Hybrid** | 75 | 6,391 | **节省87%** | ✅ 可用 | 平衡成本与召回 |
| **RAG** | 41 | 3,834 | **节省92%** | ✅ 可用 | 依赖词库质量 |
| H | 679 (41+638) | 51,057 | 反而多5% | ⚠️ 需优化 | 生产阶段（待实现门控过滤） |
| Speculative | 1276 (638×2) | 74,508 | 反而多54% | ⚠️ 需优化 | 纯Jev方案（待实现边界过滤） |

**说明：**
- H 方案当前预生成所有标签问题，需在 eval 中根据 gate_cat 结果后过滤
- Speculative 方案两轮都问全量，需在 eval 中只取边界标签的 pass2 结果
- 这两个方案的实际节省要等过滤逻辑实现才能体现

---

## 三、2026-11-16 修订：粗筛与措辞

### 3.1 排除词全文判死 → 窗口判否

旧代码（`schemes.py`）：

```python
for ex in L['excls']:
    if ex in text:      # 全文任意位置出现就判死
        return False
```

而 362 个标签共用同一套通用否定词：`不`、`没`、`没有`、`没用`、`并非`、`不算`、`毫无`、
`根本不`、`一点不`、`没效果`、`没啥用`、`毫无作用`。中文帖子几乎都含"不"字，于是候选被大面积误杀。

实测影响（100 条，截断后文本）：

| 口径 | 每帖候选 |
| --- | --- |
| `full` 全文判否（旧） | 21.1 |
| `window` 命中点 ±near 判否（新，默认） | 34.4 |
| `off` 不看排除词 | 36.4 |

新口径多救回 **1,335 个候选**（约 13.3 个/帖）。金标准平均每帖 19.6 个标签——
旧口径每帖只送 22.6 个候选，候选池几乎等于答案池，召回 34.5% 是必然结果。

现行实现（`jev_rules.screen_label`）：只在命中词前后 `near`（词库 near 列，默认 15 字）窗口内判否；
同一关键词出现多次时逐位置检查，任一处未被否定即通过。

切换方式：`--excls-scope window|full|off`（默认 window，`full` 仅供复现旧行为做对照）。

### 3.2 粗筛改用"送进模型的那段文本"

旧版粗筛读 `full_text` 全文，但送模型的是 `full_text[:max_chars]`（默认 2000 字）。
100 条里有 19 篇超过 2000 字（最长 51,472 字），共截掉 68,650 字。

后果：粗筛在第 4000 字匹配到"烟酰胺"→ 模型只看得到前 2000 字 → 判 0 → 漏标。
**这类漏标是自己造成的，还会白付 token。** 现在粗筛一律用截断后的文本。

### 3.3 问题措辞 v1 → v2

| 版本 | 措辞 | 特点 |
| --- | --- | --- |
| v1（旧） | `这篇社媒内容是否提到了「X」（类别）？常见说法如a、b、c，同义改写也算。若语境是不、没、没有等否定含义则不算。` | 确认式提问，容易往"有"偏；还把 3 个无区分度的通用否定词写进正文 |
| **v2（默认）** | `原文是否明确表达「X」（类别）？常见说法如a、b、c…（最多8个）。只有原文有对应的明确表述才算；只是相关话题、常识推测、隐含暗示都不算。若原文是否定或排除含义，也不算。` | 判据式；同义词给到 8 个；通用否定词折叠成一句 |

- 代价：v2 每问约多 25 字（B 方案 34 问/帖 ≈ 多 850 tok/帖）
- 旧措辞保留做 A/B 对照：`--qstyle v1`
- **注意**：v2 尚未做正式 A/B 验证，先用 10~20 条样本对比再全量。

### 3.4 token 估算与预算

- `est_tokens` 乘校准系数 `×1.10`（实测 220,442 估算 vs 243,480 真实 = ×1.104，单帖最多低估 2 倍）
- `--budget` 现在指**单请求总预算（含 state）**。旧版只约束 questions，
  每个 chunk 又要重复携带同一份 state，贴近 64k 上限时会超。

---

## 四、快速开始

### 第 1 步：生成请求体

```powershell
# B 方案全量 100 条
python core/schemes.py --scheme B

# 只跑 20 条做对比验证（推荐先这样，省额度）
python core/schemes.py --scheme B --samples S001,S002,S003,S004,S005,S006,S007,S008,S009,S010,S011,S012,S013,S014,S015,S016,S017,S018,S019,S020

# A2 方案（638问，效果基准；注意额度，见文末）
python core/schemes.py --scheme A2
```

**指定样本的两种方式：**

```powershell
# 方式1: 从默认清单 data\抽样清单.csv 中筛选
python core/schemes.py --scheme A2 --samples S001,S002,S003

# 方式2: 指定自定义清单文件
python core/schemes.py --scheme A2 --list data\我的清单.csv
```

### 第 2 步：配置 API Key

编辑 `core/platforms.json`，填入你的 API key：

```json
{
  "platforms": {
    "official": {
      "name": "官方 TypeSafe",
      "base_url": "https://api.typesafe.ai",
      "api_key": "tsf_你的官方key",
      "model": "jev-latest"
    },
    "edgeone": {
      "name": "腾讯云 EdgeOne",
      "base_url": "https://ai-gateway.edgeone.link",
      "api_key": "你的edgeone key",
      "model": "@makers/jev"
    }
  },
  "default": "edgeone"
}
```

### 第 3 步：调用 API

```powershell
# 先检查请求规模（不发网络、不耗额度）
python core/jev_client.py --dir out/payloads --all --scheme B --dry-run

# 冒烟：单条
python core/jev_client.py --payload out/payloads/S011__B__A2.jsonl

# 批量：走 --all 时务必带 --scheme，否则会把其它方案的 payload 一起发出去
python core/jev_client.py --dir out/payloads --all --scheme B
```

> `--dir` 和 `--payload` 二选一，`--all` 只是"处理目录内全部"的开关，**不会自己指定目录**。
> 只写 `--all --scheme B` 会报 `one of the arguments --payload --dir is required`。
>
> `--dir --samples S001 --scheme B` 会静默跑 0 条（它拼的是旧残留 `S001.jsonl`，无 scheme 字段被全部过滤）。单样本请用 `--payload`。

**跑完自动出表**（默认，`--no-csv` 可关闭）：

| 文件 | 内容 | 用途 |
| --- | --- | --- |
| `out/打标明细.csv` | **只含命中的标签**，一行一个（样本 × 标签），带类别/TOPIC ANGLE/概率/问题原文 | 交付、业务 review |
| `out/打标概览.csv` | 一行一个请求：http/耗时/token/答案数/命中数/**命中 TOPIC ANGLE 摘要** | 跑批体检 |
| `out/打标明细_全量.csv` | 含**未命中**的全部问答，多 6 列（是否命中/灰区/HTTP/耗时/两个 token） | 评测算召回率、灰区复核 |

> **命中阈值只有一个来源**：`tools/raw_to_csv.py` 里的 `DEFAULT_THRESHOLD`（当前 **0.6**）。
> `jev_client.py --threshold` 与 `jev_eval.py --tau` 不填都用它，三个入口同一个口径。
> 要临时换口径就显式传参，别去改多处。

> `灰区` = 概率落在 `[0.6×threshold, threshold)` 的标签，默认阈值 0.6 下为 0.36~0.60。

### 第 4 步：评测准确率

```powershell
# 正确用法：--scheme 是方案(B/A1/...)，--mode 是问法粒度(A1/A2)，两个维度不能混
python core/jev_eval.py --scheme B --mode A2

# 换阈值（默认 0.6，与打标明细同口径）
python core/jev_eval.py --scheme B --mode A2 --tau 0.5
```

输出：

| 文件 | 内容 |
| --- | --- |
| `out/评测报告_B_A2.md` | 准确率/召回率/F1、分维度 P/R/F1、FP/FN 榜单、漏标结构 |
| `out/打标结果细分-Jev_B_A2.csv` | 17 列预测细分表 |
| `out/打标结果汇总-Jev_B_A2.csv` | 12 列预测汇总表 |

报告里的关键口径：

- **P/R/F1 按 (样本号, 标签) 唯一对计**，这是唯一能自洽计算的口径
- 金标准 2,486 行 → 去重后 1,963 个标签（同一标签在不同证据片段会拆成多行）
- `unit 覆盖率 47.8%` 只做辅助参考，**不与 P 组 F1**（旧版把 unit 级 TP 和标签级 FP 混算，
  准确率被虚高成 63.2%）
- FN 里会标 `[未送问]`，直接区分"粗筛没送"和"送了没中"

---

## 五、schemes.py 完整参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| --scheme | **必填**，方案（A1/A2/B/H/RAG/Hybrid/Speculative） | — |
| --samples | 逗号分隔样本号，从清单中筛选 | 空=全部 |
| --list | 抽样清单 CSV 路径 | data\抽样清单.csv |
| --max-chars | 文本截断字符数（同时也是粗筛范围） | 2000 |
| --budget | **单请求总预算（含 state）** | 55000 |
| --model | Jev 模型版本 | jev-latest |
| **--qstyle** | 问题措辞：`v2` 判据式 / `v1` 旧确认式 | **v2** |
| **--excls-scope** | B 方案排除词判定：`window` 窗口 / `full` 全文(旧) / `off` 不看 | **window** |
| --top-k | RAG 取 Top-K | 50 |
| --cat-tau / --cat-k | H 方案类别门控阈值 / 保底数 | 0.15 / 12 |
| --lo / --hi | Speculative 边界区间 | 0.2 / 0.8 |

### 清单 CSV 格式

```csv
样本号,平台,CSV记录序号,入样原因
S001,小红书,5913,默认抽样清单
S002,微博,204,默认抽样清单
```

> CSV记录序号 必须对应 `data_export_clean_v3.csv` 中的 `csv_row_no`。

---

## 六、jev_client.py 完整参数说明

### 输入模式（二选一，必填）

| 参数 | 说明 |
|------|------|
| --payload | 单个 jsonl 文件路径 |
| --dir | payload 目录（配合 --samples 或 --all） |

### 样本选择

| 参数 | 说明 |
|------|------|
| --samples | 逗号分隔样本号（--dir 模式） |
| --all | 处理目录内全部 jsonl |

### 平台配置

| 参数 | 说明 | 默认值 |
|------|------|--------|
| --platform | 从 platforms.json 加载平台配置 | 空=读 default |
| --base / --key / --model | 直连覆盖（优先级最高） | — |

### 请求控制

| 参数 | 说明 | 默认值 |
|------|------|--------|
| --scheme | 按文件名过滤方案 | 空=不过滤 |
| --sleep / --retry / --timeout | 间隔 / 重试 / 超时 | 0.2 / 3 / 120 |
| --threshold | 导出用命中阈值 | 空=读 DEFAULT_THRESHOLD(0.6) |
| --top-n | 概览表 TOPIC ANGLE 展示个数 | 8 |
| --no-csv | 跑完不自动导出 CSV | 关 |
| --long-context | 启用 262k 长上下文 | 关 |
| --dry-run | 只打印请求规模，不发请求 | 关 |

**参数优先级**：命令行 > 平台配置 > 环境变量 > 默认值

---

## 七、jev_eval.py 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| **--scheme** | 方案名，匹配 `{sample}__{scheme}__{mode}.jsonl` | 空=全部 |
| **--mode** | 问法粒度 A1 / A2 | 空=全部 |
| --tau | 命中阈值 | 空=读 DEFAULT_THRESHOLD(0.6) |
| --tau-lo / --tau-hi | 灰区上下限 | tau×0.6 / tau |
| --api-tag | API 来源标记 | 空=自动识别 |

> 旧版只有 `--mode`，而文件名是 `{sample}__{scheme}__{mode}.jsonl`：
> 传 `--mode B` 永远匹配不到（0 条），传 `--mode A2` 又把 B 方案的数据读进来。现已解耦。

---

## 八、各方案说明

### A1：纯语义全量
每个标签一个 Noul 问题，只给标签名。用来测 Jev 裸语义上限。638 问/帖。
```powershell
python core/schemes.py --scheme A1 --samples S001,S002
```

### A2：全量+词库提示（效果基准）
638 问/帖，带词库常见说法。单帖约 48k tok，EdgeOne 免费额度只够 10 条。
```powershell
python core/schemes.py --scheme A2 --samples S001,S002
```

### B：词库粗筛后 Jev 验证
先用词库做字面粗筛，只对候选标签调 Jev。**实测召回天花板 65.5%**，
因为 677/1963 个金标准标签在原文里没有词库关键词的字面形式。
```powershell
python core/schemes.py --scheme B                    # 默认 window + v2
python core/schemes.py --scheme B --excls-scope full --qstyle v1   # 复现旧口径做对照
```

### H：层级门控 / RAG：Top-K 检索 / Hybrid：正则+语义 / Speculative：两轮 Jev
这四个方案当前只生成了请求体，**门控过滤/边界过滤逻辑还没在 eval 里实现**，
所以实测 token 反而比 A2 多。参数见第五节。

---

## 九、输出文件

| 来源 | 文件 |
| --- | --- |
| schemes.py | `out/payloads/{样本}__{方案}__{模式}.jsonl`、`{样本}.text.txt`、`{样本}.meta.json` |
| jev_client.py | `out/raw/{样本}__{方案}__{模式}.jsonl` |
| tools/raw_to_csv.py | `out/打标明细.csv`、`out/打标概览.csv`、`out/打标明细_全量.csv` |
| jev_eval.py | `out/打标结果细分-Jev_{方案}_{模式}.csv`、`...汇总...csv`、`out/评测报告_{方案}_{模式}.md` |

---

## 十、当前推荐流程

```powershell
# 1. 生成请求体（B 方案，100 条）
python core/schemes.py --scheme B

# 2. 检查规模（0 成本）
python core/jev_client.py --dir out/payloads --all --scheme B --dry-run

# 3. 正式调用（注意：新粗筛后候选变多，100 条约 35~40 万 tok）
python core/jev_client.py --dir out/payloads --all --scheme B

# 4. 评测（跑完已自动出三份 CSV，这一步算准确率）
python core/jev_eval.py --scheme B --mode A2
```

### 额度提醒（EdgeOne 免费 50 万/月）

| 动作 | 估算 token | 说明 |
| --- | --- | --- |
| 当前已用 | ~24.9 万 | 上一轮 100 条 B 方案 |
| 旧口径重跑 100 条 | 24.3 万 | 已跑过，不用重复 |
| **新口径重跑 100 条** | **~35~40 万** | 候选从 22.6/帖 涨到 34.4/帖 + v2 措辞变长 |
| 新口径跑 20 条 | ~7~8 万 | **推荐先这样验证** |
| A2 全量单帖 | 4.8 万 | 只够 10 条 |

> ⚠️ 剩余额度约 25 万，**不够新口径全量重跑**。建议先用 20 条对比
> `--excls-scope full --qstyle v1`（旧）vs `--excls-scope window --qstyle v2`（新），
> 看候选池和准确率的变化方向，再决定是否全量。

---

## 十一、注意事项

1. **API Key 管理**：编辑 `core/platforms.json`，用 `--platform` 切换
2. **先 dry-run**：调用前用 `--dry-run` 检查规模，避免意外消耗
3. **`--all` 必须带 `--scheme`**，否则会把其它方案的 payload 一起发出去
4. **重跑会追加写** `out/raw/*.jsonl`（`open(...,'a')`）。CSV 不会翻倍（导出时按
   样本+方案+模式+分片去重取最后一条），但 jsonl 会积累多份。大改后重跑前建议先清空 `out/raw`
5. **`tools/raw_to_csv.py` 是 0 成本重导**：换阈值、换 TOP N 都别重跑 API
6. **B 方案不能达到 95%**：字面粗筛天花板 65.5%，这是方案结构决定的
