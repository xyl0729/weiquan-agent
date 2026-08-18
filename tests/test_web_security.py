from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


WEB_ROOT = Path(__file__).resolve().parents[1] / "app" / "web"
CAPTCHA_SCRIPT_ORIGIN = "https://o.alicdn.com"


def _csp_directives(value: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for part in value.split(";"):
        tokens = part.strip().split()
        if tokens:
            directives[tokens[0]] = tokens[1:]
    return directives


def test_local_csp_does_not_allow_external_captcha_hosts() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))

    response = client.get("/")
    csp = _csp_directives(response.headers["content-security-policy"])

    assert csp["script-src"] == ["'self'"]
    assert csp["connect-src"] == ["'self'"]
    assert "frame-src" not in csp
    assert CAPTCHA_SCRIPT_ORIGIN not in str(csp)
    assert response.headers["cache-control"] == (
        "no-cache, max-age=0, must-revalidate"
    )


def test_static_assets_revalidate_before_reuse() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))

    response = client.get("/static/js/app.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == (
        "no-cache, max-age=0, must-revalidate"
    )


def test_production_csp_uses_exact_captcha_hosts_without_wildcards() -> None:
    settings = Settings(
        _env_file=None,
        captcha_enabled=True,
        captcha_scene_id="scene-public",
        captcha_prefix="prefix-public",
    ).model_copy(update={"deployment_mode": "production"})
    client = TestClient(create_app(settings))

    csp_value = client.get("/").headers["content-security-policy"]
    csp = _csp_directives(csp_value)
    captcha_origin = (
        "https://prefix-public.captcha-sdk.aliyuncs.com"
    )

    assert csp["script-src"] == [
        "'self'",
        CAPTCHA_SCRIPT_ORIGIN,
    ]
    assert csp["connect-src"] == ["'self'", captcha_origin]
    assert csp["frame-src"] == [captcha_origin]
    assert "*" not in csp_value
    assert "https://*.aliyuncs.com" not in csp_value


def test_production_csp_omits_captcha_hosts_when_disabled() -> None:
    settings = Settings(
        _env_file=None,
        captcha_scene_id="unused-scene",
        captcha_prefix="unused-prefix",
    ).model_copy(update={"deployment_mode": "production"})
    client = TestClient(create_app(settings))

    csp_value = client.get("/").headers["content-security-policy"]
    csp = _csp_directives(csp_value)

    assert csp["script-src"] == ["'self'"]
    assert csp["connect-src"] == ["'self'"]
    assert "frame-src" not in csp
    assert CAPTCHA_SCRIPT_ORIGIN not in csp_value
    assert "captcha-sdk.aliyuncs.com" not in csp_value


