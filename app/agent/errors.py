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


class ProviderUnavailableError(ProviderError):
    def __init__(self, safe_message: str = "所选模型服务当前不可用") -> None:
        super().__init__(
            "provider_unavailable",
            safe_message,
            retryable=False,
        )


class ProviderBusyError(ProviderError):
    def __init__(self) -> None:
        super().__init__(
            "provider_busy",
            "模型服务当前繁忙，请稍后重试",
            retryable=True,
        )


class InvalidProviderError(SafeApplicationError):
    def __init__(self, safe_message: str = "不支持所选模型服务") -> None:
        super().__init__("invalid_provider", safe_message)


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


class NewWorkPausedError(SafeApplicationError):
    def __init__(self) -> None:
        super().__init__(
            "new_work_paused",
            "当前暂停新的咨询和附件上传，请稍后再试",
        )


class RateLimitError(SafeApplicationError):
    def __init__(self, safe_message: str = "今日调用次数已达上限") -> None:
        super().__init__("rate_limit_exceeded", safe_message)


class CircuitTrippedError(SafeApplicationError):
    def __init__(self, safe_message: str = "今日模型费用额度已用完") -> None:
        super().__init__("circuit_tripped", safe_message)


class CaseNoProgressError(SafeApplicationError):
    def __init__(
        self,
        safe_message: str = (
            "当前信息下没有新的处理步骤；请补充对方回复、"
            "新材料、新事件或风险变化后再继续"
        ),
    ) -> None:
        super().__init__("case_no_progress", safe_message)


class ConsultationConflictError(SafeApplicationError):
    def __init__(
        self,
        safe_message: str = "会话刚刚发生更新，请重新提交本次追问",
    ) -> None:
        super().__init__("consultation_conflict", safe_message)
