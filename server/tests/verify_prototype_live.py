"""一条命令验完"前后端连在一起"这件事。

做的事：
  1. 在一个全新的临时目录里种出一个演示世界（真实接口调用，不是造数据）；
  2. 起正式服务；
  3. 用无头浏览器把教师端和学生端各走一遍；
  4. 关掉服务，再用纯静态服务验证"没有后端"时的降级仍然可用。

用法（仓库根目录）：
    .\\server\\.venv\\Scripts\\python.exe -I -B -X utf8 server/tests/verify_prototype_live.py

需要 Node 与 .scratch/proto-test 里已安装的 puppeteer-core。
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
PROTO_TEST = ROOT / ".scratch" / "proto-test"
NODE = shutil.which("node") or "node"
LOOPBACK = ipaddress.ip_address("127.0.0.1")
TEMP_ROOT = Path(tempfile.gettempdir()).resolve()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """全程只与本机服务通信：任何重定向都拒绝，避免被引到别处去。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def local_url(port: int, path: str = "") -> str:
    """只允许 http://127.0.0.1:<port><绝对路径>。"""
    url = f"http://127.0.0.1:{port}{path}"
    parts = urlsplit(url)
    if parts.scheme != "http":
        raise SystemExit("只允许 http")
    if parts.hostname is None or ipaddress.ip_address(parts.hostname) != LOOPBACK:
        raise SystemExit("只允许本机回环地址")
    if not 1024 <= (parts.port or 0) <= 65535:
        raise SystemExit("端口超出范围")
    return url


def under(path: Path, parent: Path) -> Path:
    """把路径规范化后确认它确实在 parent 之下，防止越界。"""
    resolved = path.resolve()
    root = parent.resolve()
    if not resolved.is_relative_to(root):
        raise SystemExit(f"路径越界：{resolved} 不在 {root} 之内")
    return resolved


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait(url: str, process: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"服务提前退出（{process.returncode}）")
        try:
            request = urllib.request.Request(url)
            with _OPENER.open(request, timeout=2) as response:
                if response.status < 500:
                    return
        except urllib.error.HTTPError:
            return
        except Exception:
            pass
        time.sleep(0.3)
    raise SystemExit(f"等待 {url} 超时")


def check_argv(argv: list[str], *, cwd: Path | None = None) -> list[str]:
    """进程启动前的白名单闸门。

    这个脚本会拉起 python 与 node 各若干次。虽然每次的 argv 都由本文件里的
    常量拼出来，但把"允许启动什么"写成显式检查，比依赖"我确认过没注入点"
    更可靠：以后谁改了调用处，注入不进来也休想蒙混过去。

    规则：
      - 首元素必须是解释器本身（python / node），不接受任意可执行文件名；
      - 不允许控制字符出现在任何参数里（这里根本不该有）；
      - 脚本参数若以 .py/.mjs 结尾，必须是已存在的常规文件（按子进程的工作目录解析）。
    """
    if not argv:
        raise SystemExit("空命令")
    head = argv[0]
    allowed_heads = {sys.executable, NODE}
    if head not in allowed_heads:
        raise SystemExit(f"拒绝启动未列入白名单的可执行文件：{head}")
    for arg in argv[1:]:
        if any(ch in arg for ch in "\n\r\x00"):
            raise SystemExit("参数里出现控制字符，拒绝执行")
    base = Path(cwd) if cwd is not None else Path.cwd()
    for arg in argv[1:]:
        if arg.endswith((".py", ".mjs")):
            target = Path(arg)
            if not target.is_absolute():
                target = base / target
            if not target.is_file():
                raise SystemExit(f"脚本不存在或不是常规文件：{arg}")
    return argv


