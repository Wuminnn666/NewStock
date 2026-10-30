# Agent 编排层设计说明

> 对应代码：`app/agent/`（`harness.py` 执行循环、`decider.py` 决策器、`prompts.py` 编排提示词、`trace.py` 日志）
> 数据表：`agent_runs` / `agent_steps` / `agent_confirmations`

## 1. 框架选型：为什么用「自建 Harness」而不是 LangChain / LangGraph / AutoGen

| 方案 | 优点 | 为什么这次不选 |
|---|---|---|
| LangChain AgentExecutor | 生态全、上手快 | 抽象层厚，出问题时错误栈埋在多层 wrapper 里；工具门禁要靠 callback 打补丁，不好在「工具真正执行前」硬拦截 |
| LangGraph | 显式状态机、可中断 | 强（需要 checkpoint 存储），本项目的中断只有「创建草稿前」一个点，引入它属于杀鸡用牛刀 |
| AutoGen | 多 Agent 对话 | 我们是「单 Agent + 确定性门禁」，不需要多角色协商，反而增加不可控性 |
| **自建 Harness（本方案）** | 循环体不到 200 行，门禁/中断/日志三件事都在自己手里，行为可完全预期 | 需要自己维护消息格式（成本已在此处一次性付清） |

**结论**：本项目的核心诉求不是「让 Agent 更自由」，而是「让 Agent 在关键动作上绝对不自由」。
这类需求下，一个薄而确定的执行外壳（Harness）比通用框架更合适。循环只依赖两个接口——
`Decider.decide()` 和 `DISPATCH[tool]`——将来要换框架，替换 Decider 即可，门禁与日志不受影响。

## 2. 执行循环

```
round = 1
while round <= MAX_ROUNDS(默认5):
    decision = decider.decide(messages, tools)     # ① 模型决策
    if decision.kind == "final":                   # 出答案 → 推流 → 结束
        emit_delta(...); emit_citations(...); done

    messages.append(assistant(tool_calls))         # ② 记录「模型想调什么」
    for tc in decision.tool_calls:                #    一轮内可含多个调用
        if tc.name == "create_goods_draft":
            if not gate_ok(tc):                    # ③ 硬门禁
                log(gate_blocked); 回填错误; 本轮剩余跳过; break
            confirm_id = 建确认单()                 # ④ 强制中断
            流式推送草稿 → confirm_required → return（挂起）
        result = DISPATCH[tc.name](**tc.args)      # ⑤ 执行
        log(step); messages.append(tool(result))   # ⑥ 结果回填
        if result.passed is False: 本轮剩余跳过; break
    round += 1
```

**一轮 = 一次模型决策**，一轮内可以发起多个工具调用（串行执行）。
这样「最多 5 轮」不会卡住完整上架流程：查规则+查类目（1 轮）→ 三项校验（1 轮）→ 建草稿（1 轮）。

## 3. 事件协议（SSE）

| event | 载荷 | 前端用途 |
|---|---|---|
| `meta` | `{session_id, run_id}` | 记录会话/执行 ID |
| `thinking` | `{round, max}` | 「第 N/5 轮：智能体正在决策…」 |
| `tool_call` | `{round, step, tool, args}` | 轨迹行占位（执行中） |
| `tool_result` | `{tool, status, elapsed_ms, summary}` | 回填轨迹行状态与耗时 |
| `draft_start` | `{confirm_id}` | 创建草稿确认卡片 |
| `draft_delta` | `{text}` | 草稿内容逐行流式渲染 |
| `confirm_required` | `{confirm_id, draft}` | 出现「确认创建 / 取消」按钮 |
| `delta` | `{text}` | 最终答案打字机 |
| `citations` | `[{file, chunk, snippet, score}]` | 引用来源 |
| `done` / `error` | `{finish_reason}` / `{code, message}` | 收尾 |

## 4. 提示词约束不住的，用门禁兜住

提示词（`prompts.py`）负责「让模型知道规矩」，但它不是保证。真正的保证是三层：

1. **编排层门禁**（`_gate_create_draft`）：三项预检未全通过 / 令牌不匹配 → 工具根本不执行，回填 `E-STATE-001`；
2. **工具层门禁**（`tools.create_goods_draft`）：再查一次标志位 + `payload` 哈希 + 令牌，防止「校验 A、创建 B」；
3. **契约层**：`precheck_token` 由服务端签发，模型无法伪造，也无法把「自我判断」当校验结果。

## 5. 中断与续跑

- 中断时：`agent_runs.status = waiting_confirm`，`messages` 快照 + `pending_call` 落库；
- 确认时：`POST /api/agent/confirm` → `load_pending()` 校验（存在/归属/未处理/未过期 30 分钟）→
  同意则**此刻才真正执行** `create_goods_draft`，拒绝则把「用户取消」作为工具结果回填；
- 之后从 `round + 1` 继续同一个循环，所以「确认」不是重新问一遍，而是接着上次的推理往下走。

## 6. 日志（阶段 5 分析口径）

`agent_steps` 每行 = 一次工具调用：`round_no / step_no / tool_name / arguments / status / elapsed_ms / result_summary / error_code`。

`status` 的分布本身就是分析指标：

| status | 含义 | 阶段 5 可回答的问题 |
|---|---|---|
| `ok` | 成功 | 平均轮次、工具耗时分布、哪类问题最费轮次 |
| `gate_blocked` | 越序调用被拦 | 模型违规率（提示词是否需迭代） |
| `skipped` | 前一步失败被跳过 | 失败传播路径、最常见的首个失败点 |
| `confirm_pending` | 中断等待确认 | 中断率、确认转化率（被拒比例） |
| `error` | 工具异常 | 系统稳定性 |

## 7. 可调参数（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_MAX_ROUNDS` | 5 | 最大决策轮次 |
| `AGENT_CONFIRM_TTL_MIN` | 30 | 确认单有效期（分钟） |
| `AGENT_DECIDE_TIMEOUT` | 60 | 单次决策调用超时（秒） |
