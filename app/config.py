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

    # 默认系统提示词（作为初始数据写入数据库，不写死在业务逻辑里）
    DEFAULT_PROMPT = (
        "你是商品上架资料审核助手，只根据提供的审核规则条文回答，"
        "不确定时明确说 '规则中未找到'，回答末尾附引用来源，"
        "不得编造资质要求与上架标准。"
    )


settings = Settings()

ROLE_NAME = {"submitter": "提交方", "reviewer": "审核员", "admin": "管理员"}
