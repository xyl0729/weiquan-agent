from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping, TypeAlias

from app.agent.errors import SafeApplicationError


AttachmentErrorCode: TypeAlias = Literal[
    "attachment_type_unsupported",
    "attachment_type_mismatch",
    "attachment_name_invalid",
    "attachment_pdf_encrypted",
    "attachment_corrupt",
    "attachment_text_empty",
    "attachment_too_large",
    "attachment_page_limit_exceeded",
    "attachment_pixel_limit_exceeded",
    "attachment_extracted_text_too_long",
    "attachment_extraction_timeout",
    "attachment_not_found",
    "attachment_not_reviewable",
    "attachment_not_confirmed",
    "attachment_already_bound",
    "attachment_count_exceeded",
    "attachment_context_too_long",
    "attachment_service_busy",
    "attachment_service_unavailable",
]


@dataclass(frozen=True, slots=True)
class AttachmentErrorSpec:
    status_code: int
    message: str


ATTACHMENT_ERROR_SPECS: Mapping[
    AttachmentErrorCode,
    AttachmentErrorSpec,
] = MappingProxyType(
    {
        "attachment_type_unsupported": AttachmentErrorSpec(
            422,
            "仅支持 PDF、PNG、JPG 或 JPEG 文件",
        ),
        "attachment_type_mismatch": AttachmentErrorSpec(
            422,
            "文件内容与声明类型不一致，请重新选择原始文件",
        ),
        "attachment_name_invalid": AttachmentErrorSpec(
            422,
            "文件名无效，请重命名后重试",
        ),
        "attachment_pdf_encrypted": AttachmentErrorSpec(
            422,
            "暂不支持加密 PDF，请先移除密码后重试",
        ),
        "attachment_corrupt": AttachmentErrorSpec(
            422,
            "文件无法读取，请重新导出或选择其他文件",
        ),
        "attachment_text_empty": AttachmentErrorSpec(
            422,
            "未识别到可核对文字，请选择更清晰的文件",
        ),
        "attachment_too_large": AttachmentErrorSpec(
            413,
            "文件过大，请压缩或拆分后重试",
        ),
        "attachment_page_limit_exceeded": AttachmentErrorSpec(
            413,
            "PDF 页数过多，请拆分后重试",
        ),
        "attachment_pixel_limit_exceeded": AttachmentErrorSpec(
            413,
            "图片尺寸过大，请缩小后重试",
        ),
        "attachment_extracted_text_too_long": AttachmentErrorSpec(
            413,
            "文件文字过多，请拆分后重试",
        ),
        "attachment_extraction_timeout": AttachmentErrorSpec(
            422,
            "文件处理超时，请缩小文件或稍后重试",
        ),
        "attachment_not_found": AttachmentErrorSpec(
            404,
            "附件不存在或已过期，请重新上传",
        ),
        "attachment_not_reviewable": AttachmentErrorSpec(
            409,
            "当前附件状态不可核对，请刷新后重试",
        ),
        "attachment_not_confirmed": AttachmentErrorSpec(
            409,
            "附件尚未确认，请先核对文字",
        ),
        "attachment_already_bound": AttachmentErrorSpec(
            409,
            "附件已用于其他咨询，不能重复发送",
        ),
        "attachment_count_exceeded": AttachmentErrorSpec(
            413,
            "本轮附件数量过多，请移除部分附件",
        ),
        "attachment_context_too_long": AttachmentErrorSpec(
            413,
            "本轮附件文字总量过多，请减少内容后重试",
        ),
        "attachment_service_unavailable": AttachmentErrorSpec(
            503,
            "本地文字提取暂时不可用，仍可继续文字咨询",
        ),
        "attachment_service_busy": AttachmentErrorSpec(
            503,
            "文字提取任务较多，请稍后重试",
        ),
    }
)

_INPUT_CODES = frozenset(
    {
        "attachment_type_unsupported",
        "attachment_type_mismatch",
        "attachment_name_invalid",
        "attachment_pdf_encrypted",
        "attachment_corrupt",
        "attachment_text_empty",
        "attachment_extraction_timeout",
    }
)
_RESOURCE_CODES = frozenset(
    {
        "attachment_too_large",
        "attachment_page_limit_exceeded",
        "attachment_pixel_limit_exceeded",
        "attachment_extracted_text_too_long",
        "attachment_count_exceeded",
        "attachment_context_too_long",
    }
)
_STATE_CODES = frozenset(
    {
        "attachment_not_reviewable",
        "attachment_not_confirmed",
        "attachment_already_bound",
    }
)


class AttachmentError(SafeApplicationError):
    def __init__(self, code: AttachmentErrorCode) -> None:
        spec = ATTACHMENT_ERROR_SPECS[code]
        super().__init__(code, spec.message)
        self.status_code = spec.status_code


class AttachmentInputError(AttachmentError):
    def __init__(self, code: AttachmentErrorCode) -> None:
        if code not in _INPUT_CODES:
            raise ValueError("错误代码不属于附件输入错误")
        super().__init__(code)


class AttachmentResourceLimitError(AttachmentError):
    def __init__(self, code: AttachmentErrorCode) -> None:
        if code not in _RESOURCE_CODES:
            raise ValueError("错误代码不属于附件资源限制")
        super().__init__(code)


class AttachmentNotFoundError(AttachmentError):
    def __init__(self) -> None:
        super().__init__("attachment_not_found")


class AttachmentStateConflictError(AttachmentError):
    def __init__(self, code: AttachmentErrorCode) -> None:
        if code not in _STATE_CODES:
            raise ValueError("错误代码不属于附件状态冲突")
        super().__init__(code)


class AttachmentServiceUnavailableError(AttachmentError):
    def __init__(self) -> None:
        super().__init__("attachment_service_unavailable")


class AttachmentServiceBusyError(AttachmentError):
    def __init__(self) -> None:
        super().__init__("attachment_service_busy")


def build_attachment_error(code: str) -> AttachmentError:
    if code in _INPUT_CODES:
        return AttachmentInputError(code)  # type: ignore[arg-type]
    if code in _RESOURCE_CODES:
        return AttachmentResourceLimitError(code)  # type: ignore[arg-type]
    if code == "attachment_not_found":
        return AttachmentNotFoundError()
    if code in _STATE_CODES:
        return AttachmentStateConflictError(code)  # type: ignore[arg-type]
    if code == "attachment_service_unavailable":
        return AttachmentServiceUnavailableError()
    if code == "attachment_service_busy":
        return AttachmentServiceBusyError()
    raise ValueError("未知附件错误代码")
