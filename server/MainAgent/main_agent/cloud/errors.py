class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retry_after = retry_after


def not_found():
    return ServiceError(404, "not_found", "资源不存在")


def unauthorized():
    return ServiceError(401, "invalid_credentials", "凭据无效或已过期")