def test_frontend_sources_keep_sensitive_state_and_dom_contracts() -> None:
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in (WEB_ROOT / "js").glob("*.js")
    }
    combined = "\n".join(sources.values())

    assert "capabilities.js" in sources

    for forbidden in (
        "innerHTML",
        "insertAdjacentHTML",
        "localStorage",
        '"/api/providers"',
        "provider_id",
        "listProviders",
    ):
        assert forbidden not in combined

    assert "sessionStorage" in sources["state.js"]
    assert "sessionStorage" not in "\n".join(
        source
        for name, source in sources.items()
        if name != "state.js"
    )
    assert "clearAuthenticatedState" in sources["state.js"]
    assert "sessionStorage.clear" not in sources["state.js"]

    api_source = sources["api.js"]
    for endpoint in (
        '"/api/runtime-config"',
        '"/api/auth/captcha-config"',
        '"/api/auth/register"',
        '"/api/auth/login"',
        '"/api/auth/logout"',
        '"/api/auth/forgot-password"',
        '"/api/auth/reset-password"',
        '"/api/trial/start"',
        '"/api/trial/consult"',
        '"/api/privacy"',
    ):
        assert endpoint in api_source
    assert "X-CSRF-Token" in api_source
    assert "const CONSULT_TIMEOUT_MS = 35_000;" in api_source
    assert "new AbortController()" in api_source
    assert 'code: "consult_timeout"' in api_source
    assert "本次咨询等待时间较长，请稍后重试。" in api_source
    assert "controller.signal.aborted" in api_source
    assert "isSafeUnverifiedClarification" in api_source

    capabilities_source = sources["capabilities.js"]
    assert (
        'return ["local", "authenticated"].includes(identity?.status);'
        in capabilities_source
    )

    render_source = sources["render.js"]
    for local_label in (
        "本地完整测试",
        "不计应用额度",
        "真实模型调用仍可能产生 API 费用。",
    ):
        assert local_label in render_source
    for internal_label in (
        "正式规则覆盖",
        "未核验领域指导",
        "未核验领域的处理指引",
        "覆盖边界",
        "本轮运行信息",
        "Provider",
        "请求 ID",
        "Token 用量",
        "appendCoverageBanner",
        "appendResponseMetadata",
        "renderCoverageSummary",
        "第 ${index + 1} 轮",
    ):
        assert internal_label not in render_source
    assert "针对当前问题的处理建议" in render_source

    auth_source = sources["auth.js"]
    hash_read_index = auth_source.index("location.hash")
    fragment_clear_index = auth_source.index(
        "history.replaceState",
        hash_read_index,
    )
    fragment_parse_index = auth_source.index(
        "URLSearchParams",
        fragment_clear_index,
    )
    assert hash_read_index < fragment_clear_index < fragment_parse_index

    captcha_source = sources["captcha.js"]
    assert (
        "https://o.alicdn.com/captcha-frontend/"
        "aliyunCaptcha/AliyunCaptcha.js"
    ) in captcha_source
    assert "window.AliyunCaptchaConfig" in captcha_source
    assert "window.initAliyunCaptcha" in captcha_source
    assert 'mode: "popup"' in captcha_source
    assert "local-development-captcha" not in captcha_source
    assert "本地环境自动验证" not in captcha_source
    assert "return null" in captcha_source
    assert "captcha.setVisible" in sources["auth.js"]
    assert "captcha.isEnabled" in sources["privacy.js"]
    assert '"privacy_acceptance_required"' in sources["app.js"]
    assert '"trial_identity_required"' in sources["app.js"]


def test_public_index_has_account_privacy_and_authenticated_gates() -> None:
    index = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

    for forbidden in (
        "provider-options",
        "provider-retry",
        "provider-toolbar",
    ):
        assert forbidden not in index

    for required_id in (
        "account-button",
        "account-summary",
        "quota-summary",
        "auth-dialog",
        "auth-form",
        "captcha-slot",
        "privacy-dialog",
        "privacy-form",
        "history-panel",
        "new-attachment-trigger",
        "case-attachment-trigger",
    ):
        assert f'id="{required_id}"' in index

    for module in (
        "auth",
        "captcha",
        "privacy",
        "app",
    ):
        assert f'src="/static/js/{module}.js"' in index

    assert 'data-auth-only="true"' in index
    assert 'data-trial-only="true"' in index
    assert "CASE FILE" not in index
    assert "ACCOUNT" not in index
    assert ">案件资料<" in index
    assert ">账号<" in index


def test_anonymous_layout_does_not_reserve_hidden_history_column() -> None:
    stylesheet = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")

    assert (
        '.app-shell:not([data-identity="local"]):not(\n'
        '    [data-identity="authenticated"]\n'
        "  )\n"
        "  .app-body {\n"
        "  grid-template-columns: minmax(0, 1fr);\n"
        "}"
    ) in stylesheet
    assert (
        '.app-shell:not([data-identity="local"]):not(\n'
        '    [data-identity="authenticated"]\n'
        "  )[data-view=\"case\"]\n"
        "  .app-body {\n"
        "  grid-template-columns:\n"
        "    minmax(430px, 1fr)\n"
        "    var(--summary-width);\n"
        "}"
    ) in stylesheet
