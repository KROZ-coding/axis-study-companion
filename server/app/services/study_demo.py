from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import Any

AS_OF = date(2026, 11, 3)
MIN_N = 3
RULE = "有效记录少于 3 条显示“依据不足”；正确率低于 50% 为薄弱，50% 至 80% 为待巩固，80% 及以上为已掌握。"

TOPICS = [
    {"id": "T1", "name": "有理数"},
    {"id": "T2", "name": "整式的加减"},
    {"id": "T3", "name": "一元一次方程"},
]
KPS = [
    {"id": "K1", "topic": "T1", "name": "有理数加减", "ref": "七上 1.3"},
    {"id": "K2", "topic": "T1", "name": "有理数乘除", "ref": "七上 1.4"},
    {"id": "K3", "topic": "T1", "name": "乘方与运算顺序", "ref": "七上 1.5"},
    {"id": "K4", "topic": "T2", "name": "合并同类项", "ref": "七上 2.2"},
    {"id": "K5", "topic": "T2", "name": "去括号", "ref": "七上 2.2"},
    {"id": "K6", "topic": "T3", "name": "移项", "ref": "七上 3.2"},
    {"id": "K7", "topic": "T3", "name": "去分母", "ref": "七上 3.3"},
    {"id": "K8", "topic": "T3", "name": "列方程解应用题", "ref": "七上 3.4"},
]
ITEMS = [
    {"id": "Q-101", "kp": "K1", "error": "异号相加符号判断错", "material": "异号相加：先定符号，再算绝对值之差", "desc": "异号两数相加时，结果的符号取错", "stem": "计算：(−7) + 4 − (−5)", "steps": ["(−7) + 4 − (−5)", "= 3 − (−5)", "= 8"], "wrong": 1, "fix": ["= −3 + 5", "= 2"], "why": "异号两数相加，取绝对值较大的数的符号。(−7) + 4 应得 −3，不是 3。"},
    {"id": "Q-201", "kp": "K2", "error": "除法未取倒数", "material": "除以一个数，等于乘它的倒数", "desc": "除法改成乘法时没有取倒数", "stem": "计算：(−12) ÷ (−3/4)", "steps": ["(−12) ÷ (−3/4)", "= (−12) × (−3/4)", "= 9"], "wrong": 1, "fix": ["= (−12) × (−4/3)", "= 16"], "why": "除以 −3/4 要改成乘它的倒数 −4/3，不能只把 ÷ 换成 ×。"},
    {"id": "Q-301", "kp": "K3", "error": "混淆 −a² 与 (−a)²", "material": "−2² 与 (−2)²：先看底数是谁", "desc": "把 −a² 当成 (−a)² 计算", "stem": "计算：−2² + (−3)²", "steps": ["−2² + (−3)²", "= 4 + 9", "= 13"], "wrong": 1, "fix": ["= −4 + 9", "= 5"], "why": "−2² 的底数是 2，结果是 −4；(−3)² 的底数是 −3，结果是 9。"},
    {"id": "Q-401", "kp": "K4", "error": "把非同类项合并", "material": "同类项：字母相同，指数也相同", "desc": "把字母不同的项当作同类项合并", "stem": "化简：3a + 2b − a + 4ab", "steps": ["3a + 2b − a + 4ab", "= 2a + 6ab"], "wrong": 1, "fix": ["= 2a + 2b + 4ab"], "why": "2b 与 4ab 所含字母不同，不是同类项，不能合并。"},
    {"id": "Q-501", "kp": "K5", "error": "去括号漏乘", "material": "去括号：括号外的数要乘遍每一项", "desc": "括号外的数只乘了括号里的第一项", "stem": "解方程：3(x − 2) = 2x + 1", "steps": ["3(x − 2) = 2x + 1", "3x − 2 = 2x + 1", "x = 3"], "wrong": 1, "fix": ["3x − 6 = 2x + 1", "x = 7"], "why": "3 要同时乘 x 和 −2，得 3x − 6；这里只乘了第一项。"},
    {"id": "Q-502", "kp": "K5", "error": "括号前负号未变号", "material": "括号前是负号，括号里每项都变号", "desc": "括号前是负号时，括号里的项没有全部变号", "stem": "化简：5 − (x − 3)", "steps": ["5 − (x − 3)", "= 5 − x − 3", "= 2 − x"], "wrong": 1, "fix": ["= 5 − x + 3", "= 8 − x"], "why": "括号前是负号，去括号后 −3 要变成 +3。"},
    {"id": "Q-601", "kp": "K6", "error": "移项未变号", "material": "移项要变号：从等号一边移到另一边", "desc": "把项移到等号另一边时没有变号", "stem": "解方程：5x − 3 = 2x + 9", "steps": ["5x − 2x = 9 − 3", "3x = 6", "x = 2"], "wrong": 0, "fix": ["5x − 2x = 9 + 3", "3x = 12", "x = 4"], "why": "−3 从左边移到右边要变成 +3。"},
    {"id": "Q-701", "kp": "K7", "error": "漏乘不含分母的项", "material": "去分母：每一项都乘最简公分母", "desc": "去分母时，没有分母的项漏乘了公分母", "stem": "解方程：(x + 1)/2 − 1 = x/3", "steps": ["3(x + 1) − 1 = 2x", "3x + 2 = 2x", "x = −2"], "wrong": 0, "fix": ["3(x + 1) − 6 = 2x", "3x − 3 = 2x", "x = 3"], "why": "两边同乘 6 时，−1 也要乘 6，得 −6。"},
    {"id": "Q-801", "kp": "K8", "error": "等量关系找错", "material": "找等量关系：先用一句话写出相等", "desc": "列方程时只用了部分数量关系", "stem": "某班 40 人，男生比女生多 4 人。女生有多少人？", "steps": ["设女生 x 人", "x + 4 = 40", "x = 36"], "wrong": 1, "fix": ["x + (x + 4) = 40", "x = 18"], "why": "全班人数 = 女生 + 男生，男生是 x + 4，不能只写 x + 4 = 40。"},
]
PRACTICE = {
    "K1": {"id": "P-101", "stem": "(−9) + 5 = ？", "options": ["−14", "−4", "4", "14"], "answer": 1, "why": "异号相加：符号随绝对值较大的 −9，再算 9 − 5 = 4，结果是 −4。"},
    "K2": {"id": "P-201", "stem": "6 ÷ (−2/3) = ？", "options": ["−4", "−9", "9", "4"], "answer": 1, "why": "除以 −2/3 等于乘 −3/2：6 × (−3/2) = −9。"},
    "K3": {"id": "P-301", "stem": "−3² 的值是？", "options": ["9", "−9", "6", "−6"], "answer": 1, "why": "底数是 3，先算 3² = 9，再取相反数，得 −9。"},
    "K4": {"id": "P-401", "stem": "下列合并同类项正确的是？", "options": ["2x + 3y = 5xy", "5a² − 3a² = 2", "4m − m = 3m", "x² + x = x³"], "answer": 2, "why": "只有 4m 与 m 是同类项，4m − m = 3m。"},
    "K5": {"id": "P-501", "stem": "解方程 2(x + 3) = x + 10，去括号后正确的是？", "options": ["2x + 3 = x + 10", "2x + 6 = x + 10", "2x + 5 = x + 10", "x + 6 = x + 10"], "answer": 1, "why": "2 要乘 x，也要乘 3，得 2x + 6。继续解得 x = 4。"},
    "K6": {"id": "P-601", "stem": "方程 4x + 2 = x − 7 移项正确的是？", "options": ["4x + x = −7 − 2", "4x − x = −7 − 2", "4x − x = −7 + 2", "4x − x = 7 − 2"], "answer": 1, "why": "x 移到左边变成 −x，+2 移到右边变成 −2。"},
    "K7": {"id": "P-701", "stem": "方程 x/2 − 1 = x/3 两边同乘 6，得？", "options": ["3x − 1 = 2x", "3x − 6 = 2x", "3x − 6 = x", "x − 6 = 2x"], "answer": 1, "why": "每一项都乘 6，−1 也要变成 −6。"},
    "K8": {"id": "P-801", "stem": "3 支笔和 2 本本子共 16 元，本子每本 2 元。设笔每支 x 元，方程是？", "options": ["3x + 2 = 16", "3x + 4 = 16", "3x = 16 + 4", "2x + 3 = 16"], "answer": 1, "why": "2 本本子共 4 元，所以 3x + 4 = 16。"},
}

