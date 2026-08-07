from __future__ import annotations


class SafeApplicationError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class ProviderError(SafeApplicationError):
    def __init__(
        self,
        category: str,
        safe_message: str = "模型服务暂时不可用",
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(category, safe_message)
        self.category = category
        self.retryable = retryable


class ProviderConfigurationError(ProviderError):
    def __init__(self, safe_message: str = "DeepSeek 配置不完整") -> None:
        super().__init__(
            "provider_configuration",
            safe_message,
            retryable=False,
        )


class ProviderOutputError(ProviderError):
    def __init__(self, safe_message: str = "模型返回格式无效") -> None:
        super().__init__("provider_invalid_output", safe_message)


class DataIntegrityError(SafeApplicationError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(code, safe_message)


class SessionNotFoundError(SafeApplicationError):
    def __init__(self) -> None:
        super().__init__("session_not_found", "会话不存在或已过期")


class RequestInputError(SafeApplicationError):
    def __init__(self, safe_message: str = "请求字段无效") -> None:
        super().__init__("request_validation", safe_message)


class StorageUnavailableError(SafeApplicationError):
    def __init__(self, safe_message: str = "本地存储暂时不可用") -> None:
        super().__init__("storage_unavailable", safe_message)


class RateLimitError(SafeApplicationError):
    def __init__(self, safe_message: str = "今日调用次数已达上限") -> None:
        super().__init__("rate_limit_exceeded", safe_message)


class CircuitTrippedError(SafeApplicationError):
    def __init__(self, safe_message: str = "今日模型费用额度已用完") -> None:
        super().__init__("circuit_tripped", safe_message)
