"""为可点击原型准备一个可直接演示的真实数据世界。

这不是测试脚本，也不做故障注入——它只做一件事：在一个全新的数据目录里，
按正式接口把"一间教室"建出来，让原型连着真后端时一登录就有东西可看。

建出来的东西：
  管理员 / 教师 陈宁 / 学生 林夏、周予 / 一门课 / 一个班
  一份已确认的教学材料 / 五道已审查的选择题
  一个目标 / 第一轮任务（含独立首诊）/ 两名学生各自的正式卷
  林夏与周予各交一份卷，跑真实后台评卷，再生成一份班级报告

用法（仓库根目录）：
    .\\server\\.venv\\Scripts\\python.exe -I -B -X utf8 server/tests/seed_demo_world.py --data-dir server/data/demo-world

密码只在终端打印一次；加 --password-file 才会另存一份到本机。
账号与口令可用 --admin-username/--admin-password/--teacher-username/
--teacher-password/--student-password 覆盖默认值；平台口令策略不放宽
（管理员 12–128 位，教师与学生至少 8 位）。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"

READY_TIMEOUT = 40.0
GRADE_TIMEOUT = 60.0
LOOPBACK = ipaddress.ip_address("127.0.0.1")

# 演示世界的固定人物与设定。内容是虚构样例，不是真实学生资料。
WORLD = {
    "teacher": {"username": "chenning", "display_name": "陈宁"},
    "students": [
        {"key": "lin", "username": "linxia", "display_name": "林夏"},
        {"key": "zhou", "username": "zhouyu", "display_name": "周予"},
    ],
    "course": {"name": "七年级数学", "term": "2026 秋"},
    "class": {"name": "七（1）班"},
    "material": {
        "name": "一元一次方程 · 去括号与检验（摘录）",
        "body": (
            "七年级数学 · 一元一次方程\n\n"
            "2.2 去括号\n"
            "括号外的数要乘到括号里的每一项。\n"
            "例：3(x - 2) = 3x - 6，而不是 3x - 2。\n\n"
            "2.3 移项与检验\n"
            "移项要变号；解出后要把解代回原方程检验。\n"
        ),
    },
    "goal": {
        "title": "能正确去括号并检验方程的解",
        "description": "先看每人已有的依据，再决定讲解还是直接做题。本轮 2 题。",
    },
}

# 五题：第 1、2 题是正式卷，第 3 题是独立首诊，第 4、5 题留给后续轮次。
QUESTIONS = [
    {
        "id": "demo-q-bracket-expand",
        "stem": "3(x − 2) = 2x + 1，去括号后得到？",
        "options": ["3x − 2 = 2x + 1", "3x − 6 = 2x + 1", "3x + 6 = 2x + 1"],
        "answer": 1,
        "points": 2,
        "explanation": "3 要同时乘 x 和 −2，所以是 3x − 6。",
        "rubric": [{"point": "去括号时括号外的数乘到每一项", "score": 2}],
    },
    {
        "id": "demo-q-check-solution",
        "stem": "方程 2x + 3 = 11 的解是 x = 4，代回检验结果是？",
        "options": ["左边 = 11，右边 = 11，成立", "左边 = 9，右边 = 11，不成立", "无法检验"],
        "answer": 0,
        "points": 3,
        "explanation": "2×4 + 3 = 11，左右相等，所以 x = 4 是解。",
        "rubric": [{"point": "把解代回原方程并比较两边", "score": 3}],
    },
    {
        "id": "demo-q-diagnostic",
        "stem": "移项：由 x + 5 = 12 得到 x = ？",
        "options": ["7", "17", "−7"],
        "answer": 0,
        "points": 1,
        "explanation": "移项要变号：x = 12 − 5 = 7。",
        "rubric": [{"point": "移项时改变符号", "score": 1}],
    },
    {
        "id": "demo-q-fraction-coefficient",
        "stem": "方程 (1/2)x = 6 的解是？",
        "options": ["x = 3", "x = 12", "x = 1/12"],
        "answer": 1,
        "points": 2,
        "explanation": "两边同乘 2，x = 12。",
        "rubric": [{"point": "系数化为 1", "score": 2}],
    },
    {
        "id": "demo-q-word-problem",
        "stem": "某数的 3 倍减去 4 等于 11，这个数是？",
        "options": ["5", "7", "15"],
        "answer": 0,
        "points": 3,
        "explanation": "设为 x：3x − 4 = 11，x = 5。",
        "rubric": [{"point": "把文字条件翻译成方程", "score": 3}],
    },
]


def _local_base(port: int) -> str:
    """构造本机回环地址，并断言它不是别的什么东西。"""
    base = f"http://127.0.0.1:{port}"
    parts = urlsplit(base)
    if parts.scheme != "http":
        raise SystemExit("只允许 http")
    if parts.hostname is None:
        raise SystemExit("地址缺少主机名")
    address = ipaddress.ip_address(parts.hostname)
    if address != LOOPBACK:
        raise SystemExit("只允许本机回环地址 127.0.0.1")
    if parts.port != port or not 1024 <= port <= 65535:
        raise SystemExit("端口异常")
    return base


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """种子脚本只和本机服务通信，任何重定向都视为异常，直接拒绝。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


