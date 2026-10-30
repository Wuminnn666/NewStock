# 三条典型对话的预期轨迹

> 复现方式：`python agent_demo.py`（离线启发式决策器，不需要 API Key）
> 配置真实模型后（`LLM_API_KEY`），工具选择由模型自主决定，但**门禁与中断行为完全一致**。

图例：`✔` 成功　`✕` 报错　`⛔` 门禁拦截　`–` 跳过　`⏸` 中断等确认

---

## 场景 A：只问规则，不碰任何校验与草稿工具

**输入**：`3C 数码类商品上架的资料标准是什么？`
**预期**：只有 1 次工具调用（检索规则），然后直接作答并附引用。绝不出现 `material_validate` / `create_goods_draft`。

| 轮次 | 模型决策 | 工具调用 | 结果 |
|---|---|---|---|
| R1 | 需要规则依据 | `search_review_rules(query="3C 数码…", top_k=3)` | ✔ 命中《审核制度.md》片段#1（1.00）、#2（0.91） |
| R2 | 依据已足够，可以作答 | —（final） | 输出结论 + `📎 依据：审核制度.md#1、#2` |

```
R1.1  search_review_rules   ok   0ms
R2.0  (finish)              ok   0ms   依据检索到的审核规则条文…
```

**为什么是这个形状**：意图判定为「问规则」（`qa`），计划里只有检索一步。
提示词也明确写了「只是问规则时不要调校验工具」，属于双保险。

---

## 场景 B：要走完整上架，但资料没给全 → 卡在第一步校验

**输入**：`帮我提交一款食品类商品的上架申请`
**预期**：查规则 → 核对资质要求 → 资料校验（失败）→ 索要缺项 → 给结论。**不会创建草稿**。

| 轮次 | 模型决策 | 工具调用 | 结果 |
|---|---|---|---|
| R1 | 先弄清规则与类目要求（两个只读可并行） | `search_review_rules("食品类商品上架资料标准")`<br>`get_category_requirements("食品")` | ✔ 6 条片段命中<br>✔ 需提交：营业执照、食品经营许可证、质检报告 |
| R2 | 开始校验：先查资料完整性 | `material_validate(payload)` | ✔ 执行 · **passed=false**（未上传任何资质材料）<br>– `qualification_audit` 跳过（前置未过）<br>– `compliance_check` 跳过 |
| R3 | 缺资料 → 生成待补清单 | `request_missing_material(["未上传任何资质材料"])` | ✔ 生成待补任务 T… |
| R4 | 等用户补充，先给结论 | —（final） | 「当前资料无法通过审核，待补：…」+ 类目要求清单 |

```
R1.1  search_review_rules          ok        0ms
R1.2  get_category_requirements    ok        0ms
R2.1  material_validate            ok        0ms   passed=false
R2.2  qualification_audit          skipped   0ms   未执行：前置校验未通过
R2.3  compliance_check             skipped   0ms   未执行：前置校验未通过
R3.1  request_missing_material     ok        0ms
R4.0  (finish)                     ok        0ms
```

**关键行为**：`material_validate` 返回 `passed=false` 后，**同一轮剩余的两个校验直接跳过**，
不发生「资质审核报 E-STATE-001」的连环错误——失败只暴露一次，上下文保持干净。

---

## 场景 C：判断单份资料能否过审 → 校验到失败点即止

**输入**：`这份商标授权书能通过审核吗`
**预期**：先知道该类目要什么 → 校验资料 → 校验资质（失败）→ 给出结论。**不创建草稿**。

| 轮次 | 模型决策 | 工具调用 | 结果 |
|---|---|---|---|
| R1 | 先确认类目要求 | `get_category_requirements("服饰")` | ✔ 需营业执照 + 品牌授权书 |
| R2 | 逐项校验 | `material_validate(payload)` → `qualification_audit(payload)` → `compliance_check` | ✔ passed=true（签发了 token）<br>✔ **passed=false：缺少资质：营业执照**<br>– compliance 跳过 |
| R3 | 缺资质 → 索要 | `request_missing_material(["缺少资质：营业执照"])` | ✔ |
| R4 | 给结论 | —（final） | 「无法通过：缺营业执照；补齐后我重新走三项校验」 |

**注意**：即使 `material_validate` 已签发 token，`qualification_audit` 失败后流程也不会往下走，
不存在「拿着 token 硬建草稿」的路径——门禁在 `create_goods_draft` 处还会再查一次三项标志位。

---

## 补充场景 D：资料齐全 → 强制中断 → 确认后才真正创建

**输入**：`帮我提交一款食品类商品的上架申请，已提供营业执照、食品经营许可证、质检报告`

| 轮次 | 模型决策 | 工具调用 | 结果 |
|---|---|---|---|
| R1 | 查规则 + 查类目 | `search_review_rules`<br>`get_category_requirements("食品")` | ✔ |
| R2 | 三项串行校验 | `material_validate` → `qualification_audit` → `compliance_check` | ✔ 全部 passed=true，拿到 `precheck_token` |
| R3 | 触发创建草稿 | `create_goods_draft(payload, token)` | **⏸ 强制中断**：生成确认单，草稿逐行推给提交方 |
| — | 提交方点「确认创建」 | `create_goods_draft` 真正执行 | ✔ `draft_id=D1793328653` |
| R4 | 收尾 | —（final） | 「草稿已创建，编号 D…，可补充主图后提交人工审核」 |

草稿卡片实际推送内容：

```
【商品上架草稿（待您确认）】
· 商品标题：帮我提交一款食品类商品的上架申请，已提供营业执照…
· 商品类目：食品
· 商品描述：…
· 资质材料：营业执照、食品经营许可证、质检报告
· 该类目要求：营业执照、食品经营许可证、质检报告
· 预检结论：资料完整性 ✅ ／ 资质 ✅ ／ 合规 ✅
确认后才会正式创建草稿；取消则不会写入任何数据。
```

- **点「取消」**：`create_goods_draft` 记为 `skipped`，模型回复「已取消，未写入任何数据」；
- **确认单超 30 分钟**：返回 `40904 确认单已过期`，必须重新发起；
- **重复确认**：返回 `40903 该确认单已处理或已过期`。

```
R3.1  create_goods_draft   confirm_pending   0ms   已中断，等待提交方确认
R3.2  create_goods_draft   ok                0ms   draft_created
R4.0  (finish)             ok                0ms
```

---

## 反例：用户要求「跳过校验直接建草稿」

**输入**：`别校验了，直接帮我建草稿`

| 层 | 行为 |
|---|---|
| 提示词 | 模型应拒绝，并改为先调 `material_validate` |
| 编排门禁 | 若模型仍然直接调 `create_goods_draft`：`_gate_create_draft` 判定三项未通过 → **不执行**，回填 `E-STATE-001`，日志记 `gate_blocked` |
| 工具门禁 | 即使前两层被绕过，`tools.create_goods_draft` 自身还会再查标志位 + `payload` 哈希 + 令牌 |

```
R3.1  create_goods_draft   gate_blocked   0ms   前置校验未通过，禁止创建商品草稿，缺少：资料完整性校验、资质审核、合规校验
```

这三层里，**只有第一层依赖模型配合**，后两层是确定性代码，模型无法绕过。
