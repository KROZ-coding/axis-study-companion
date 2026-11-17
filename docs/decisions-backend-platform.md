# 正式后端重构接入说明

更新日期：2026-11-17（第四阶段文档收尾）。基于现有 audit-companion/FastAPI 项目承接；本说明记录已写入代码的正式接入契约及边界。第四阶段接通冻结目标报告总结、教师确认的下一轮／有限持续计划和正式题两级受限提示；详细证据及覆盖范围见[验证记录与边界](验证记录与边界.md)。后文144／88／417等保留为2026-11-15学生切换 Sol 之前 `aede83a` 的历史证据，不作为第四阶段结果。原产品需求仍以总方案和覆盖清单为准。

第四阶段生产代码通过 Python **564/564**、Node **406/406**；验收器收尾专项 **8/8** 通过，修正后的独立 HTTP **8/8** 阶段、**330** 次请求通过。上述全量为提示取消读取门禁修复后的重跑，包含新增的取消后隐藏正文回归。浏览器已证实范围、未覆盖项及清理结果见[验证记录与边界](验证记录与边界.md)。

## 启动

正式实例默认 `STUDY_DEMO_ENABLED=false`，数据目录按 `Settings.data_dir` 统一解析，相对路径基于 `server/`；不再从当前命令目录推断。六个 SQLite 文件分别为 platform、learning、jobs、content、reports、companion。

首次管理员（在 server 目录运行）：

```powershell
.\.venv\Scripts\python.exe -m app.init_admin --username your-admin --display-name 管理员
```

密码由终端隐藏输入，不设默认管理员，不提交明文密码。应用和初始化命令使用相同数据目录。

通过根启动脚本或在仓库根目录执行 `python -m uvicorn app.main:app --app-dir server --host 127.0.0.1 --port 8000` 启动。正式根页面返回服务状态；`/prototype/` 的 `web/prototype/` 已接真实认证、师生工作区和学生学伴，`/health` 返回 `mode: platform`。需要旧虚构演示时在独立实例设置 `STUDY_DEMO_ENABLED=true`、`APP_ENV=development` 和独立的 `STUDY_DATA_DIR`，仅开放旧 `/api/study/*`、`/api/teaching/*` 与旧网页，不混合正式接口。完整安装与环境命令见[本地部署与承接说明](本地部署与承接说明.md)。

## 接口与领域

- `/api/v1/auth`：Cookie 登录、教师申请审核、学生建号一次激活、全部角色重置、停用和会话撤销。复用原项目 Argon2 工具；一次性码/会话只存哈希。
- `/api/v1/course-applications`、`courses`、`classes`：管理员确认课程；班级负责人、只读/共同管理、成员转班与负责人交接。课程材料与班级学情分别授权。
- `/api/v1/goals`、`tasks`：幂等目标和轮次发布、冻结个人卷、定时窗口、设备租约、草稿／提交分离、整轮评分统一揭示、多次重做、延期；第四阶段下一轮沿用原目标发布，持续计划受教师确认的有限题池／轮数／截止约束。
- `/diagnostics`：独立短诊与暂时不会，题目与正式卷分开，不计正式成绩；作答与 `unknown` 互斥，首诊不开放受限提示。
- `/attempts`：评分异议先复核并转教师，人工评分与附加更正，保留原答原分。教师不能代改草稿或提交。
- `/companion`：学生会话创建／命名／归档恢复、消息分页、后台回答、持久片段 SSE、收藏备注、教师短评与学生原线程回复；全入口按当前授权及合并答案政策投影。
- `/jobs`、`budgets`：后台优先队列、租约代次、取消/确认恢复、用量与费用分开对账。调用未知结果不能盲目重发，迟到费用独立保存。
- `/reports`：四类报告从一个冻结快照导出 HTML/PDF/XLSX/JSON/多表CSV ZIP；规则和数据修订可追溯。
- `/api/v1/teaching-insights`：从完整目标报告的指定冻结修订创建、读取总结；使用独立 `teaching_insight` 领域与 Terra/high 的 `summary`，不是学伴 `response`。`/{insight_id}/next-round-drafts` 创建下一轮草稿，`/next-round-drafts/{draft_id}` 支持读取／CAS 保存及 `/preview`、`/publish`、`/bounded-plan`。
- `/input/math`：公式模板与显式确认。`/input/audio/transcriptions` 在供应商未配置时明确返回503，音频不保存/不外发。

