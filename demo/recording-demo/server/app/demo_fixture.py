"""Newly authored, fictional recording fixture; never used by the platform app."""
from __future__ import annotations

import secrets
import time
from uuid import uuid4

SOURCE = """七年级数学：从基础概念到一元一次方程（虚构演示课程）
本课程材料为本机录屏新编，不包含真实学生资料。
相反数之和为零。绝对值表示数轴上一个点到原点的距离，距离不能为负。负数位于原点左侧。
等式两边同时加减同一个数，或同时乘除同一个非零数，仍保持相等。解方程时先整理常数项，再把未知数的系数化为一；最后将结果代回原方程检查。
合并同类项要求字母和各字母的指数分别相同，只合并数字系数。单项式中的数字因数称为系数。
正方形四条边等长，周长由四条边的长度相加得到。
"""


def question(qid, kind, stem, answer, point, *, options=None, rubric=None, fmt=None):
    value = {"id": "recording-" + qid, "type": kind, "stem": stem, "answer": answer,
             "options": options or [], "points": 2, "difficulty": "easy", "knowledge_points": [point],
             "family_id": "recording-family-" + qid, "explanation": "依据已确认课程材料中的" + point + "进行检查。",
             "external_processing_permission": False, "provenance": {"kind": "recording_demo", "fictional": True},
             "rubric": rubric or []}
    if fmt:
        value["answer_format"] = fmt
    return value


QUESTIONS = [
    question("choice-opposite", "single_choice", "−7 的相反数是哪一个？", 0, "相反数", options=["7", "−7", "0", "1"]),
    question("choice-equation", "single_choice", "一元一次方程 3x = 12 的解是哪一个？", 0, "等式性质", options=["x = 4", "x = 3", "x = 12", "x = 9"]),
    question("choice-terms", "single_choice", "合并同类项：3a + 2a 的结果是哪一个？", 0, "同类项", options=["5a", "5a²", "6a", "a"]),
    question("judge-zero", "judge", "判断：零的绝对值是负数。", False, "绝对值"),
    question("judge-negative", "judge", "判断：所有负数都大于零。", False, "有理数大小"),
    question("judge-terms", "judge", "判断：a 与 a² 可以合并为一个同类项。", False, "同类项"),
    question("fill-absolute", "fill", "绝对值计算：|−8| = ____。", "8", "绝对值", fmt="math"),
    question("fill-equation", "fill", "等式求解：x + 5 = 9，则 x = ____。", "4", "等式性质", fmt="math"),
    question("fill-coefficient", "fill", "单项式中的数字因数叫作这个单项式的____。", "系数", "单项式", fmt="text"),
    question("text-negative", "text", "说理题：解释为什么 −3 小于 0。", "−3 是负数，负数小于零，因此 −3 < 0。", "有理数大小",
             rubric=[{"point": "指出 −3 是负数", "score_units": 100}, {"point": "用数轴顺序或负数定义解释小于零", "score_units": 100}]),
    question("text-equation", "text", "过程题：写出解方程 2x + 3 = 11 的主要步骤并检验。", "两边减去3得2x=8；两边除以2得x=4；代入2×4+3=11，成立。", "等式性质",
             rubric=[{"point": "两边减去 3 得到 2x=8", "score_units": 60}, {"point": "两边除以 2 得到 x=4", "score_units": 80}, {"point": "代入原式检验", "score_units": 60}]),
    question("text-square", "text", "几何表达：边长为 a 的正方形周长如何表示？说明理由。", "正方形有四条相等的边，周长为a+a+a+a=4a。", "周长",
             rubric=[{"point": "指出四条边等长", "score_units": 100}, {"point": "四条边相加得到 4a", "score_units": 100}]),
]
FORMAL_IDS = tuple("recording-" + suffix for suffix in ("choice-equation", "judge-zero", "fill-equation", "text-equation"))

DIAGNOSTICS = [
    question("diagnostic-add", "single_choice", "独立首诊：由 x + 2 = 7，可以在等式两边同时怎样操作？", 1, "等式性质", options=["加 2", "减 2", "乘 2"]),
    question("diagnostic-distance", "judge", "独立首诊：数轴上一个点到原点的距离可以是负数。", False, "绝对值"),
]


