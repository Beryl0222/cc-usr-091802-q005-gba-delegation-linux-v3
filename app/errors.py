"""领域错误与 HTTP 状态码的映射。"""


class ApiError(Exception):
    """所有可预期的业务错误都抛出本类型，由 HTTP 层统一转 JSON。"""

    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        body = {"error": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


def bad_request(message, details=None):
    return ApiError(400, "bad_request", message, details)


def not_found(message, details=None):
    return ApiError(404, "not_found", message, details)


def conflict(message, details=None):
    # 422：请求结构合法，但当前业务状态不允许（截止点已过、容量不足等）
    return ApiError(422, "conflict", message, details)
