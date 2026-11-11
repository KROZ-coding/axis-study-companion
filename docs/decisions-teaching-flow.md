# 教学流程后端决策与接入说明

> 本文件记录已完成的演示教学后端接口及约束。2026-11-11 的后续产品目标另见[产品与前后端技术总方案](智慧学伴产品与前后端技术总方案.md)；新方案不代表本文件的API已经升级为正式账号和完整AI业务。

日期：2026-11-09。用户指定先执行后端重构；`web/` 仍运行旧样例页面，新任务界面尚未接入。

## 实现范围与状态

保留 FastAPI 单体、SQLite 和现有依赖。`main.py` 负责装配、同源保护、异常转换与静态资源；旧接口迁入 `routers/study/legacy.py`。`services/study/` 提供显式数据与范围、历史分析、报告、演示上下文及教学用例，`models/study.py`、`schemas/study.py` 定义业务对象与请求/响应。

教师建立不可变的历史学习回顾，再保存跟进草稿、确认发布。每名目标学生生成独立任务，可以开始、确认阅读、保存草稿和正式提交。教师反馈指向具体 attempt；要求重试后生成新的 attempt，原提交保留。

| 对象 | 状态与约束 |
| --- | --- |
| 学习回顾 | 冻结学生、知识点、日期范围、数据版本、历史证据及分析 |
| 跟进安排 | `draft → published → closed`，只有草稿可编辑 |
| 学生任务 | `not_started → in_progress → submitted → completed`；教师可由 `submitted` 转为 `needs_retry`，随后再次提交 |
| 阅读确认 | 学生自报已阅读，不表示理解或掌握 |
| 规则评分 | 复用 `grade_session()` 客观题规则，不传模型配置，不调用 `apply_grading_result()` |

发布冻结材料正文、题目、答案、解析、建议依据、回顾与内容版本。已发布安排不能直接修改，需要建立新安排。安排关闭后，学生不能开始、保存或提交；教师仍可确认已提交任务完成，但不能要求重试。

新 attempts 与历史诊断分开保存，不会更新固定历史诊断。任务完成、阅读确认、答对单题不代表掌握度提高。本轮没有自动将新提交纳入历史分析的入口。

## 本地演示身份

新接口前缀为 `/api/teaching`，每次请求必须明确身份：

```text
X-Demo-Role: teacher
```

或：

```text
X-Demo-Role: student
X-Demo-Student-Id: S07
```

可带 `X-Demo-Class-Id`，默认 `demo-math-7-1`；班级不匹配返回 403。教师身份不接受学生身份头，通过专用只读预览接口携带目标学生及任务。学生只能读取自己的分析和被分配任务，也只能保存/提交自己的任务。

这些头用于本地选择身份，任何本机调用者都能切换，**不是正式认证**。服务保持回环地址运行。旧 `/api/study/*` 仍是全量、含答案的本地兼容接口；正式账号部署前必须替换集中身份依赖，并移除或隔离旧接口。

## 接口清单

| 方法与路径（省略 `/api/teaching`） | 身份 | 用途 |
| --- | --- | --- |
| `GET /context` | 教师/学生 | 操作者、班级、范围、数据版本、样例标记 |
| `GET /content`、`GET /materials/{id}` | 教师/学生 | 内容目录及材料正文 |
| `GET /questions/{id}` | 教师/学生 | 教师可看答案；学生只有 `id/stem/options` |
| `GET /insights?topic=all&student_id=S07` | 教师/学生 | 范围内历史分析，学生固定为自己 |
| `GET /dashboard` | 教师 | 真实进行中安排和待反馈提交，无记录时为零 |
| `GET/POST /reviews`、`GET /reviews/{id}` | 教师 | 回顾列表、创建与冻结结果 |
| `GET/POST /followups`、`GET/PUT /followups/{id}` | 教师 | 安排列表、草稿及进展 |
| `POST /followups/{id}/publish`、`/close` | 教师 | 发布、关闭 |
| `GET /followups/{id}/preview/{student_id}` | 教师 | 指定学生任务只读预览 |
| `GET /tasks` | 学生 | 被分配任务，保留关闭后的历史 |
| `GET /tasks/{id}` | 教师/所属学生 | 草稿、进度、历次提交、反馈 |
| `POST /tasks/{id}/start` | 学生 | 开始任务 |
| `PUT /tasks/{id}/draft` | 学生 | 完整替换答案草稿与已阅读材料集合 |
| `POST /tasks/{id}/submit` | 学生 | 提交已保存草稿，规则评分 |
| `POST /tasks/{id}/feedback` | 教师 | 确认完成或要求重试并说明 |
| `GET /followups/{id}/report.json`、`/report.csv` | 教师 | 同一安排、回顾及版本的成果导出 |

学生首次提交前，任务不返回参考答案和解析。首次提交后可查解析与历次尝试；重试仍用原题，解析保持可查。当前每项安排聚焦一个知识点，可面向多名学生。

任务报告 JSON 含按安排学生及知识点收窄的 `historical_snapshot`，包含原始日期范围、规则和历史 records，发布后保持冻结；学生任务只下发该学生的依据摘要。CSV 携带日期、内容版本与证据编号，反馈列仅对应最新提交，并标明 `feedback_attempt_id`；全部历史提交与反馈保留在 JSON 中。

## 请求与冲突处理

所有写请求带 `request_id`（1–100 个字母、数字或 `_.:-`）。修改现有对象时还带整数 `expected_version`：发布/关闭用安排版本，开始/保存/提交/反馈用任务版本。前端使用每次成功响应中的新版本。

同一操作者、班级和请求编号的相同请求返回最初成功响应，不新增任务、attempt 或反馈，命中成功记录时也不再次保存。该响应可能为旧版本，恢复页面后再 GET 最新状态。编号用于不同路径/参数、过期版本或非法状态转换返回 409。失败操作不占编号，可原样重试。

