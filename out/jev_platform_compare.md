# Jev 模型 API 平台对比（2026-11-15 二次核实）

## 一、平台清单（按"国内能不能零门槛拿到 key"排序）

| 平台 | 注册门槛 | 免费额度 | 端点 / 模型 | 状态 |
| --- | --- | --- | --- | --- |
| **Cloudflare Workers AI** | ✅ **仅邮箱**（QQ/163 均可），**不用手机号、不用信用卡、不用实名**，国内可直连 | ❌ **免费 Neurons 不覆盖 Jev**。10,000 Neurons/天仅限 `@cf/` 自家模型；`typesafe/jev` 从 AI Gateway 预付 credits 扣费，新账号余额为 0 → 实测返回 `HTTP 402 Insufficient balance (code 2021)` | `POST https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run`<br>model: `typesafe/jev` | 输入 $0.042/M（买 credits 另加 5% 手续费）；零数据留存 ✅；context 仅 32k |
| **腾讯云 EdgeOne Makers** | ✅ 腾讯云账号，免费版自动下发 | **50 万 token/月** | `https://ai-gateway.edgeone.link/v1/systemone`<br>model: `@makers/jev` | 官方文档 2026-11-12 更新，国内直连最稳 |
| **TypeSafe 官方** | ✅ 邮箱，5 分钟 | ❌ 新注册无赠费（9/27 起取消）。Playground 可免费试玩 | `https://api.typesafe.ai/v1/systemone`<br>model: `jev-latest` | 官方源头，$0.042/M；Playground 可用于对照真实响应结构 |
| **OpenRouter** | ✅ 邮箱 | ❌ 无免费层，需先充值 | `https://openrouter.ai/api/alpha/decisions`<br>model: `typesafe/jev-1.13` | $0.042/M，路径与原生协议不同 |
| ~~**Vercel AI Gateway**~~ | ❌ **必须绑国际信用卡**（`customer_verification_required`） | 声称 $5/月，但**不绑卡就是 $0** | 网关路由 `typesafe-ai/jev-1.14.0` | 🔴 **已排除**：免费层限流极狠，实测 131 问批次耗时 ≈1.5 小时；充值即失去每月免费额度 |
| ~~**OpenCode Zen**~~ | — | ~~`jev-1.13-free`~~ | — | ❌ 11-15 复核已下架（86 个模型中无 jev） |
| **jevmodel.org**（第三方中文站） | ✅ 免注册 | 1 万 token（≈60 次）；登录送 10 万 | `https://jevmodel.org/v1/systemone` | ⚠️ 站方自述与 TypeSafe 无关联，**正式业务数据不要发** |
| **jev-api.org**（第三方英文站） | ✅ 邮箱 | ❌ 无免费额度 | 自有 `POST /api/v1/decisions`（1~6 问）<br>兼容 `POST /v1/systemone`（1~256 问） | 按次计费，模型停在 jev-1.13 |
| Cloudflare AI Gateway | — | 无独立额度（BYOK） | — | 与 Workers AI 是两回事，别混 |

## 二、免费额度到底能跑多少条（按你实测 payload 算）

单帖 token 实测值：B = 1,804 / RAG = 3,834 / Hybrid = 6,391 / A1 = 26,059 / **A2 = 48,449** / H = 51,057

| 方案 | 单帖 token | EdgeOne 50 万 token/月 能跑 | Cloudflare 免费额度 |
| --- | --- | --- | --- |
| B | 1,804 | 277 条/月 ✅ | ❌ 不覆盖 |
| RAG | 3,834 | 130 条/月 ⚠️ | ❌ 不覆盖 |
| Hybrid | 6,391 | 78 条/月 ⚠️ | ❌ 不覆盖 |
| A2 | 48,449 | **10 条/月** ❌ | ❌ 不覆盖（且 32k context 也会超限） |
| H | 51,057 | 10 条/月 ❌ | ❌ 不覆盖 |