def seed_world(runtime):
    from .services.material_parser import parse_material
    from .utils.security import hash_password
    users, credentials = {}, {}
    for key, name, role in (("teacher", "陈宁", "teacher"), ("lin", "林夏", "student"), ("zhou", "周予", "student")):
        password = secrets.token_urlsafe(24)
        users[key] = runtime.platform.create_user("recording_" + key, name, hash_password(password), role)
        credentials[key] = {"username": users[key]["username"], "password": password, "role": role}
    teacher_id = users["teacher"]["id"]
    course = runtime.platform.create_course("七年级数学 · 基础与方程", "2026 秋 · 虚构演示", teacher_id)
    classroom = runtime.platform.create_class(course["id"], "七（1）班 · 演示", owner_teacher_id=teacher_id)
    for key in ("lin", "zhou"):
        runtime.platform.create_course_membership(course["id"], users[key]["id"])
        runtime.platform.create_class_membership(classroom["id"], users[key]["id"])
    content = SOURCE.encode("utf-8")
    material = runtime.content.import_material(
        course_id=course["id"], owner_id=teacher_id, filename="recording-lesson.txt", content=content,
        parsed=parse_material("recording-lesson.txt", content), title="基础概念与方程 · 虚构演示讲义",
        class_id=classroom["id"], visibility="shared", license_code="own", rights_confirmed=True,
        allow_generation=True, allow_retrieval=True, allow_student_view=True, external_processing_allowed=False,
    )
    blocks = runtime.content.list_blocks(material["id"])
    block_ids = [block["id"] for block in blocks]
    runtime.content.confirm_blocks(material["id"], block_ids, actor_id=teacher_id,
        expected_revision=material["revision"], public_block_ids=block_ids, public_content_attested=True,
        course_owner_confirmation=True, note="演示教师确认新编虚构课程材料，无试卷答案。")
    for spec in QUESTIONS + DIAGNOSTICS:
        value = {**spec, "source_anchors": [{"block_id": blocks[0]["id"], "file_id": material["id"],
                  "anchor": blocks[0]["anchor"], "export_allowed": True}]}
        runtime.learning.create_question_version(teacher_id, spec["id"], 1, value, status="approved", course_id=course["id"])
    return {"users": users, "credentials": credentials, "course_id": course["id"], "class_id": classroom["id"],
            "material_id": material["id"], "round_number": 0, "rounds": []}


def new_round(runtime, world):
    number = world["round_number"] + 1
    teacher = world["users"]["teacher"]["id"]
    key = uuid4().hex
    # These are the same transactional store commands used by the platform routes.
    with runtime.learning.transaction():
        goal = runtime.learning.create_goal(teacher, f"基础概念与方程 · 第 {number} 次演示",
            "先完成 2 道独立首诊，再完成 4 道正式练习，涵盖单选、判断、填空与简答；按保存事实形成学习证据。",
            course_id=world["course_id"], class_id=world["class_id"], request_id="demo-goal-" + key)
        published = runtime.learning.publish_round(teacher, goal["id"], expected_version=goal["version"],
            request_id="demo-publish-" + key, round_no=1,
            student_ids=[world["users"][key]["id"] for key in ("lin", "zhou")],
            question_versions=[{"question_id": qid, "version": 1} for qid in FORMAL_IDS],
            diagnostic_question_versions=[{"question_id": q["id"], "version": 1} for q in DIAGNOSTICS],
            deadline_at=time.time() + 30 * 86400, allow_late=True, allow_retries=True, allow_retry_after_deadline=True)
    world.update(goal_id=goal["id"], round_number=number,
                 task_ids=dict(zip(("lin", "zhou"), published["task_ids"])))
    world["rounds"].append({"goal_id": goal["id"], "round_number": number, "task_ids": world["task_ids"]})
    return published


