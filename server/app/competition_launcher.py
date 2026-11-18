"""Local competition configuration and launch, without sending a model request."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit


SERVER_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SERVER_DIR / "competition.local.json"
CHANNELS = ("teacher", "student", "grading", "summary")
MODELS = {"teacher": "gpt-5.6-sol", "student": "gpt-5.6-sol",
          "grading": "gpt-5.6-terra", "summary": "gpt-5.6-terra"}


class CredentialConfigError(ValueError):
    """A fixed, safe credential-configuration message for the local CLI."""


def _credential_env_names(value: dict) -> tuple[str, str]:
    names = tuple(value.get(field) for field in ("teacher_key_env", "student_key_env"))
    if any(not isinstance(name, str) or not name.isascii() or not name.isidentifier() or len(name) > 128 for name in names):
        raise CredentialConfigError("凭据配置须填写有效的 teacher_key_env 和 student_key_env 环境变量名。")
    return names


def _validate_credentials(teacher_key: str, student_key: str) -> None:
    for key in (teacher_key, student_key):
        if not isinstance(key, str) or not key.strip() or len(key) > 8192 or any(ord(char) < 32 for char in key):
            raise CredentialConfigError("凭据环境变量缺失或无效。请在当前进程设置配置引用的教师和学生凭据；不要将密钥写入配置。")


def read_config(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("配置文件不存在或格式不受支持。请先运行 --setup，或用 --config 指定项目配置。")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ValueError("无法读取本机比赛配置。请检查 JSON 格式；不要将密钥粘贴到日志。") from None
    if isinstance(value, dict) and {"teacher_key", "student_key"} & set(value):
        raise CredentialConfigError(
            "旧比赛配置含明文凭据，未自动覆盖。请将凭据移入当前进程环境变量，移除 teacher_key/student_key，"
            "改用 schema_version=2 及 teacher_key_env/student_key_env 环境变量名后再启动。"
        )
    required = {"schema_version", "base_url", "teacher_key_env", "student_key_env", "data_dir"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"api_type"}:
        raise ValueError("比赛配置字段不完整。")
    if type(value["schema_version"]) is not int or value["schema_version"] != 2:
        raise ValueError("不支持此比赛配置版本。")
    _credential_env_names(value)
    validate_url(value["base_url"])
    config_api_type(value)
    if not isinstance(value["data_dir"], str) or not value["data_dir"].strip():
        raise ValueError("比赛配置需要独立数据目录。")
    return value


def validate_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        valid = (isinstance(value, str) and not any(char.isspace() for char in value)
                 and parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment
                 and parsed.port in (None, 443))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("模型地址须为不含凭据、查询参数的 HTTPS 地址。")


def config_api_type(value: dict) -> str:
    """Infer old local configurations from their exact endpoint path."""
    validate_url(value["base_url"])
    path = urlsplit(value["base_url"]).path.rstrip("/")
    inferred = "responses" if path.endswith("/responses") else "chat_completions"
    api_type = value.get("api_type", inferred)
    if not isinstance(api_type, str) or api_type not in {"chat_completions", "responses"}:
        raise ValueError("模型协议须为 responses 或 chat_completions。")
    if (path.endswith("/responses") and api_type != "responses") or (
        path.endswith("/chat/completions") and api_type != "chat_completions"
    ):
        raise ValueError("模型协议与完整接口地址不一致。")
    return api_type


def apply_config(value: dict, data_dir: Path | None = None) -> Path:
    api_type = config_api_type(value)
    if {"teacher_key_env", "student_key_env"} & set(value):
        teacher_env, student_env = _credential_env_names(value)
        teacher_key, student_key = os.environ.get(teacher_env), os.environ.get(student_env)
    else:
        # Compatibility for callers that already obtained authorized credentials
        # in memory. read_config never accepts these fields from a file.
        teacher_key, student_key = value.get("teacher_key"), value.get("student_key")
    _validate_credentials(teacher_key, student_key)
    target = data_dir or Path(value["data_dir"])
    if not target.is_absolute():
        target = SERVER_DIR / target
    target = target.resolve()
    # Resolve explicit references before dropping inherited model/knowledge state.
    for name in tuple(os.environ):
        if name.startswith(("LLM_", "MAXKB_", "POKE_")):
            os.environ.pop(name, None)
    os.environ.update({
        "SKIP_ENV_FILE": "1", "APP_ENV": "development", "COOKIE_SECURE": "false",
        "ALLOWED_HOSTS": "127.0.0.1,localhost", "CORS_ORIGINS": "",
        "STUDY_DEMO_ENABLED": "false", "STUDY_COMPETITION_UNMETERED": "true",
        "STUDY_WORKER_ENABLED": "true", "DATA_DIR": str(target),
        "LLM_REASONING_EFFORT": "high", "LLM_BATCH_TIMEOUT_SECONDS": "360",
        "LLM_INTERACTION_TIMEOUT_SECONDS": "180", "LLM_BATCH_MAX_TOKENS": "8192",
        "LLM_INTERACTION_MAX_TOKENS": "4096",
        "POKE_TEACHER_API_KEY": teacher_key, "POKE_STUDENT_API_KEY": student_key,
        "LLM_PROVIDERS_JSON": json.dumps([
            {"name": "poke-" + channel, "channel": channel, "base_url": value["base_url"],
             "api_type": api_type,
             "api_key_env": "POKE_TEACHER_API_KEY" if channel == "teacher" else "POKE_STUDENT_API_KEY",
             "model": MODELS[channel], "max_concurrency": 2, "queue_size": 60}
            for channel in CHANNELS
        ]),
    })
    return target


def setup(path: Path, *, base_url: str, data_dir: Path,
          teacher_key_env: str = "POKE_TEACHER_API_KEY", student_key_env: str = "POKE_STUDENT_API_KEY") -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("配置已存在，未覆盖。请编辑该本机文件，或用 --config 指定新的配置文件。")
    if not path.name.endswith(".local.json"):
        raise ValueError("本机配置必须使用 .local.json 文件名，以免被加入版本控制。")
    validate_url(base_url)
    _credential_env_names({"teacher_key_env": teacher_key_env, "student_key_env": student_key_env})
    target = data_dir if data_dir.is_absolute() else SERVER_DIR / data_dir
    value = {"schema_version": 2, "base_url": base_url.rstrip("/"),
             "teacher_key_env": teacher_key_env, "student_key_env": student_key_env,
             "data_dir": str(target.resolve())}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print("本机配置已保存，仅包含凭据环境变量名。未读取密钥或发送模型请求；启动前请在当前进程设置相应环境变量。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--setup", action="store_true", help="创建仅含凭据环境变量名的本机配置，不覆盖已有文件")
    actions.add_argument("--check", action="store_true", help="离线检查四通道，不发请求或打开数据库")
    actions.add_argument("--init-admin", action="store_true", help="在相同比赛数据目录初始化首个管理员")
    parser.add_argument("--username")
    parser.add_argument("--display-name", default="管理员")
    parser.add_argument("--base-url", default="https://www.poke2api.com/v1/responses")
    parser.add_argument("--teacher-key-env", default="POKE_TEACHER_API_KEY", help="教师凭据的环境变量名")
    parser.add_argument("--student-key-env", default="POKE_STUDENT_API_KEY", help="学生、评分和总结凭据的环境变量名")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口须在 1–65535 之间。")
    try:
        if args.setup:
            setup(args.config, base_url=args.base_url, data_dir=args.data_dir or Path("data/competition"),
                  teacher_key_env=args.teacher_key_env, student_key_env=args.student_key_env)
            return 0
        data_dir = apply_config(read_config(args.config), args.data_dir)
        from .config import Settings
        from .deployment_check import check_settings
        config = Settings()
        report = check_settings(config, require_models=True)
        if args.check:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["status"] == "configuration_valid" else 1
        if report["status"] != "configuration_valid":
            raise ValueError("四通道配置检查未通过，请运行 --check 查看脱敏结果。")
        if args.init_admin:
            if not args.username:
                raise ValueError("初始化管理员时需要 --username。")
            if not sys.stdin.isatty():
                raise ValueError("请在本机交互终端设置管理员密码。")
            from .init_admin import initialize_admin
            password = getpass.getpass("管理员密码（至少12字符，隐藏输入）：")
            if password != getpass.getpass("再次输入管理员密码："):
                raise ValueError("两次管理员密码不一致。")
            initialize_admin(data_dir / "platform.sqlite3", username=args.username,
                             display_name=args.display_name, password=password)
            print("管理员已创建。请使用该账号从页面登录。")
            return 0
    except CredentialConfigError as error:
        print(f"比赛启动未完成：{error}")
        return 1
    except (ValueError, RuntimeError, OSError):
        # Never interpolate configuration/parser exceptions containing credentials.
        print("比赛启动未完成：配置缺失、校验失败或管理员已存在。请先运行 --setup / --check；已有配置不会被覆盖。")
        return 1
    import uvicorn
    from .main import create_app
    print(f"比赛工作区：http://127.0.0.1:{args.port}/prototype/")
    print(f"独立数据目录：{data_dir}")
    print("真实模型已配置；比赛模式记录实际用量，金额为空，不要求价格或站内额度。")
    uvicorn.run(create_app(config), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
