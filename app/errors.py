# -*- coding: utf-8 -*-
"""统一业务异常：所有接口先返回错误码 + 中文信息。"""


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)