所有正式写请求须携带同源 `Origin`；HTTP 客户端也需明确发送。错误含稳定code，不返回供应商完整异常或秘密。演示身份头不参与正式授权。

## 学伴生成、流式与恢复契约

`GET /api/v1/companion/answer-policy` 可带 `task_id`、`question_version_id`，由服务端合并当前任务与学生全局政策；自由聊天仍须两者均获准，已出分任务不能绕过其他进行中任务。第四阶段对正式当前题增加独立的 `hint` 门禁：服务端已保存合法尝试，且当前 `attempt_cycle`、`draft_hash`、政策／校验器版本匹配时，才可请求两级受限提示。首诊继续独立作答，不扩大自由聊天权限。模型只返回单字段 `action_id` JSON，服务器从有限动作模板生成可见内容，不公开 raw 输出或 `delta`；无外发许可、坏输出、预算／配置不可用等情形回退固定 `safe_guidance`。这不等于开放式解题、课程检索、记忆增强或完整阶段 E 验收。

`POST /api/v1/companion/conversations/{id}/messages` 保存消息、回答意图与 outbox，返回后由真实 worker 处理；同一 `request_id` 的同载荷重试不重复生成。受限提示使用独立 `hint` purpose，按当前正式题的已保存尝试绑定；自由聊天的可编辑带题文本不能代替该权威绑定。任务／题目范围须属于当前学生及会话课程，客户端不能指定其他学生或班级来改变权限和扣账归属。返回作答重新读取服务端任务并获取编辑租约，未修改的本地缓存不遮蔽服务端草稿。

正式作答草稿按700ms防抖、4s最长等待串行保存；未决写入保留原 `request_id` 与载荷重放，遵守租约／CAS，处理 IME 组合输入并在交卷结果未知时保护输入和后续动作。首诊的答案与“暂时不会”互斥；本地草稿仅在内存缓存，不据此承诺跨浏览器重启恢复。自动化和实际 GUI／设备覆盖分别见[验证记录与边界](验证记录与边界.md)。

只有服务端 `source: restricted_hint`、`status: validated_delivered` 且确定事件 ID／作答 cycle 匹配的 help 事件计为帮助；`requested`、`validated`、`fallback` 不计，旧无 `status` 的 help 保留兼容保守处理。提示的 `unknown`／`cost_pending` 保留待核对，核对完成后仍须明确确认恢复。

受限 `hint` 在已付费的成功 call 或安全 checkpoint 持久化后，若后续交付写入发生 `sqlite3.OperationalError`，队列可为 `failed`，领域回答仍保留 `streaming` 或已落盘的 `completed`，不因这次存储错误改写为失败回答。明确确认恢复原作业后，只复用原成功 call 的结果或 checkpoint，不新建付费调用、不重复扣费；已 `completed` 的提示沿用原回答绑定补齐交付记录。恢复时重查当前权限、作答绑定与费用状态，交付确认前隐藏提示，取消或撤权阻断采用。此存储错误保留规则限于该 hint 路径，不适用于所有异常或所有作业。

`GET /api/v1/companion/responses/{id}/stream` 提供 `start/reset/delta/end/snapshot/revoked` SSE。每次获准连接／重连的第一帧都是按当前授权和答案政策投影的权威 `snapshot`，不因 `Last-Event-ID`／`after` 已在尾部而跳过；回答文本、代次和事件游标在同一读取事务中取得。客户端先替换旧前缀，再接收该快照之后已提交的片段，按代次和事件 ID 去重；代次变化、游标失配或可见范围变化再次发快照，终态读取补齐快照之后的当前代事件。队列状态、`cancel_requested`、`reconciliation_required` 或 `cost_pending` 改变时，即使回答事件游标不变也会通知，不能只按游标丢弃状态更新。

历史消息带关联回答状态，刷新后使用消息历史、`GET /responses/{id}` 和 `/events` 读取同一回答；这些 GET 仅读取持久事实，不重发消息、不创建付费调用。前端流异常时使用有界退避 GET 恢复。回答因 `reconciling`／`awaiting_confirmation` 暂停时显示核对调用结果、核对费用或等待确认的原因，并提供“刷新状态”“取消本次回答”。`web/prototype/app.js` 还将 `intent: hint`、回答为 `streaming`／`completed` 且 `job.status: failed` 的交付中断纳入确认恢复入口；非 hint 保持原有暂停条件和 `awaiting_confirmation` 恢复规则。