STATUS = {
    "weak": {"label": "薄弱", "rank": 0},
    "shaky": {"label": "待巩固", "rank": 1},
    "none": {"label": "依据不足", "rank": 2},
    "ok": {"label": "已掌握", "rank": 3},
}


def _status(n: int, right: int) -> str:
    if n < MIN_N:
        return "none"
    rate = right / n
    return "weak" if rate < 0.5 else "shaky" if rate < 0.8 else "ok"


def _make_materials() -> list[dict[str, Any]]:
    items_by_kp: dict[str, list[dict[str, Any]]] = {kp["id"]: [] for kp in KPS}
    for item in ITEMS:
        items_by_kp[item["kp"]].append(item)
    materials: list[dict[str, Any]] = []

    def add(**values: Any) -> None:
        materials.append({"id": f"M-{len(materials) + 1:02d}", "forItem": None, **values})

    for kp in KPS:
        for item in items_by_kp[kp["id"]]:
            source = f"样例教材 {kp['ref']}"
            add(kp=kp["id"], topic=kp["topic"], type="讲解", title=item["material"], source=source,
                forItem=item["id"], body=[item["desc"] + "。", item["why"], "对照：" + item["steps"][item["wrong"]] + "　→　" + item["fix"][0]])
            add(kp=kp["id"], topic=kp["topic"], type="例题", title="例题：" + item["stem"], source=source,
                forItem=item["id"], body=[item["stem"], "正确过程：" + "　".join(item["fix"])])
        for level, count in (("基础", 6), ("变式", 5), ("综合", 4)):
            add(kp=kp["id"], topic=kp["topic"], type="练习", title=f"{kp['name']} · {level}练习", source=f"样例教材 {kp['ref']}",
                body=[f"围绕「{kp['name']}」的{level}练习，共 {count} 题。", "第 1 题：" + PRACTICE[kp["id"]]["stem"]])
        add(kp=kp["id"], topic=kp["topic"], type="微课", title=f"{kp['name']} · 微课", source=f"样例教材 {kp['ref']}",
            body=["讲解视频，约 6 分钟。样例只登记条目，不附视频文件。"])
        add(kp=kp["id"], topic=kp["topic"], type="易错", title=f"{kp['name']} · 易错整理", source=f"样例教材 {kp['ref']}",
            body=[f"{item['error']}：{item['desc']}。" for item in items_by_kp[kp["id"]]])
    for index, topic in enumerate(TOPICS):
        add(kp=None, topic=topic["id"], type="小结", title=topic["name"] + " · 单元小结", source=f"样例教材 七上第{'一二三'[index]}章",
            body=["本单元知识点：" + "、".join(kp["name"] for kp in KPS if kp["topic"] == topic["id"]) + "。"])
    return materials