@dataclass
class Client:
    base: str
    cookie: str = ""
    _opener: urllib.request.OpenerDirector = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # 校验一次基地址，之后所有请求都复用它
        expected_port = urlsplit(self.base).port
        if _local_base(int(expected_port or 0)) != self.base:
            raise SystemExit(f"拒绝非本机回环地址：{self.base}")
        self._opener = urllib.request.build_opener(_NoRedirect)

    def url(self, path: str) -> str:
        """所有请求都必须落在已校验的本机基地址之下。"""
        if not path.startswith("/"):
            raise SystemExit(f"只接受绝对路径：{path}")
        if "//" in path.split("?", 1)[0][1:]:
            raise SystemExit(f"路径里不允许出现协议或主机：{path}")
        return _local_base(int(urlsplit(self.base).port or 0)) + path

    def request(self, method: str, path: str, *, body=None, expect=200, origin=True):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if origin:
            headers["Origin"] = self.base
        if self.cookie:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(self.url(path), data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=30) as response:
                raw = response.read()
                status = response.status
                set_cookie = response.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as error:
            raw = error.read()
            status = error.code
            set_cookie = error.headers.get("Set-Cookie", "") or ""
        if status != expect:
            raise SystemExit(f"{method} {path} 期望 {expect}，实际 {status}：{raw[:400].decode('utf-8', 'replace')}")
        if "sid=" in set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        return json.loads(raw) if raw else None

    def upload_material(self, path: str, filename: str, content: bytes, fields: dict[str, str]) -> dict:
        boundary = "----axisseed"
        chunks = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: text/plain\r\n\r\n".encode("utf-8"),
            content,
            b"\r\n",
        ]
        for name, value in fields.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            chunks.append(str(value).encode("utf-8"))
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        request = urllib.request.Request(
            self.url(path), data=b"".join(chunks), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "Origin": self.base, "Cookie": self.cookie, "Accept": "application/json"},
        )
        with self._opener.open(request, timeout=30) as response:
            return json.loads(response.read())

    def generate_report(self, path: str, payload: dict, key: str) -> dict:
        request = urllib.request.Request(
            self.url(path), data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Origin": self.base,
                     "Cookie": self.cookie, "Idempotency-Key": key},
        )
        with self._opener.open(request, timeout=60) as response:
            return json.loads(response.read())


def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def check_argv(argv: list[str]) -> list[str]:
    """启动服务进程前的白名单闸门：只允许解释器本身，参数不带控制字符。"""
    if not argv or argv[0] != sys.executable:
        raise SystemExit(f"拒绝启动未列入白名单的可执行文件：{argv[0] if argv else '(空)'}")
    for arg in argv[1:]:
        if any(ch in arg for ch in "\n\r\x00"):
            raise SystemExit("参数里出现控制字符，拒绝执行")
    return argv


def wait_health(base: str, process: subprocess.Popen, deadline: float) -> None:
    client = Client(base)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"服务提前退出，返回码 {process.returncode}")
        try:
            request = urllib.request.Request(client.url("/health"))
            with client._opener.open(request, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.2)
    raise SystemExit("服务健康检查超时")


