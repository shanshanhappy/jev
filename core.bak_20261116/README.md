# Jev 打标方案使用说明

> 最后更新：2026-11-15，基于 S001 样本实测数据

## 文件结构

`
core/
├── schemes.py       # ⭐ 所有方案的统一入口（7种方案）
├── jev_client.py    # API 客户端（支持 --platform 多平台配置）
├── jev_eval.py      # 评测模块（生成细分表、汇总表、报告）
├── jev_rules.py     # 共享规则（标签反查、证据回搜、情绪派生）
├── platforms.json   # 平台配置文件（多平台 API key）
├── jev_prompts.py   # 原有实现（已整合到 schemes.py，保留兼容）
└── README.md        # 本文件
`

---

## 方案对比（S001 实测数据）

| 方案 | 问题数/帖 | est_tokens/帖 | 相对A2 | 实现状态 | 适用场景 |
|------|----------|--------------|--------|---------|---------|
| **A2** | 638 | 48,449 | 基准 | ✅ 可用 | **当前推荐**：效果基准、测试阶段 |
| A1 | 638 | 26,059 | 节省46% | ✅ 可用 | 测量 Jev 裸语义上限 |
| **B** | 2~86（均 22.6，100 条实测） | 均 2,204 | **节省~95%** | ✅ 可用 | 预算紧张、大批量数据 |
| **Hybrid** | 75 | 6,391 | **节省87%** | ✅ 可用 | 平衡成本与召回 |
| **RAG** | 41 | 3,834 | **节省92%** | ✅ 可用 | 依赖词库质量 |
| H | 679 (41+638) | 51,057 | 反而多5% | ⚠️ 需优化 | 生产阶段（待实现门控过滤） |
| Speculative | 1276 (638×2) | 74,508 | 反而多54% | ⚠️ 需优化 | 纯Jev方案（待实现边界过滤） |

**说明：**
- H 方案当前预生成所有标签问题，需在 jev_eval.py 中根据 gate_cat 结果后过滤
- Speculative 方案两轮都问全量，需在 jev_eval.py 中只取边界标签的 pass2 结果
- 这两个方案的**实际节省**要等 eval 实现后过滤逻辑才能体现

---

## 快速开始

### 第 1 步：生成请求体

`powershell
# B 方案全量 100 条（默认清单全部样本，每样本一个问题数不等的请求）
python core/schemes.py --scheme B

# A2 方案（638问，效果基准；注意额度，见文末"当前推荐流程"）
python core/schemes.py --scheme A2
`

**指定样本的两种方式：**

`powershell
# 方式1: 从默认清单 data\抽样清单.csv 中筛选
python core/schemes.py --scheme A2 --samples S001,S002,S003

# 方式2: 指定自定义清单文件
python core/schemes.py --scheme A2 --list data\我的清单.csv

# 不指定参数：使用默认清单的全部样本
python core/schemes.py --scheme A2
`

### 第 2 步：配置 API Key

编辑 core/platforms.json，填入你的 API key：

`json
{
  "platforms": {
    "official": {
      "name": "官方 TypeSafe",
      "base_url": "https://api.typesafe.ai",
      "api_key": "tsf_你的官方key",
      "model": "jev-latest",
      "description": "官方端点，支持 jev-1.14.0"
    },
    "edgeone": {
      "name": "腾讯云 EdgeOne",
      "base_url": "https://ai-gateway.edgeone.link",
      "api_key": "你的edgeone key",
      "model": "@makers/jev",
      "description": "腾讯云免费额度 50 万 token/月，实测返回 jev-1.14.0"
    }
  },
  "default": "edgeone"
}
`

### 第 3 步：调用 API

```powershell
# 先检查请求规模（不发网络、不耗额度）
python core/jev_client.py --payload out/payloads/S001__B__A2.jsonl --dry-run

# 冒烟：单样本单方案（推荐先跑 B，1804 tok/条，免费额度够跑 250+ 次）
python core/jev_client.py --payload out/payloads/S001__B__A2.jsonl

# 批量：走 --all 时务必带 --scheme，否则会把 T001 测试批和旧残留一起发出去
python core/jev_client.py --dir out/payloads --all --scheme B
```

> ⚠️ `--dir --samples S001 --scheme B` 会静默跑 0 条（它拼的是旧残留 `S001.jsonl`，无 scheme 字段被全部过滤）。单样本请用 `--payload`。

**跑完自动出表**（默认，`--no-csv` 可关闭；`--threshold 0.7` 可改命中阈值）：

