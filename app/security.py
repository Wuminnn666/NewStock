# -*- coding: utf-8 -*-
"""密码哈希与 JWT 签发/校验（不依赖数据库，避免循环导入）。"""
import hashlib
import hmac
import os
import time

import jwt

from .config import settings

_PBKDF2_ROUNDS = 100000


def hash_password(password: str) -> str:
    """使用 PBKDF2-SHA256 加盐哈希，格式：salt$hash。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return salt.hex() + "$" + dk.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def create_access_token(user) -> str:
    """签发 JWT：sub=uid，role 用于接口级 RBAC。"""
    payload = {
        "uid": user.id,
        "username": user.username,
        "role": user.role,
        "name": user.display_name or user.username,
        "exp": int(time.time()) + settings.JWT_EXPIRE_MINUTES * 60,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALG)


def decode_token(token: str):
    """解析 JWT，失败返回 None。"""
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALG])
    except Exception:
        return None
