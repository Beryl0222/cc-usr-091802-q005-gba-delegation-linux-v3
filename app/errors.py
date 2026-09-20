"""领域错误：携带稳定的机器可读错误码，由 HTTP 层映射为状态码。"""


class DomainError(Exception):
    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


class NotFound(DomainError):
    def __init__(self, what, key):
        super().__init__("not_found", f"{what} 不存在: {key}", 404, {"resource": what, "id": key})


class Conflict(DomainError):
    def __init__(self, code, message, details=None):
        super().__init__(code, message, 409, details)


class PermissionDenied(DomainError):
    def __init__(self, message="无权查看该资源", details=None):
        super().__init__("forbidden", message, 403, details)
