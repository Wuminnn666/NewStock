# -*- coding: utf-8 -*-
"""全局配置：全部可通过环境变量覆盖，供应商可替换。"""
import os


class Settings:
    APP_NAME = "商品上架与资料审核系统"
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-please-change-in-prod")
    JWT_ALG = "HS256"
    JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "720"))

    DB_URL = os.getenv("DB_URL", "sqlite:///./data.db")

    # LLM（OpenAI 兼容协议，可换 DeepSeek / 通义等）
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
    LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
    LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1200"))
    # 超时三件套：缺一个就会挂死。
    #  - connect：TCP/TLS 握手，正常 1s 内完成，给 5s 足够；
    #  - read：流式场景下这是「两个 chunk 之间」的最大间隔，不是总时长，
    #          所以 60s 已经非常宽松（模型正常 1~3s 吐第一个字）；
    #  - write/pool：防止连接池被占满时无限等待。
    # 没有 read 超时的后果：上游不吐字也不断开，请求会永久挂住，
    # SSE 连接持续堆积，最终把 worker 拖死——这是最常见的线上雪崩形态。
    LLM_CONNECT_TIMEOUT = float(os.getenv("LLM_CONNECT_TIMEOUT", "5"))
    LLM_READ_TIMEOUT = float(os.getenv("LLM_READ_TIMEOUT", "60"))
    LLM_WRITE_TIMEOUT = float(os.getenv("LLM_WRITE_TIMEOUT", "10"))
    LLM_POOL_TIMEOUT = float(os.getenv("LLM_POOL_TIMEOUT", "5"))

    # 运行环境：dev / prod。prod 下若干「不安全的默认值」会直接拒绝启动。
    ENV = os.getenv("ENV", "dev")

    # ---------------- 限流与配额（对应上线清单 B1/B2/B3） ----------------
    # 阈值取值思路：正常提交方一天提问几次到几十次，10 次/分钟已经非常宽松，
    # 但足以挡住脚本刷接口。登录接口更严，因为它是撞库的首选目标。
    RATE_CHAT_PER_MIN = int(os.getenv("RATE_CHAT_PER_MIN", "10"))
    RATE_AGENT_PER_MIN = int(os.getenv("RATE_AGENT_PER_MIN", "10"))
    RATE_LOGIN_PER_MIN = int(os.getenv("RATE_LOGIN_PER_MIN", "5"))

    # token 日配额（按账号 / 全局）。注意：流式接口拿不到精确 usage，
    # 这里按字符数估算（见 guard.estimate_tokens），用于「防失控」而非「精确计费」。
    LLM_DAILY_TOKENS_PER_USER = int(os.getenv("LLM_DAILY_TOKENS_PER_USER", "200000"))
    LLM_DAILY_TOKENS_GLOBAL = int(os.getenv("LLM_DAILY_TOKENS_GLOBAL", "2000000"))

    # 同时在飞的模型请求上限 + 排队上限；排队超时宁可快速失败，也不让用户干等
    LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "10"))
    LLM_QUEUE_TIMEOUT = float(os.getenv("LLM_QUEUE_TIMEOUT", "15"))

    # 单实例允许同时挂着的 SSE 流式连接数（对应上线清单 B4）。
    # 为什么必须显式设上限：一条 SSE 连接占一个协程 + 一个 socket fd，
    # 连接只增不减会把 FD 吃光，届时整个进程连健康检查都拒连——
    # 表现为「服务假死」，比直接 429 更糟。宁可让第 201 个用户看到「稍后再试」。
    SSE_MAX_CONNECTIONS = int(os.getenv("SSE_MAX_CONNECTIONS", "200"))

    # ---------------- 降级开关（对应上线清单 E5） ----------------
    # DEGRADED_MODE=true：不调模型，只回检索到的规则条文 + 引用。
    # 这是模型故障/账单异常时唯一能「不停服」的手段，所以必须能独立于代码生效。
    DEGRADED_MODE = os.getenv("DEGRADED_MODE", "false").lower() in ("1", "true", "yes")
    # AGENT_ENABLED=false：整个智能体编排下线，仅保留规则问答
    AGENT_ENABLED = os.getenv("AGENT_ENABLED", "true").lower() in ("1", "true", "yes")

    # ---------------- 数据保留（对应上线清单 A3） ----------------
    RETENTION_CHAT_DAYS = int(os.getenv("RETENTION_CHAT_DAYS", "180"))
    RETENTION_AGENT_DAYS = int(os.getenv("RETENTION_AGENT_DAYS", "90"))
    RETENTION_SECURITY_DAYS = int(os.getenv("RETENTION_SECURITY_DAYS", "365"))

    # Embedding（默认复用 LLM 的 key/base_url）
    EMB_API_KEY = os.getenv("EMB_API_KEY", os.getenv("LLM_API_KEY", ""))
    EMB_BASE_URL = os.getenv("EMB_BASE_URL", os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"))
    EMB_MODEL = os.getenv("EMB_MODEL", "text-embedding-3-small")
    EMB_DIM = int(os.getenv("EMB_DIM", "1536"))

    # 检索参数
    CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "500"))
    TOP_K = int(os.getenv("TOP_K", "3"))
    HYBRID_BM25 = float(os.getenv("HYBRID_BM25", "0.5"))
    HYBRID_VECTOR = float(os.getenv("HYBRID_VECTOR", "0.5"))
    HISTORY_ROUNDS = int(os.getenv("HISTORY_ROUNDS", "6"))
    MAX_QUESTION_LEN = int(os.getenv("MAX_QUESTION_LEN", "2000"))
    UPLOAD_MAX_MB = int(os.getenv("UPLOAD_MAX_MB", "20"))

    # ---------------- Agent 编排 ----------------
    # 最大决策轮次：一轮 = 一次模型决策，一轮内可以并发/串行发起多个工具调用
    AGENT_MAX_ROUNDS = int(os.getenv("AGENT_MAX_ROUNDS", "5"))
    # 人工确认单有效期（分钟），超时作废，防止过期草稿被误创建
    AGENT_CONFIRM_TTL_MIN = int(os.getenv("AGENT_CONFIRM_TTL_MIN", "30"))
    # 决策调用（非流式）超时秒数
    AGENT_DECIDE_TIMEOUT = int(os.getenv("AGENT_DECIDE_TIMEOUT", "60"))

    # 默认系统提示词（作为初始数据写入数据库，不写死在业务逻辑里）
    DEFAULT_PROMPT = (
        "你是商品上架资料审核助手，只根据提供的审核规则条文回答，"
        "不确定时明确说 '规则中未找到'，回答末尾附引用来源，"
        "不得编造资质要求与上架标准。"
    )


settings = Settings()

ROLE_NAME = {"submitter": "提交方", "reviewer": "审核员", "admin": "管理员"}