def run(argv: list[str], *, env=None, cwd: Path, title: str) -> None:
    """统一用参数列表启动子进程：不经过 shell，且启动前过一遍白名单。"""
    print(f"\n=== {title} ===")
    result = subprocess.run(check_argv(list(argv), cwd=cwd), env=env, cwd=str(cwd), text=True,
                            capture_output=True, shell=False, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    # 保留足够长的尾部：失败时脚本会打印页面状态，截太短就只剩一句 timeout
    print(output.strip()[-8000:])
    if result.returncode != 0:
        raise SystemExit(f"{title} 未通过（退出码 {result.returncode}）")


def cleanup(workdir: Path, *, keep: bool) -> None:
    """只删除本次 mkdtemp 建出来的目录；前缀与位置都再核一次。"""
    if keep:
        print(f"保留临时目录：{workdir}")
        return
    # mkdtemp 保证目录已存在且是我们刚建的；这里再核一次前缀与位置，
    # 任何一条不符就不动手，宁可留下也不误删。
    if not workdir.is_dir():
        return
    if not workdir.name.startswith("axis-verify-"):
        print(f"跳过清理（目录名不像本次创建）：{workdir.name}")
        return
    if not workdir.resolve().is_relative_to(TEMP_ROOT):
        print("跳过清理（目录不在系统临时目录下）")
        return
    shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="验证结束后保留临时数据与凭据")
    args = parser.parse_args()

    if not PROTO_TEST.exists():
        raise SystemExit(f"找不到 {PROTO_TEST}；无头浏览器依赖没有安装")

    workdir = Path(tempfile.mkdtemp(prefix="axis-verify-"))
    data_dir = under(workdir / "data", workdir)
    creds_file = under(workdir / "credentials.json", workdir)
    print(f"临时数据目录：{data_dir}")

    env = dict(os.environ)
    env["SKIP_ENV_FILE"] = "1"
    seed_script = under(SERVER / "tests" / "seed_demo_world.py", SERVER)
    verify_cwd = under(PROTO_TEST, ROOT)

    try:
        # 1. 种世界
        run([sys.executable, "-I", "-B", "-X", "utf8", str(seed_script),
             "--data-dir", str(data_dir), "--password-file", str(creds_file)],
            env=env, cwd=under(ROOT, ROOT), title="种出一个演示世界")

        if not creds_file.exists():
            raise SystemExit("没有生成凭据文件")

        # 2. 用种子的库起正式服务
        port = free_port()
        base = local_url(port)
        server_env = dict(env)
        server_env.update({"APP_ENV": "development", "DATA_DIR": str(data_dir),
                           "STUDY_DATA_DIR": str(data_dir)})
        # 服务日志直接丢掉：这个脚本只关心测试结论，不落文件也就没有路径问题
        server = subprocess.Popen(
            check_argv([sys.executable, "-m", "uvicorn", "app.main:app",
                        "--host", "127.0.0.1", "--port", str(port)], cwd=SERVER),
            cwd=str(under(SERVER, ROOT)), env=server_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            wait(local_url(port, "/health"), server, 45)
            print(f"正式服务：{base}")

            live_env = dict(os.environ)
            live_env["PROTO_BASE"] = base
            live_env["AXIS_CREDS"] = str(creds_file)

            # 3. 浏览器走一遍
            run([NODE, "check-live.mjs"], env=live_env, cwd=verify_cwd,
                title="学生端：真实任务与真实结果")
            run([NODE, "check-live-teacher.mjs"], env=live_env, cwd=verify_cwd,
                title="教师端：真实班级与真实学情")
        finally:
            server.terminate()
            try:
                server.wait(timeout=12)
            except subprocess.TimeoutExpired:
                server.kill()

        # 4. 没有后端时的降级
        static_port = free_port()
        static_base = local_url(static_port)
        static = subprocess.Popen(
            check_argv([sys.executable, "-m", "http.server", str(static_port), "--bind", "127.0.0.1"],
                       cwd=ROOT / "web"),
            cwd=str(under(ROOT / "web", ROOT)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(2)
            fallback_env = dict(os.environ)
            fallback_env["PROTO_BASE"] = static_base
            run([NODE, "check-entry.mjs"], env=fallback_env, cwd=verify_cwd,
                title="无后端：入口仍可用")
            run([NODE, "check-auth.mjs"], env=fallback_env, cwd=verify_cwd,
                title="无后端：登录流程仍可用")
        finally:
            static.terminate()
            try:
                static.wait(timeout=8)
            except subprocess.TimeoutExpired:
                static.kill()

        print("\n=== 全部通过 ===")
        print("前后端已连通：真实登录、真实任务、真实评卷、无后端降级都验证过。")
    finally:
        cleanup(workdir, keep=args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
