# -*- coding: utf-8 -*-
"""Agent 编排层（Agent Orchestration Layer）。

对外只暴露两个东西：
    AgentHarness  —— 执行循环（决策 → 调工具 → 回填 → 再决策，最多 N 轮）
    get_decider   —— 决策器工厂（有 Key 用真实大模型，没 Key 用离线启发式）

设计说明见 docs/agent_design.md。
"""
from .decider import Decision, HeuristicDecider, LLMDecider, ToolCall, get_decider  # noqa: F401
from .harness import AgentHarness  # noqa: F401

__all__ = [
    "AgentHarness",
    "Decision",
    "ToolCall",
    "LLMDecider",
    "HeuristicDecider",
    "get_decider",
]