显示及执行“确认恢复（可能产生费用）”均要求 `cost_pending`、`reconciliation_required`、`cancel_requested` 全部为假；回答自身为 `failed`、`cancelled` 或 `revoked` 时不允许。用户明确确认后，向原作业的 `POST /api/v1/jobs/{job_id}/resume` 发送 `confirm_paid: true`，后续读取／订阅原回答，不新建消息或回答。该通用按钮的费用提示不表示上述已有成功结果的 hint 恢复会再次付费。

取消使用原作业的 `POST /api/v1/jobs/{job_id}/cancel`。明确取消 `failed` 作业也会设置 `cancel_requested: true` 并将队列状态改为 `cancelled`；尚未完成的回答随之以 `cancelled` 收尾，不误报为 `failed`。已持久 `completed` 的领域结果保留，但提示投影与交付采用仍受取消门禁约束。公共 `POST /api/v1/jobs/{job_id}/resume` 遇到取消标记或 `cancelled` 队列状态时返回 HTTP 409（`cancelled_job`）；这是公共业务入口的限制，不能泛化为底层 `JobQueue.retry` 禁止所有取消态重试。

已发生及待对账费用继续保存，未知调用的队列状态仍可为 `reconciling`；取消不会清零费用，也不会在稍后对账时复活回答。暂停／取消／确认恢复控件及存储故障恢复有自动化验证，尚未进行浏览器故障注入；学生正常流程的 GUI 覆盖范围见[验证记录与边界](验证记录与边界.md)。

HTTP、SSE、作业读取与 worker 共享当前访问检查。上游发送前、增量保存和最终提交再次检查账号、课程／班级归属以及全局和任务门禁；历史与事件重放同样重新授权，不沿用旧许可。失权时停止输出并返回 `revoked`，政策收紧或失败时前端清除不再可见的旧前缀。上下文只纳入当前仍可见、任务范围兼容的有限历史轮次；这不是课程检索或长期记忆功能。

## 冻结报告总结与下一轮

总结只接受完整目标报告的指定冻结修订，按该快照全体学生、全轮次证据及原版本规则复算 `facts`。模型只能选择有限解释码；未知／改写的数字、遗漏／新增学生、未知或跨学生证据、低证据强断言均拒绝。服务器固定模板生成可见总结，保留证据不足及规则未校准的限制；目前未定位具体知识点，不称自由 AI 诊断。来源修订变化显示旧数据状态，不覆盖原冻结结果，也不自动重发模型调用。

总结保存幂等请求、来源修订与输入 hash，并以 `generation` 防止迟到结果覆盖；创建、重放、读取、发送和采纳重查当前教师班级权限。完成／失败／取消状态与通知 outbox 同事务写入，投递使用稳定业务键。总结领域恢复仅接受 `failed`，还须通过原作业的结果／费用核对和明确付费确认门禁；取消不复活。active 管理员须通过作业与总结的完整资源绑定校验，才能访问 `/jobs` 元数据和对账入口，不获得总结正文权限。

下一轮使用已审核题库确定性配卷，草稿编辑按 `expected_revision` CAS；完整预览后携带有效 `preview_token` 和 `confirm` 发布到原目标 `max(round_no)+1`。排除已使用题族，前轮完成且评分齐备才可推进，冲突时重新核对，不另建目标。有限持续计划由教师确认题池、轮数、间隔和截止；缺题、前轮未完成、待评分或撤权时暂停，不自动造题或无限补题。

## 数据与版本

仍为 platform、learning、jobs、content、reports、companion 六库，不新增数据库。第四阶段 reports 的数据库 schema 为2，增加 `teaching_insights`、`teaching_insight_outbox`；冻结报告载荷的 `report.snapshot.v1` 不因此改名。companion schema 为2，真实 v1 迁移保留原消息／回答及关联关系，增加受限提示字段和 `hint_checkpoints`。learning 仍为 v2，初始化时 eager ensure `teacher_plan_drafts`、`teacher_plan_previews`、`teacher_next_round_drafts` 三张计划表，无需先访问计划服务。

学习库 v2 原有请求幂等、题族元数据、源修订、诊断、计划、异议和通知出站记录继续保留。任务提交与评分 outbox 同事务；跨库投递采用稳定业务键。题目正文、提交答案、原始评分与更正历史分别保存。分值100单位/分，费用为显式整数计费单位。

