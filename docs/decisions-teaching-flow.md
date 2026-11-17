# 教学流程后端决策与接入说明

> 本文件主体保留 **2026-11-09 演示教学后端** 的接口及约束，`/api/teaching`、演示身份头和 `teaching_state.schema_version=1` 均为历史实现，不改写为正式 `/api/v1` 或 schema2。2026-11-11 的后续产品目标见[产品与前后端技术总方案](智慧学伴产品与前后端技术总方案.md)；2026-11-16 第四阶段正式实现单列于文末，当前接口见[正式后端接入说明](decisions-backend-platform.md)。

历史日期：2026-11-09。以下至“验证与限制”的“本轮／当前／新接口”均指该次演示重构：用户指定先执行后端重构；当时 `web/` 仍运行旧样例页面，新任务界面尚未接入。

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

2026-11-09 当次未改造前端，没有新任务页浏览器验收或桌面/手机截图。正式登录、通知、课表、赛事数据导入、外部模型和独立教学效果评测不在当次演示实现范围；此历史限制不代表后续正式平台的当前状态。

历史模型选择（2026-11-10）：当时教师回复、出题和审题使用 Poke API 的 `gpt-5.6-sol`（`high`），学生侧 AI 功能使用 `gpt-5.6-terra`（`high`）。此处保留当时决策，不作为当前学生模型配置。

2026-11-15 变更：按用户明确要求，学生学伴 `student`、`hint` 用途改为 `gpt-5.6-sol/high`，教师仍使用 Sol/high；评分 `grading`、摘要 `summary` 保留 Terra/high，必须显式配置各自通道，不再回落到 `student`。该次通过隔离测试，未执行真实供应商联调；配置与验证边界见[模型分工与接入约定](模型分工与接入约定.md)。

## 2026-11-16 第四阶段正式教学链路

本节记录2026-11-16落盘、2026-11-17收尾核对的正式 `/api/v1` 实现，与上述演示接口分开。生产代码通过 Python **564/564**、Node **406/406**；验收器收尾专项 **8/8** 通过，最终独立 HTTP **8/8** 阶段、**330** 次请求通过。上述全量为提示取消读取门禁修复后的重跑。详细证据、GUI 范围和未覆盖项见[验证记录与边界](验证记录与边界.md)，历史演示验证不计入当前验收。

1. **冻结目标报告总结**：教师选择包含全体学生、全轮次证据的目标报告冻结修订，程序按原规则复算 `facts`；Terra/high 的 `summary` 只选择有限解释码。未知／改写数字、遗漏／新增学生、未知或跨学生证据、低证据强断言均拒绝，服务器固定模板生成可见文字。目前只能解释目标级证据，未定位具体知识点，不称自由 AI 诊断。总结为独立 `teaching_insight` 领域，不复用学伴 `response`；幂等、`generation` 与当前权限重查约束创建、重放和结果采用，状态与通知 outbox 同事务保存。总结领域仅 `failed` 可恢复，并须通过费用／结果核对和明确确认，取消不复活；active 管理员经完整资源绑定校验仅能访问 jobs 元数据与对账，不获得正文权限。
2. **教师确认下一轮**：从已审核题库确定性配卷，教师编辑按 CAS 保存，核对完整预览并 `confirm` 后发布到原目标 `max(round_no)+1`。排除已使用题族，前轮完成且评分齐备才推进。有限持续计划由教师确认题池、轮数、间隔和截止；缺题、未完成、待评分或撤权则暂停，不自动造题，也不把模型解释当作自动发布授权。
3. **正式题两级受限提示**：Sol/high 使用独立 `hint` purpose，只对服务端已保存合法尝试、当前 cycle／draft hash 绑定一致的正式当前题开放。模型仅返回单字段 `action_id` JSON，可见内容来自服务器固定模板，无 raw 输出或 `delta` 公开；无外发许可、坏输出、预算／配置不可用等回退固定引导。首诊仍独立，`unknown` 与实际答案互斥，自由聊天权限不扩大。
4. **帮助证据与恢复**：只计服务端 `source: restricted_hint`、`status: validated_delivered` 且确定事件 ID／作答 cycle 匹配的帮助；`requested`、`validated`、`fallback` 不计，旧无 `status` help 保守兼容。未知调用与 `cost_pending` 保留待核对，核对完成后仍须明确确认恢复。hint 在已付费的成功 call 或安全 checkpoint 持久化后，后续交付写入发生 `sqlite3.OperationalError` 时，队列可为 `failed`，领域回答保留 `streaming`／`completed`；确认恢复原作业只复用原成功 call 的结果或 checkpoint，不新建付费调用、不重复扣费。已 `completed` 的提示沿用原回答绑定补齐交付记录；恢复重查权限及作答绑定，交付确认前隐藏提示，取消／撤权阻断采用。此存储错误保留规则限于该 hint 路径，不推广为所有异常均可恢复。
5. **正式作答保存**：草稿700ms防抖、4s最长等待，串行写入且未决操作使用同一请求重放；租约／CAS、IME 组合输入和交卷结果未知保护已落盘。本地草稿缓存仅在内存中，服务端已保存事实仍为权威。
6. **学生恢复入口与取消**：`web/prototype/app.js` 允许 `intent: hint`、回答为 `streaming`／`completed` 且 `job.status: failed` 时显示确认恢复；`cost_pending`、`reconciliation_required`、`cancel_requested` 任一为真，或回答自身为 `failed`／`cancelled`／`revoked` 时不显示也不执行。非 hint 保持原有暂停条件和 `awaiting_confirmation` 恢复规则。用户明确确认后以 `confirm_paid: true` 恢复原作业，再读取原回答，不新发消息。明确取消 `failed` 作业会设置 `cancel_requested: true`／队列 `cancelled`，公共 resume 返回 HTTP 409（`cancelled_job`）；已发生费用保留。已 `completed` 的领域结果保留，取消仍阻断提示投影与交付采用；公共取消后禁止恢复的规则不代表底层 `JobQueue.retry` 禁止所有取消态重试。

正式平台仍为六库：reports schema2 保存 `teaching_insights`／outbox；companion schema2 包含真实 v1 迁移和 `hint_checkpoints`；learning 仍为 v2，在初始化时 eager ensure 三张 planning 表。以上版本不改变历史 `StudyStore` 的 `teaching_state.schema_version=1`。

第五阶段真实供应商、价格、质量、账单、实机和部署尚未验收；受限解释和固定模板提示不代表开放式辅导、课程检索、长期记忆或真实教学效果完成。
