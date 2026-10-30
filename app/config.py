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