草稿为完整替换，缺省 `answers={}`、`read_material_ids=[]` 会清空对应内容。正式提交不接收临时答案，先保存全部作答与指定材料阅读确认。选项从 0 开始，拒绝布尔、字符串、越界选项和任务外题目/材料。

保存失败返回 503，事务还原教学状态、版本、提交、反馈、重复请求记录及 dirty 标记。内存模式同样校验和回滚，但不能跨服务重启保留。

## 可执行 API 演示

按本地部署说明启动服务。需要隔离演示时，启动前将 `STUDY_DATA_DIR` 指向一个新的空目录；不覆盖正在使用的库。以下 PowerShell 使用虚构学生 S07 和知识点 K5：

```powershell
$base = 'http://127.0.0.1:8000/api/teaching'
$teacher = @{ 'X-Demo-Role' = 'teacher' }
$student = @{ 'X-Demo-Role' = 'student'; 'X-Demo-Student-Id' = 'S07' }
function Send-Teaching($method, $path, $headers, $body) {
    $json = $body | ConvertTo-Json -Depth 20
    Invoke-RestMethod -Method $method -Uri "$base$path" -Headers $headers `
        -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($json))
}
function New-RequestId { [guid]::NewGuid().ToString('N') }

# 查看依据并冻结回顾。
Invoke-RestMethod "$base/insights?student_id=S07&topic=T2" -Headers $teacher
$review = Send-Teaching POST '/reviews' $teacher @{
    request_id = (New-RequestId); title = '去括号学习回顾'; topic = 'T2'
    student_ids = @('S07'); knowledge_point_ids = @('K5')
}
$catalog = Invoke-RestMethod "$base/content" -Headers $teacher
$material = @($catalog.materials | Where-Object { $_.kp -eq 'K5' -and $_.type -eq '讲解' })[0]

# 保存草稿，核对后发布。
$followup = Send-Teaching POST '/followups' $teacher @{
    request_id = (New-RequestId); review_id = $review.id
    title = '去括号：检查括号内每一项'; goal = '检查分配律的使用'
    knowledge_point_id = 'K5'; student_ids = @('S07')
    material_ids = @($material.id); question_ids = @('P-501')
}
$followup = Send-Teaching POST "/followups/$($followup.id)/publish" $teacher @{
    request_id = (New-RequestId); expected_version = $followup.version
}
$preview = Invoke-RestMethod "$base/followups/$($followup.id)/preview/S07" -Headers $teacher
$task = $preview.task

# 学生保存，重新读取模拟刷新，然后正式提交。
$task = Send-Teaching PUT "/tasks/$($task.id)/draft" $student @{
    request_id = (New-RequestId); expected_version = $task.version
    answers = @{ 'P-501' = 1 }; read_material_ids = @($material.id)
}
$task = Invoke-RestMethod "$base/tasks/$($task.id)" -Headers $student
$task = Send-Teaching POST "/tasks/$($task.id)/submit" $student @{
    request_id = (New-RequestId); expected_version = $task.version
}

# 教师反馈，学生查阅，导出任务成果。
$task = Send-Teaching POST "/tasks/$($task.id)/feedback" $teacher @{
    request_id = (New-RequestId); expected_version = $task.version
    attempt_id = $task.attempts[-1].id; decision = 'completed'
    comment = '已查看本次作答。下次继续检查括号内每一项。'
}
Invoke-RestMethod "$base/tasks/$($task.id)" -Headers $student
Invoke-RestMethod "$base/followups/$($followup.id)/report.json" -Headers $teacher
```

要求重试时使用 `decision='needs_retry'` 和具体说明。学生重新保存并提交后产生新的 attempt，原作答与原反馈仍可查看。

## 存储、迁移与恢复

`StudyStore` 子类扩展上游快照、编码、状态读取/应用和恢复，复用 SQLite 全量压缩快照。`teaching_state.schema_version=1` 包含 `reviews/followups/tasks/attempts/feedback/idempotency/known_last_choices/migrations`，清空报告缓存不会删除教学事实。

旧 `reports['study-practice']` 仅迁为 `known_last_choices[student_id][practice_id]`，不补造时间、次数、教师安排。首次原始副本存于 `teaching_state.migrations['study-practice'].original`；重复迁移不覆盖新选择。需回退旧程序时先停服、另存当前 JSON/ZIP 备份，在备份副本中将 `original` 放回 `reports['study-practice']`，再用旧版本恢复工具验证，勿直接覆盖原库。旧 `store.json` 导入后保留 `.migrated`。

写入使用 `teaching_transaction()`，按 `_save_lock → lock` 持锁直至可靠保存并支持深拷贝回滚；读取使用 `teaching_snapshot()`。仍为单进程快照设计，只使用一个应用进程，不启动多个 Uvicorn worker。JSON/ZIP 备份恢复含教学状态，本轮未新增网页备份管理接口。

## 验证与限制

运行 `server/.venv/Scripts/python.exe -I server/tests/run_tests.py`。测试入口在导入应用前隔离临时目录、关闭模块级默认持久化并清空模型配置；持久化用例各自创建临时库。实际测试结果见本地部署说明。

本轮未改造前端，没有新任务页浏览器验收或桌面/手机截图。正式登录、通知、课表、赛事数据导入、外部模型和独立教学效果评测不在本次实现范围。

后续产品模型选择已于 2026-11-10 确认：教师回复、出题和审题使用 Poke API 的 `gpt-5.6-sol`（`high`），学生侧 AI 功能使用 `gpt-5.6-terra`（`high`）。配置与未接入边界见[模型分工与接入约定](模型分工与接入约定.md)。