def simulate_rubric(question, answer):
    """Explicit recording-only rubric checks, not a general subjective grader."""
    import re
    import unicodedata
    from .services.math_grading import validate_ai_rubric
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(answer))).replace("−", "-")
    qid = question["id"]
    if qid == "recording-text-negative":
        checks = ["负数" in text and "-3" in text,
                  ("小于" in text or "<0" in text or "左" in text) and ("负数" in text or "数轴" in text)]
    elif qid == "recording-text-equation":
        checks = ["2x=8" in text, re.search(r"(?<![0-9A-Za-z])x=4(?![0-9])", text) is not None,
                  any(term in text for term in ("代入", "检验")) and ("2×4+3=11" in text or "2*4+3=11" in text or "左右相等" in text)]
    elif qid == "recording-text-square":
        checks = [any(term in text for term in ("四", "4条")) and any(term in text for term in ("相等", "等长", "同长")), "4a" in text or "4*a" in text]
    else:
        raise ValueError("本机演示规则仅覆盖预置三道简答题，其余题目需要教师评分")
    points = [{"point": row["point"], "score_units": row["score_units"] if matched else 0,
               "why": "演示规则识别到该步骤" if matched else "演示规则未识别到该步骤，教师可复核"}
              for row, matched in zip(question["rubric"], checks)]
    result = validate_ai_rubric(question, {"points": points, "why": "本机演示规则评分（模拟），非真实模型评分；不确定或表达不同可由教师复核。"})
    return {**result, "source": "simulation", "simulation": True, "rule_version": "recording-rubric-v1"}


def hint_text(question, message, step):
    """Only public stem/concepts are used; neither reference answers nor options."""
    stem = question["stem"]
    if "相反数" in stem:
        steps = ["先想一想：一个数与它的相反数相加应满足什么关系？", "在数轴上，找到与原数到原点距离相同、方向相反的位置。", "用你选的数与原数相加进行检查，先不要只凭符号猜。"]
    elif "绝对值" in stem or "距离" in stem:
        steps = ["把绝对值符号看作数轴上到原点的距离。", "先判断距离可能取什么范围，再比较题干陈述或填写结果。", "画一条简短数轴，把原数的位置和原点标出来检查。"]
    elif "同类项" in stem:
        steps = ["分别圈出每项里的字母与指数。", "只有字母和相应指数都相同时，才能合并数字系数。", "合并后再检查字母部分是否被你无意改变。"]
    elif "单项式" in stem:
        steps = ["把一个单项式拆为数字因数与字母因数。", "回看讲义对“数字因数”的术语定义。", "用一个你自己写的单项式核对这个概念。"]
    elif "正方形" in stem:
        steps = ["先在图上标出四条边，每条边的长度如何表示？", "周长是沿外边界走一圈的总长度，可以先写成相加的式子。", "最后再合并同类项，并解释为什么可以合并。"]
    elif "负数" in stem or "小于" in stem:
        steps = ["先在数轴上标出原点和题目中的数。", "比较左右位置，并回忆数轴的哪一侧表示更小的数。", "把“位置关系”写成一句有依据的判断，不只写结论。"]
    else:
        steps = ["先圈出未知数项与常数项，确定下一步要消去哪一项。", "每一步都对等式两边做相同的运算；先整理常数，再处理未知数的系数。", "保留中间式，最后把你求得的值代回原方程检查左右是否相等。"]
    if step <= 3:
        text = f"第 {step} 步提示：{steps[step - 1]}"
    else:
        text = "复习提示：回看你已经写下的条件、变形依据和检查步骤，找出尚未说明的一处；请用自己的话补全理由。不会直接给出答案。"
    if any(word in message for word in ("答案", "直接", "选哪个")):
        text = "我会帮助你继续推理，最终结果由你独立完成。\n\n" + text
    return text + "\n\n请把这一步的尝试补充到作答区。以上为本机模拟辅导。"


def chat_text(message):
    if any(term in message for term in ("方程", "移项", "等式")):
        text = "解方程可以分成三步：整理常数项、把未知数系数化为一、代回原式检验。每次变形都写清两边做了相同的运算。正在练习的题，请先在作答区保存尝试，再使用该题的提示。"
    elif any(term in message for term in ("计划", "复习", "学习")):
        text = "建议先用两道独立首诊了解起点，再完成正式练习。每做完一道题写下判断依据；遇到卡点先保存已有步骤，最后根据评分与求助记录安排复习。"
    else:
        text = "可以先告诉我你正在学的知识点，或你已经尝试过的步骤。把已知条件、需要求出的量和卡住的位置分开写，会更容易找到下一步。正式练习中的具体题，请从题目旁进入提示。"
    return text + "\n\n以上为本机模拟学伴回复，未调用外部模型。"