| 文件 | 内容 | 用途 |
| --- | --- | --- |
| `out/打标明细.csv` | **只含命中的标签**，一行一个（样本 × 标签），带类别/TOPIC ANGLE/概率/问题原文 | 交付、业务 review |
| `out/打标概览.csv` | 一行一个请求：http/耗时/token/答案数/命中数/**命中 TOPIC ANGLE 摘要** | 跑批体检 |
| `out/打标明细_全量.csv` | 含**未命中**的全部问答，多 6 列（是否命中/灰区/HTTP/耗时/两个 token） | 评测算召回率、灰区复核 |

> `灰区` = 概率落在 `[0.6×threshold, threshold)` 的标签，判定不稳、建议人工看。默认阈值 0.6 下为 0.36~0.60。

> **命中阈值只有一个来源**：`tools/raw_to_csv.py` 里的 `DEFAULT_THRESHOLD`（当前 **0.6**）。`jev_client.py --threshold` 不填就用它，两个入口不会再出现「同一批数据、两个阈值、两个行数」的情况。要临时换口径就显式传 `--threshold 0.5`，别去改两个地方。

> `TOPIC ANGLE(前8)` = 该帖命中标签里**按概率从高到低取前 8 个**，用「、」连接；同名标签只保留最高概率那次。想改个数用 `--top-n 5`（`jev_client.py` 与 `tools/raw_to_csv.py` 都支持）。

也可以单独跑 `python tools/raw_to_csv.py`。

**跑完打印本批报告**（只统计本次运行，不含 `out/raw` 历史记录）：

```
====================================================
本批运行报告
----------------------------------------------------
请求数        100   成功 100   失败 0
input_tokens  合计 243,480   均 2,435/条
output_tokens 合计 45,580
耗时          均 1.9s   最长 6.9s   总计 3.1 分钟
估算 vs 实测  估 220,442  实 243,480  偏差 +10%
标签命中      1,454 / 2,259 问 (64.4%)
每帖命中      最少 2   最多 51   均 14.5
折算成本      $0.0102 (≈¥0.07)  按 $0.042/M input
低命中(<5)    S052(2) S060(2) S004(3) ...
----------------------------------------------------
失败明细:                          ← 只有存在失败时才出现
  S037   http=429  {...}
====================================================
```

- `估算 vs 实测` 用 payload 里的 `est_tokens` 对比真实 `input_tokens`，用来校准预估公式
- `折算成本` 按官方 $0.042/M 估算，EdgeOne 免费额度内可以忽略
- `低命中(<5)` 多为正则粗筛候选太少导致的 B 方案结构性漏召，不是 Jev 判错，建议抽原文核对

### 第 4 步：评测结果

```powershell
python core/jev_eval.py --mode A2 --tau 0.5
```

---

## schemes.py 完整参数说明

### 必填参数

| 参数 | 说明 | 示例 |
|------|------|------|
| --scheme | **必填**，选择方案（A1/A2/B/H/RAG/Hybrid/Speculative） | --scheme A2 |

### 样本选择参数

| 参数 | 说明 | 默认值 | 示例 |
|------|------|--------|------|
| --samples | 逗号分隔样本号，从清单中筛选 | 空=全部 | --samples S001,S002 |
| --list | 指定抽样清单 CSV 文件路径 | data\抽样清单.csv | --list data\我的清单.csv |

### 通用参数

| 参数 | 说明 | 默认值 | 示例 |
|------|------|--------|------|
| --max-chars | 文本截断字符数 | 2000 | --max-chars 1500 |
| --budget | 单请求 token 预算上限 | 55000 | --budget 50000 |
| --model | Jev 模型版本 | jev-latest | --model jev-1.14.0 |

### 方案特定参数

| 参数 | 适用方案 | 说明 | 默认值 |
|------|---------|------|--------|
| --top-k | RAG | 取相似度最高的前 K 个标签 | 50 |
| --cat-tau | H | 类别门控概率阈值 | 0.15 |
| --cat-k | H | 类别 Top-K 保底数量 | 12 |
| --lo | Speculative | 边界概率下限 | 0.2 |
| --hi | Speculative | 边界概率上限 | 0.8 |

### 清单 CSV 格式

`csv
样本号,平台,CSV记录序号,入样原因
S001,小红书,5913,默认抽样清单
S002,微博,204,默认抽样清单
`

> **注意**：CSV记录序号 必须对应 data_export_clean_v3.csv 中的 csv_row_no。

---

## jev_client.py 完整参数说明

### 输入模式（二选一）

| 参数 | 说明 | 示例 |
|------|------|------|
| --payload | 单个 jsonl 文件路径 | --payload out/payloads/S001__A2__A2.jsonl |
| --dir | payload 目录（配合 --samples 或 --all） | --dir out/payloads |

### 样本选择

| 参数 | 说明 | 示例 |
|------|------|------|
| --samples | 逗号分隔样本号（--dir 模式必填） | --samples S001,S002 |
| --all | 处理目录内全部 jsonl | --all |

### 平台配置

| 参数 | 说明 | 默认值 | 示例 |
|------|------|--------|------|
| --platform | 从 platforms.json 加载平台配置 | 空 | --platform official |
| --base | API 基础 URL（优先级最高） | https://api.typesafe.ai | --base https://xxx.com |
| --key | API Key（优先级最高） | 从环境变量读取 | --key tsf_xxx |
| --model | 模型版本（优先级最高） | jev-latest | --model jev-1.14.0 |

### 请求控制

| 参数 | 说明 | 默认值 | 示例 |
|------|------|--------|------|
| --scheme | 过滤指定方案（B/H/RAG/Hybrid/Speculative） | 空=不过滤 | --scheme A2 |
| --sleep | 请求间隔秒数 | 0.2 | --sleep 0.5 |
| --retry | 失败重试次数 | 3 | --retry 5 |
| --timeout | 请求超时秒数 | 120 | --timeout 180 |

### 高级选项

| 参数 | 说明 |
|------|------|
| --long-context | 启用 262k 长上下文（加 TypeSafe-Danger-Zone-Long-Context: true 头） |
| --dry-run | 仅打印请求规模，不发送网络请求 |

### 参数优先级

`
命令行参数 (--base/--key/--model) > 平台配置 (--platform) > 环境变量 (*) > 默认值
`

---

## jev_eval.py 完整参数说明

| 参数 | 说明 | 默认值 | 示例 |
|------|------|--------|------|
| --mode | 评测模式（A1/A2/B，需与 payload 一致） | A2 | --mode A2 |
| --tau | 命中概率阈值（≥此值算命中） | 0.5 | --tau 0.6 |
| --tau-lo | 灰区下限 | 0.3 | --tau-lo 0.2 |
| --tau-hi | 灰区上限 | 0.7 | --tau-hi 0.8 |
| --api-tag | API 来源标记 | 空 | --api-tag official |

### 灰区说明

概率落在 [tau-lo, tau-hi] 区间的标签会被标记为"灰区"，记录在汇总表的 灰区待复核数 列中，用于人工复核。

---

## 各方案详细说明

### 方案 A1：纯语义全量

**思路**：每个标签一个 Noul 问题，不加词库提示，只问"是否提到「X」（类别）？"

**适用**：测量 Jev 裸语义能力上限

**成本**：约 ¥138/万条（节省46%）

`powershell
python core/schemes.py --scheme A1 --samples S001,S002
`

### 方案 A2：全量+词库提示（当前推荐）

**思路**：每个标签一个 Noul 问题，带词库命中说法和排除提示

**适用**：测试阶段，建立效果基准

**成本**：约 ¥259/万条（基准）

`powershell
python core/schemes.py --scheme A2 --samples S001,S002
`

### 方案 B：正则粗筛后 Jev 验证

**思路**：先用词库规则在本地匹配，只对命中的标签调 Jev

**优点**：成本极低（节省 96%）

**缺点**：纯语义标签（108个）天然漏掉

`powershell
python core/schemes.py --scheme B --samples S001,S002
`

### 方案 H：层级门控

**思路**：先问 41 个类别，再问候选标签

**当前状态**：⚠️ 预生成所有638个标签问题，需在 eval 中根据 gate_cat 结果后过滤

**优点**：理论上大幅降本（节省81%）

**缺点**：当前实现反而多5% token，需优化

`powershell
python core/schemes.py --scheme H --samples S001,S002 --cat-tau 0.15 --cat-k 12
`

**参数：**
- --cat-tau：类别门控阈值（默认 0.15），概率 ≥ 此值的类别进入下一轮
- --cat-k：类别 Top-K 保底（默认 12），即使概率低也保留前 K 个类别

### 方案 RAG：语义检索后 Jev 验证

**思路**：用词库关键词计算相似度，取 Top-K 标签调 Jev

**优点**：成本极低（节省 92%）

**缺点**：依赖词库质量，可能漏召回

`powershell
python core/schemes.py --scheme RAG --samples S001,S002 --top-k 50
`

**参数：**
- --top-k：取相似度最高的前 K 个标签（默认 50）

### 方案 Hybrid：正则 + 语义

**思路**：正则命中的直接用，语义标签调 Jev 验证

**优点**：召回可控，成本中等（节省 87%）

**缺点**：需要维护两套逻辑

`powershell
python core/schemes.py --scheme Hybrid --samples S001,S002
`

### 方案 Speculative：两轮 Jev

**思路**：第一遍用 A1 简短指令，第二遍只对边界标签用 A2 详细指令

**当前状态**：⚠️ 两轮都问全量638，需在 eval 中只取边界标签的 pass2 结果

**优点**：纯 Jev 方案

**缺点**：当前实现反而多54% token，需优化

`powershell
python core/schemes.py --scheme Speculative --samples S001,S002 --lo 0.2 --hi 0.8
`

**参数：**
- --lo：边界下限（默认 0.2）
- --hi：边界上限（默认 0.8）

---

## 输出文件

### schemes.py 生成

- out/payloads/{样本}__{方案}__{模式}.jsonl — 请求体
- out/payloads/{样本}.text.txt — 帖子文本（用于证据回搜）
- out/payloads/{样本}.meta.json — 样本元数据

### jev_client.py 生成

- out/raw/{样本}__{方案}__{模式}.jsonl — API 原始响应

### tools/raw_to_csv.py 生成（jev_client 跑完会自动调用）

- out/打标明细.csv — **仅命中**的标签，交付用（14 列）
- out/打标概览.csv — 每请求一行：http/耗时/token/命中数
- out/打标明细_全量.csv — 含未命中，评测与灰区复核用（20 列）

### jev_eval.py 生成

- out/打标结果细分-{方案}_{模式}.csv — 17 列预测细分表
- out/打标结果汇总-{方案}_{模式}.csv — 12 列预测汇总表
- out/评测报告_{方案}_{模式}.md — 评测报告

---

## 当前推荐流程（100 条抽样 · B 方案）

```powershell
# 1. 生成请求体：不传 --samples 即处理 data\抽样清单.csv 全部 100 条
#    产出 out\payloads\S001__B__A2.jsonl ~ S100__B__A2.jsonl（每样本一个文件）+ 各自的 text/meta
python core/schemes.py --scheme B

# 2. 检查规模（不发网络、不耗额度）
python core/jev_client.py --dir out/payloads --all --scheme B --dry-run

# 3. 正式调用（100 条 ≈ 22 万 tok，约 7~8 分钟，EdgeOne 免费额度 50 万/月够用）
python core/jev_client.py --dir out/payloads --all --scheme B

# 4. 出表（第 3 步跑完已自动导出，这一步只在需要改阈值时重跑）
python tools/raw_to_csv.py --threshold 0.5   # 想换口径才传，默认 0.6
```

**100 条实测规模（B 方案，2026-11-16 生成）**

| 项 | 值 |
| --- | --- |
| payload 文件 | 100 个（S001~S100，每样本 1 个） |
| 问题总数 | 2,259 |
| 每样本问题数 | min 2 / max 86 / 均 22.6 |
| 估算 token 合计 | 220,442（估得偏保守，实测约 ×1.09） |
| 单请求分块 | 全部为 1 块（远低于 --budget 55000） |

> ⚠️ **`--all` 必须带 `--scheme`**。不带的话会把 `out\payloads` 里其它方案（A1/A2/H/RAG/Hybrid/Speculative）的 payload 全部发出去。另外 `_test\` 下的 T001 测试批已移出该目录，不会被 `--all` 扫到。

> ⚠️ **换 A2 时额度不够**：A2 单帖 48,449 tok，100 条 = 4.85M tok，EdgeOne 免费 50 万/月只够 10 条。跑 A2 全量要么充官方端点（约 ¥1.5 / 100 条），要么先用 `--max-chars 800` 压文本。

**后续优化**：拿到 B 的基线结果后，再对比 Hybrid/RAG/A2 的召回率与成本。

---

## 注意事项

1. **API Key 管理**：编辑 core/platforms.json 配置多平台，用 --platform 切换
2. **先 dry-run**：调用前用 --dry-run 检查请求规模，避免意外消耗
3. **方案选择**：测试阶段用 A2（基准），生产阶段根据成本选 B/Hybrid/RAG
4. **平台切换**：jev_client.py 支持 --platform 参数切换 API 平台
5. **清单格式**：自定义清单的 CSV记录序号 必须对应 data_export_clean_v3.csv 中的 csv_row_no
6. **参数优先级**：命令行参数 > 平台配置 > 环境变量 > 默认值
7. **H 和 Speculative 方案**：当前实现未优化，实际节省需等 eval 后过滤逻辑