def poll(client: Client, path: str, done, timeout=GRADE_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = client.request("GET", path)
        if done(value):
            return value
        time.sleep(0.4)
    raise SystemExit(f"等待 {path} 超时")


def main() -> int:
    parser = argparse.ArgumentParser(description="为一个全新的数据目录建立可演示的真实数据世界")
    parser.add_argument("--data-dir", default=str(SERVER / "data" / "demo-world"))
    parser.add_argument("--password-file", default="")
    parser.add_argument("--keep-server", action="store_true", help="脚本结束后不停止服务")
    parser.add_argument("--admin-username", default="axis_admin", help="管理员账号；默认 axis_admin")
    parser.add_argument("--admin-password", default="", help="管理员口令；不填则随机生成。要求 12–128 位")
    parser.add_argument("--teacher-username", default="", help="教师账号；不填则用世界设定里的 chenning")
    parser.add_argument("--teacher-password", default="", help="教师口令；不填则随机生成。至少 8 位")
    parser.add_argument("--student-password", default="", help="两名学生共用口令；不填则各自随机生成。至少 8 位")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if data_dir.exists() and any(data_dir.iterdir()):
        raise SystemExit(f"{data_dir} 已存在且非空。请换一个目录，或先手动确认后删除。")
    data_dir.mkdir(parents=True, exist_ok=True)

    passwords = {
        "admin": secrets.token_urlsafe(18),
        "teacher": secrets.token_urlsafe(18),
        "lin": secrets.token_urlsafe(18),
        "zhou": secrets.token_urlsafe(18),
    }
    admin_username = args.admin_username.strip()
    teacher_username = args.teacher_username.strip() or WORLD["teacher"]["username"]
    teacher_password = args.teacher_password or passwords["teacher"]
    if args.admin_password:
        if not 12 <= len(args.admin_password) <= 128:
            raise SystemExit("管理员口令要求 12–128 位（平台策略，脚本不做放宽）")
        passwords["admin"] = args.admin_password
    if args.teacher_password:
        if len(args.teacher_password) < 8:
            raise SystemExit("教师口令至少 8 位")
        passwords["teacher"] = args.teacher_password
    if args.student_password:
        if len(args.student_password) < 8:
            raise SystemExit("学生口令至少 8 位")
        passwords["lin"] = passwords["zhou"] = args.student_password

    # 1. 建管理员（只在新库首次执行）
    env = dict(os.environ)
    env.update({"SKIP_ENV_FILE": "1", "APP_ENV": "test", "DATA_DIR": str(data_dir),
                "STUDY_DATA_DIR": str(data_dir)})
    sys.path.insert(0, str(SERVER))
    from app.init_admin import initialize_admin  # noqa: E402

    initialize_admin(data_dir / "platform.sqlite3", username=admin_username,
                     display_name="演示管理员", password=passwords["admin"])
    print(f"已建立管理员 {admin_username}")

    # 2. 起正式服务（非 demo 模式）
    port = free_port()
    base = _local_base(port)
    server_env = dict(env)
    server_env.update({"APP_ENV": "development", "DATA_DIR": str(data_dir), "STUDY_DATA_DIR": str(data_dir)})
    log_path = data_dir / "server.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        check_argv([sys.executable, "-m", "uvicorn", "app.main:app",
                    "--host", "127.0.0.1", "--port", str(port)]),
        cwd=str(SERVER), env=server_env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        wait_health(base, process, time.monotonic() + READY_TIMEOUT)
        print(f"服务已就绪：{base}")

        admin, teacher = Client(base), Client(base)
        lin, zhou = Client(base), Client(base)

        admin.request("POST", "/api/v1/auth/login", body={"username": admin_username, "password": passwords["admin"]})

        # 3. 教师：申请 → 管理员审核 → 登录
        application = admin.request("POST", "/api/v1/auth/teacher-applications", expect=202, body={
            "username": teacher_username,
            "display_name": WORLD["teacher"]["display_name"],
            "password": teacher_password,
        })
        admin.request("POST", f"/api/v1/auth/teacher-applications/{application['id']}/approve")
        teacher.request("POST", "/api/v1/auth/login", body={
            "username": teacher_username, "password": teacher_password, "role": "teacher",
        })
        teacher_user = teacher.request("GET", "/api/v1/auth/me")
        print(f"教师已就绪：{WORLD['teacher']['display_name']}")

        # 4. 课程：申请 → 审核
        course_application = teacher.request("POST", "/api/v1/course-applications", expect=202, body={
            "name": WORLD["course"]["name"], "term": WORLD["course"]["term"],
        })
        approved = admin.request("POST", f"/api/v1/course-applications/{course_application['id']}/review",
                                 body={"approve": True, "note": "演示世界"})
        course_id = approved["course_id"]

        # 5. 班级
        classroom = teacher.request("POST", f"/api/v1/courses/{course_id}/classes", expect=201, body={
            "name": WORLD["class"]["name"], "owner_teacher_id": teacher_user["id"],
        })
        class_id = classroom["id"]

        # 6. 两名学生：建号 → 用一次性码激活 → 登录
        students = {}
        for entry in WORLD["students"]:
            created = teacher.request("POST", "/api/v1/auth/students", expect=201, body={
                "username": entry["username"], "display_name": entry["display_name"],
                "course_id": course_id, "class_id": class_id,
            })
            client = lin if entry["key"] == "lin" else zhou
            client.request("POST", "/api/v1/auth/activate", expect=204, body={
                "code": created["activation_code"], "password": passwords[entry["key"]],
            })
            client.request("POST", "/api/v1/auth/login", body={
                "username": entry["username"], "password": passwords[entry["key"]], "role": "student",
            })
            user = client.request("GET", "/api/v1/auth/me")
            students[entry["key"]] = (client, user)
            print(f"学生已就绪：{entry['display_name']}")

        # 7. 教学材料：上传 → 确认可见块
        material = teacher.upload_material(
            f"/api/v1/courses/{course_id}/materials", "lesson.txt",
            WORLD["material"]["body"].encode("utf-8"),
            {"visibility": "shared", "license_code": "own", "rights_confirmed": "true",
             "allow_generation": "true", "allow_retrieval": "true",
             "allow_student_view": "true", "external_processing_allowed": "false"},
        )
        material_id = material["material"]["id"]

        blocks = teacher.request("GET", f"/api/v1/materials/{material_id}/blocks")
        block_items = blocks.get("items", blocks if isinstance(blocks, list) else [])
        block_ids = [item["id"] for item in block_items]
        if block_ids:
            teacher.request("POST", f"/api/v1/materials/{material_id}/confirm", body={
                "block_ids": block_ids, "expected_revision": material["material"]["revision"],
            })
        print(f"材料已就绪：{len(block_ids)} 个可见块")

        # 8. 题目：写入正式 LearningStore（与正式流程同一张表、同一套版本控制）
        from app.config import Settings
        from app.runtime import Runtime

        runtime = Runtime(Settings(data_dir=str(data_dir)))
        teacher_id = teacher_user["id"]
        refs, diagnostic_ref, future_refs = [], None, []
        for index, spec in enumerate(QUESTIONS):
            content = {
                "type": "single_choice", "stem": spec["stem"], "options": spec["options"],
                "answer": spec["answer"], "points": spec["points"],
                "family_id": f"demo-family-{index}", "explanation": spec["explanation"],
                "rubric": spec["rubric"], "external_processing_permission": False,
                "source_anchors": [{"block_id": block_ids[0], "file_id": material_id,
                                    "anchor": "lesson.txt#L1", "export_allowed": True}] if block_ids else [],
            }
            runtime.learning.create_question_version(teacher_id, spec["id"], 1, content,
                                                     status="approved", course_id=course_id)
            ref = {"question_id": spec["id"], "version": 1}
            if index < 2:
                refs.append(ref)
            elif index == 2:
                diagnostic_ref = ref
            else:
                future_refs.append(ref)
        print(f"题目已就绪：{len(QUESTIONS)} 道")

        # 9. 目标 → 发布第一轮
        goal = teacher.request("POST", "/api/v1/goals", expect=201, body={
            "request_id": "seed-goal", "title": WORLD["goal"]["title"],
            "description": WORLD["goal"]["description"], "course_id": course_id, "class_id": class_id,
        })
        student_ids = [students[key][1]["id"] for key in ("lin", "zhou")]
        teacher.request("POST", f"/api/v1/goals/{goal['id']}/rounds", expect=201, body={
            "request_id": "seed-round-1", "expected_version": goal["version"], "round_no": 1,
            "student_ids": student_ids, "question_versions": refs,
            "diagnostic_question_versions": [diagnostic_ref],
            "deadline_at": time.time() + 7 * 24 * 3600,
            "allow_late": True, "allow_retries": True, "allow_retry_after_deadline": True,
            "common_question_ratio": 100,
        })
        print(f"目标已发布：{WORLD['goal']['title']}（第 1 轮）")

        # 10. 两名学生各自走一遍：首诊 → 正式卷 → 保存 → 交卷
        answers_by_student = {
            "lin": {refs[0]["question_id"]: 0, refs[1]["question_id"]: 0},   # 林夏：两道都需要补
            "zhou": {refs[0]["question_id"]: 1, refs[1]["question_id"]: 0},  # 周予：去括号已会
        }
        task_ids = {}
        for key, (client, _user) in students.items():
            tasks = client.request("GET", "/api/v1/tasks")["items"]
            task_id = tasks[0]["id"]
            task_ids[key] = task_id

            task = client.request("GET", f"/api/v1/tasks/{task_id}")
            if task.get("diagnostic_required"):
                diagnostic = client.request("POST", f"/api/v1/tasks/{task_id}/diagnostics", expect=201, body={
                    "request_id": f"seed-{key}-diagnostic",
                    "expected_version": task["version"],
                })
                client.request("POST", f"/api/v1/diagnostics/{diagnostic['id']}/submit", body={
                    "request_id": f"seed-{key}-diagnostic-submit",
                    "answers": {diagnostic_ref["question_id"]: 0},
                    "unknown_question_ids": [],
                })

            lease = client.request("POST", f"/api/v1/tasks/{task_id}/edit-leases", body={
                "device_id": f"seed-{key}", "lease_seconds": 300,
            })
            draft = client.request("PUT", f"/api/v1/tasks/{task_id}/draft", body={
                "expected_version": lease["task_version"],
                "request_id": f"seed-{key}-draft",
                "lease_token": lease["lease_token"], "fence_token": lease["fence_token"],
                "answers": answers_by_student[key],
            })
            client.request("POST", f"/api/v1/tasks/{task_id}/submit", expect=202, body={
                "expected_version": draft["version"],
                "request_id": f"seed-{key}-submit",
                "lease_token": lease["lease_token"], "fence_token": lease["fence_token"],
            })
            print(f"{WORLD['students'][0 if key == 'lin' else 1]['display_name']} 已交卷")

        # 11. 等后台真实评卷
        for key, (client, _user) in students.items():
            task_id = task_ids[key]
            graded = poll(client, f"/api/v1/tasks/{task_id}",
                          lambda item: item["status"] in {"results_ready", "awaiting_manual", "closed"})
            attempt = (graded.get("attempts") or [{}])[0]
            print(f"{key} 评卷完成：{attempt.get('total_score')} 分，状态 {graded['status']}")

        # 12. 班级报告：先取源修订与规则版本，再生成（报告必须绑定冻结快照）
        try:
            rules = teacher.request("GET", "/api/v1/reports/rules")
            source = teacher.request(
                "GET",
                f"/api/v1/reports/source-revision?kind=class&course_id={course_id}&class_id={class_id}",
            )
            report = teacher.generate_report(
                "/api/v1/reports/generate",
                {
                    "scope": {"kind": "class", "course_id": course_id, "class_id": class_id},
                    "schema_version": rules["schema_version"],
                    "rule_version": rules["default_rule_version"],
                    "expected_source_revision": source["source_revision"],
                },
                "seed-class-report",
            )
            print(f"班级报告已生成：{report.get('report_id')}（源修订 {report.get('source_revision')}）")
        except urllib.error.HTTPError as error:
            print(f"班级报告未生成（{error.code}）：{error.read()[:200].decode('utf-8', 'replace')}")

        # 13. 输出凭据
        summary = {
            "base_url": base,
            "data_dir": str(data_dir),
            "course_id": course_id,
            "class_id": class_id,
            "goal_id": goal["id"],
            "accounts": {
                "admin": {"username": admin_username, "password": passwords["admin"]},
                "teacher": {"username": teacher_username, "password": teacher_password},
                "lin": {"username": WORLD["students"][0]["username"], "password": passwords["lin"]},
                "zhou": {"username": WORLD["students"][1]["username"], "password": passwords["zhou"]},
            },
        }
        if args.password_file:
            Path(args.password_file).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        print("\n=== 演示世界已就绪 ===")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print("\n请把这些口令只保存在本机，不要提交到仓库。")
    finally:
        if not args.keep_server:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            log.close()
            print("服务已停止。重新启动即可继续演示。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