def build_sample() -> dict[str, Any]:
    students = [{"id": f"S{index:02d}", "name": f"学生{index:02d}"} for index in range(1, 51)]
    item_by_kp: dict[str, list[dict[str, Any]]] = {kp["id"]: [] for kp in KPS}
    for item in ITEMS:
        item_by_kp[item["kp"]].append(item)

    records: list[dict[str, Any]] = []
    for student_index, student in enumerate(students, start=1):
        if student["id"] == "S07":
            continue
        for kp_index, kp in enumerate(KPS, start=1):
            pattern = (student_index * 7 + kp_index * 3) % 10
            status = "weak" if pattern < 2 else "shaky" if pattern < 4 else "none" if pattern < 6 else "ok"
            if status == "weak":
                count, right = 4, 1
            elif status == "shaky":
                count, right = 4, 2
            elif status == "none":
                count, right = (student_index + kp_index) % 3, 0
            else:
                count, right = 3, 3
            candidates = item_by_kp[kp["id"]]
            for attempt in range(count):
                correct = attempt < right
                item = candidates[(student_index + kp_index + attempt + (0 if correct else 1)) % len(candidates)]
                day_offset = (student_index * 5 + kp_index * 3 + attempt * 7) % 27
                records.append({
                    "sid": student["id"], "kp": kp["id"], "item": item["id"], "correct": correct,
                    "date": (date(2026, 10, 8) + timedelta(days=day_offset)).isoformat(),
                })
    for kp_id, count, right in (("K1", 2, 2), ("K2", 2, 1), ("K3", 1, 1), ("K6", 3, 3), ("K7", 1, 1), ("K8", 1, 0)):
        for attempt in range(count):
            records.append({"sid": "S07", "kp": kp_id, "item": item_by_kp[kp_id][0]["id"], "correct": attempt < right,
                            "date": (date(2026, 10, 8) + timedelta(days=attempt * 7)).isoformat()})
    for item, correct, day in (("Q-502", True, 4), ("Q-501", False, 7), ("Q-502", False, 13), ("Q-501", True, 16), ("Q-501", False, 21), ("Q-501", False, 26)):
        records.append({"sid": "S07", "kp": "K5", "item": item, "correct": correct,
                        "date": (date(2026, 10, 8) + timedelta(days=day)).isoformat()})
    records.sort(key=lambda row: (row["date"], row["sid"]))
    duplicate_count = max(1, round(len(records) * 0.02))
    raw_records = records + [dict(records[index % len(records)]) for index in range(duplicate_count)]
    seen: set[tuple[str, str, str, bool]] = set()
    unique_records = []
    for record in raw_records:
        key = (record["sid"], record["item"], record["date"], record["correct"])
        if key not in seen:
            seen.add(key)
            unique_records.append(record)
    records = unique_records
    for index, record in enumerate(records, start=1):
        record["id"] = f"R-{index:04d}"

    raw_count = len(raw_records)
    records_by_student_kp: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        records_by_student_kp.setdefault((record["sid"], record["kp"]), []).append(record)

    stats: dict[str, dict[str, dict[str, Any]]] = {}
    for student in students:
        stats[student["id"]] = {}
        for kp in KPS:
            rows = records_by_student_kp.get((student["id"], kp["id"]), [])
            wrongs = [row for row in rows if not row["correct"]]
            counts = Counter(row["item"] for row in wrongs)
            top_item = max(counts, key=lambda item_id: (counts[item_id], max(i for i, row in enumerate(wrongs) if row["item"] == item_id))) if counts else None
            stats[student["id"]][kp["id"]] = {
                "sid": student["id"], "kp": kp["id"], "n": len(rows), "right": sum(row["correct"] for row in rows),
                "status": _status(len(rows), sum(row["correct"] for row in rows)), "records": rows, "wrongs": wrongs,
                "topItem": top_item, "topCount": counts[top_item] if top_item else 0, "lastWrong": wrongs[-1] if wrongs else None,
            }

    materials = _make_materials()
    material_by_item = {item["forItem"]: item for item in materials if item["type"] == "讲解" and item["forItem"]}
    material_by_kp = {
        kp["id"]: next(material for material in materials if material["kp"] == kp["id"] and material["type"] == "练习")
        for kp in KPS
    }
    suggestions: dict[str, dict[str, dict[str, Any]]] = {}
    recommended: dict[str, list[str]] = {}
    for student in students:
        suggestions[student["id"]] = {}
        for kp in KPS:
            stat = stats[student["id"]][kp["id"]]
            suggestion = material_by_item.get(stat["topItem"]) or material_by_kp[kp["id"]]
            suggestions[student["id"]][kp["id"]] = suggestion
        best = _focus(stats[student["id"]], [kp["id"] for kp in KPS])
        if best["focus"]:
            material_id = suggestions[student["id"]][best["focus"]["kp"]]["id"]
            recommended.setdefault(material_id, []).append(student["id"])

    return {
        "className": "七年级（样例）1班", "me": "S07", "asOf": AS_OF.isoformat(), "minN": MIN_N, "rule": RULE,
        "topics": TOPICS, "kps": KPS, "items": ITEMS, "practice": PRACTICE, "students": students,
        "records": records, "materials": materials, "stats": stats, "suggestions": suggestions,
        "recommended": recommended, "status": STATUS, "rawCount": raw_count,
        "dupRemoved": raw_count - len(records), "sampleLabel": "虚构样例数据",
    }


def _focus(student_stats: dict[str, dict[str, Any]], kp_ids: list[str]) -> dict[str, Any]:
    stats = [student_stats[kp_id] for kp_id in kp_ids]

    def rate(stat: dict[str, Any]) -> float:
        return stat["right"] / stat["n"] if stat["n"] else 1.0

    need = sorted(
        (stat for stat in stats if stat["status"] in {"weak", "shaky"}),
        key=lambda stat: (STATUS[stat["status"]]["rank"], rate(stat), -stat["n"]),
    )
    none = [stat for stat in stats if stat["status"] == "none"]
    overall = need[0]["status"] if need else "none" if none else "ok"
    return {"focus": need[0] if need else None, "need": need, "none": none, "stats": stats, "overall": overall}


def student_report(sid: str, kp_ids: list[str] | None = None) -> dict[str, Any]:
    data = SAMPLE
    if sid not in data["stats"]:
        raise KeyError(sid)
    selected = kp_ids or [kp["id"] for kp in data["kps"]]
    return _focus(data["stats"][sid], selected)


SAMPLE = build_sample()