> **结论：目前唯一真免费的渠道只有腾讯云 EdgeOne（50 万 token/月），而且它只够跑 10 条 A2。Cloudflare 的免费 Neurons 不覆盖第三方模型，Jev 必须充值 AI Gateway credits（实测 402 / code 2021）。**
>
> ⚠️ Cloudflare 充值后仍可能不通：社区多例反馈，自动创建的 `default` gateway 的 `wholesale` 标志为 `false`，导致第三方模型持续返回 402。需手动新建 gateway，并在创建时把 Workers AI Billing 设为 **Unified Billing**。

## 三、所以真正该做的账

| 跑什么 | token 量 | 付费成本 | 结论 |
| --- | --- | --- | --- |
| 100 条 × A2 | 4.84M | **$0.20（≈¥1.5）** | 💡 **直接充值最划算** |
| 100 条 × B | 0.18M | $0.008（≈¥0.06） | EdgeOne 免费额度够（50 万/月） |
| 1 万条 × A2 | 484M | **$20.3（≈¥146）** | 正式测试量级 |

**100 条 A2 全量的真实成本是 2 毛美金。** 为了省这 ¥1.5 去折腾免费渠道的注册门槛、限流、日额度拆分，时间成本远超收益。建议：**免费额度只用来验证"链路通不通 + 返回结构对不对"，正式跑批直接充 $5（够跑 2500 条 A2）。**

## 四、成本压缩的关键杠杆

A2 单帖 48,449 tok 里，**638 个 Noul 问题的定义占了 96%**，state 只占约 200 tok。所以：

- 每个 Noul 问题精简 20 token → 单帖省 12,760 tok → **成本直降 26%**
- 改用 cached input（Cloudflare 侧 $0.00/M）：questions 模板固定不变，**理论上可再省 96% 的输入成本**（前提是走 Cloudflare 付费通道，免费层不覆盖第三方模型）
- 这两条优化建议在测试里单独跑一版对照，是方案四最有说服力的成本论据

## 五、Jev 关键技术限制（TypeSafe 官方文档）

| 项 | 数值 | 说明 |
| --- | --- | --- |
| 单请求 context | 64k tokens | `state` + 所有 questions 合计 |
| 单问题 context | 32k tokens | `state` + 单个 question |
| Long context | 262k tokens | 需 header `TypeSafe-Danger-Zone-Long-Context: true` |
| questions 数量 | **无上限** | 只受 context 限制，可一次性塞入全部维度 |
| 单个 Choice 选项数 | **最多 255 个** | 够覆盖单维度全部标签 |
| 计费 | 仅 input | $0.042/M tokens，**output 免费** |
| 模型版本 | `jev-1.14.0` / `jev-1.13.0` | 别名 `jev-latest` 指向 1.14.0 |
| Cloudflare 侧 context | **32,000 tokens** | 比官方 64k 低一半，A2 的 48k 在 Cloudflare 上会超限 ⚠️ |

## 六、调用协议要点

- 只支持 System One 原生协议 `POST /v1/systemone`（Cloudflare 走 `/ai/run`），**不能走** `/v1/chat/completions`
- 不支持流式输出（一次性结构化求值）
- `choice` 的 `criteria` 是 **map**（选项名 → 说明），不是数组
- 返回 `choice` + `probabilities`（全选项概率分布）+ `confidence`（0~1）
- Cloudflare 多包一层：body 需 `{"model": ..., "input": {state, questions}}`，响应取 `result`

## 七、官方注册赠费时间线

| 日期 | 事件 |
| --- | --- |
| 2026-09-20 | TypeSafe 全面开放，新用户注册送 $5（36kr 等报道） |
| 2026-09-25 | Vercel 侧 Jev 的免费促销结束，转为 $0.042/M |
| 2026-09-27 | TypeSafe 重新开放注册，**新账号不再送免费额度** |
| 限流参考 | 官方 100K tokens/s、80 req/s，动态调整 |