后台 worker 在ASGI生命周期启动，导入应用不打开数据库。评分按既定规则自动恢复；其他付费任务先完成调用结果对账和费用结算，再由用户明确确认恢复原作业。`unknown`／`cost_pending` 不按零费用处理，不盲目重发；已持久成功的结果复用，受限提示的 checkpoint 支持跨库中断后恢复且不二次付费调用。界面关闭不取消后台，取消／撤权仍阻断结果采用。自由学伴 SSE 仅读取已提交片段，每次连接／重连先发当前投影快照，不新发调用，循环校验当前会话和答案门禁。

旧压缩SQLite/JSON/ZIP只读解码与dry-run保留，不在启动时实例化旧Store迁库。没有明确账号/班级映射的数据不能自动转真实学生事实。实际旧库导入适配仍禁用；旧样例库保留原状。

备份覆盖六库与附件、schema和SHA256清单，包含 reports schema2 的总结／outbox、companion schema2 的提示 checkpoint 及 learning v2 的三张计划表，仅恢复到隔离目录。活库读取保留已提交WAL可见性，完整独立快照用immutable只读校验，恢复返回时不生成WAL/SHM旁文件。恢复包的`databases/`与附件根需按Runtime布局装配后启动；正常运行的WAL库之后可以生成旁文件。六库逐库SQLite快照不能保证在持续跨库写入时具有同一业务时点；正式备份仍应停写后执行，恢复后需核对会话、租约、提示 checkpoint 和未决调用。

## 模型与预算

2026-11-15按用户要求，教师交互／出题／审题和学生学伴（`student`、`hint` 用途）固定 `gpt-5.6-sol/high`；评分和结果总结固定 `gpt-5.6-terra/high`。独立 `ModelGateway` 使用 `teacher/student/grading/summary` 四个显式通道，依次为 Sol/Sol/Terra/Terra，全部 high，并支持非流式／SSE；`grading`、`summary` 缺失时拒绝对应调用，不再回落 `student`。旧 `LLMClient` 单独保留，不作正式网关隐式回退，不能因失败静默换型号或密钥。

任务绑定消息由服务端从真实归属解析并冻结 `student_id/course_id/class_id/task_id` 到 outbox 和作业，预占与结算据此使用班级学生日额。无任务自由聊天使用明确配置的个人全局日额与 `project/platform` 总额，不任意挑选班级。额度通过 `PUT /api/v1/budgets/limits` 设置：管理员配置项目和个人全局额度，班级负责人配置所属学生的班级日额；`fund` 钱包不是正式额度来源。

价格按 provider/model 保存版本化 `version`、`currency` 和 `input_units_per_million`／`output_units_per_million`，单价为每百万 token 的整数计费单位。`currency` 必须与 `BUDGET_CURRENCY` 一致；缺价格、缺适用预算或币种不一致时阻断付费调用。usage 缺失保留 `cost_pending` 等待对账，不以0结算，也不盲目重发未知请求。名义 `site-credit` 不保证法币兑换或供应商现金收费上限。

非敏感配置参考 `server/.env.example`，base URL 为 `https://www.poke2api.com/v1`；`api_key_env` 只引用环境变量名，凭据由进程环境变量或密钥服务注入，本机 `.env` 仅放非敏感项。凭据、Cookie、数据库和运行产物不得提交或回显；提供过凭据不等于已注入运行环境。2026-11-15 当时未启用本机模型配置、未注入对应凭据，单价、币种和总调用额度未确认，该次没有真实付费调用；本次文档同步未复核凭据环境。真实 Sol／Terra 线路、价格、预算／账单及教学质量属于第五阶段，尚未验收；2026-11-10 Sol/high HTTP 200 仅为历史最小连接记录。

## 保留的验收边界

第四阶段专项、最终全量及 GUI 证据统一见[验证记录与边界](验证记录与边界.md)。本节下列命令、数字和浏览器描述保留 **2026-11-15 `aede83a` 及更早版本的当时记录**；其中“本轮／前轮”指所列历史验证，不是第四阶段的复测声明。

当时的本机 GUI 夹具从仓库根目录启动，输出必须指定 `.scratch/` 下不存在的新忽略目录：

```powershell
.\server\.venv\Scripts\python.exe -I -B -X utf8 server/tests/verify_companion_live.py --serve --stream-seconds 8 --output-dir .scratch/companion-gui-NEW
```

