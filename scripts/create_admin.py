from __future__ import annotations

import argparse
import sys
from getpass import getpass
from pathlib import Path

from fastapi import FastAPI

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.auth.dependencies import initialize_auth_dependencies  # noqa: E402
from app.auth.errors import AuthError  # noqa: E402
from app.auth.service import AuthService  # noqa: E402
from app.config import Settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在受控服务器终端创建已验证管理员账号。",
    )
    parser.add_argument("email", help="管理员邮箱")
    parser.add_argument(
        "--privacy-consent-recorded",
        action="store_true",
        help="确认已取得账号持有人对当前隐私政策的同意",
    )
    args = parser.parse_args()
    if not args.privacy_consent_recorded:
        parser.error("必须确认已记录当前隐私政策同意")

    password = _read_password()
    application, service = _production_service()
    try:
        user = service.create_admin_account(
            email=args.email,
            password=password,
            privacy_version=service.policy.version,
            privacy_accepted=True,
        )
    except AuthError as exc:
        parser.error(exc.safe_message)
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        _dispose_engine(application)
    print(f"管理员账号已创建：{user.email}")
    return 0


def _read_password() -> str:
    first = getpass("输入密码（10 至 128 个字符）：")
    second = getpass("再次输入密码：")
    if first != second:
        raise SystemExit("两次输入的密码不一致")
    return first


def _production_service() -> tuple[FastAPI, AuthService]:
    settings = Settings()
    if settings.deployment_mode != "production":
        raise SystemExit("该命令只允许在 production 模式运行")
    application = FastAPI()
    application.state.settings = settings
    return application, initialize_auth_dependencies(application)


def _dispose_engine(application: FastAPI) -> None:
    engine = getattr(application.state, "database_engine", None)
    if engine is not None:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