`service-info.json` 提供实际 origin、场景和本地凭据位置；`free/guided/diagnostic` 账号分别覆盖自由对话、任务固定引导和独立首诊，口令每次本机生成，不输出或提交。夹具使用真实应用／认证／worker，注入 `FakeGateway` 且无真实上游。`aede83a` smoke（不加 `--serve`）于2026-11-15通过**8阶段、88次真实回环 HTTP 请求，24.469秒**，脱敏证据 `.scratch/companion-live-final-20261115-02/evidence.json`；关闭退出码0、`reaped: true`，临时数据库和凭据均已清理。旧冻结 smoke 的91次／21.594秒及独立 `--serve` 生命周期正常启停清理仅保留为前次证据。

Node联合命令144/144通过，0失败，572.848毫秒（学生72、传输34、教师38），含同游标队列状态通知和暂停／取消／确认恢复控件自动化验证。冻结版本完整Python回归 `run_tests.py` 417/417通过，137.333秒，退出码0；恢复门禁worker＋queue专项51/51通过，15.026秒，覆盖直接resume路由在费用／对账未完成时拒绝恢复、拒绝后队列／回答／事件／调用／钱包／项目预算不变，以及结算后恢复同一作业、两次合法调用分别记账。旧Python411/411（127.234秒）、Node139/139（577.147毫秒）和专项73/73（61.240秒）仅作历史记录。`node --check` 与 `git diff --check` 此前已通过。

官方浏览器前轮已有1440×900桌面证据，并已确认103会话分页、110条历史消息分页、新建发送与追问后刷新同会话两轮消息；390×844输入区在底栏上方约11px，带题可编辑草稿及固定 `safe_guidance`、可访问行内命名（替换 `prompt`）和归档恢复通过。本轮补验确认返回作答在390×844与1366×900均保留B选中及1/1进度，桌面三栏布局正常；桌面中文输入、Tab聚焦发送／Enter发出后两轮固定安全引导终态及输入框清空通过。

停服后的新发送显示“连不上服务，请确认后端已启动”，保留中文草稿并提供发送结果未确认说明及重试原请求入口；未重启验证恢复，也未做生成过程中断线重连的 GUI 测试。当前浏览器运行时不支持减少动态、真实手机软键盘／IME组合输入及控制台监听，未做移动实机测试；中文文本输入成功不等于真实输入法组合验证。

旧 GUI 夹具 `.scratch/companion-live-browser-20261115-01` 因编排中断结束；确认进程与监听不存在后手动清理其隔离凭据和运行目录，元数据为 `stopped_after_orchestrator_interruption`，没有正常关闭退出码证据。新夹具 `.scratch/companion-live-browser-final-20261115-01` 已正常停止：`evidence.json` 记录 `status: stopped`、退出码0、`reaped: true` 和临时数据已删除，`service-info.json` 确认凭据已删除；补验报告为该目录 `gui-report.md`，附 `screenshots/` 八张截图；报告保留当时411／139／91的自动化计数，并已补充最终结果及后续控件未做 GUI 复验的说明，该冻结版结果以上文为准。serve 的 `http_requests: 0` 不统计浏览器请求，不能当作独立 HTTP smoke 或 GUI 通过计数。

激活虚拟环境后可复现回归；仓库根目录没有 `package.json`，直接使用 Node 内置测试运行器：

```powershell
python -I -B -X utf8 server/tests/run_tests.py
node --test server/tests/frontend-regression.cjs server/tests/companion-api-regression.cjs server/tests/teacher-library-regression.cjs server/tests/teacher-planning-regression.cjs
```

2026-11-12的**345项完整隔离回归、27阶段／399次真实 HTTP**保留为历史平台验证，覆盖资料、首诊、发布、后台客观评分、报告、持续计划、异议及六库恢复后复验；详情见[后端修复与实际流程验证结果](后端重构统一测试结果.md)。它们不是本轮新增学伴的最终计数。

第四阶段的受限总结、两级提示和教师确认的下一轮能力已落盘，其最终自动化与 GUI 覆盖以统一验证记录为准；代码与本机模拟证据不等于完整P1或阶段 E验收。真实供应商、价格、教学质量、账单、实机和部署仍属第五阶段尚未验收；扫描OCR／任意公式还原、ASR、正式资料许可、算法校准、课程检索和记忆增强也须各自证据。历史固定引导 GUI 不能替代当前 R29 受限提示验收，不能将有界合成场景推广为全部产品路径或真实教学效果。
