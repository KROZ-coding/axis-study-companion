# 正式后端重构接入说明

更新日期：2026-11-15。基于现有 audit-companion/FastAPI 项目承接；本说明记录当前已写入代码的接入契约及验收边界。Node联合回归已通过144/144，最新本机假网关 smoke 为8阶段／88次HTTP／24.469秒。冻结版本完整Python回归417/417通过，137.333秒，退出码0。本机假网关浏览器已确认分页、发送追问后刷新、窄屏输入区、带题引导、命名、归档恢复、返回作答及停服后新发送失败时的草稿保留；新增暂停／取消／确认恢复控件仅自动化验证。原产品需求仍以总方案和覆盖清单为准。

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
- `/api/v1/goals`、`tasks`：幂等目标和轮次发布、冻结个人卷、定时窗口、设备租约、草稿/提交分离、整轮评分统一揭示、多次重做、延期。
- `/diagnostics`：独立短诊与暂时不会，题目与正式卷分开，不计正式成绩。
- `/attempts`：评分异议先复核并转教师，人工评分与附加更正，保留原答原分。教师不能代改草稿或提交。
- `/companion`：学生会话创建／命名／归档恢复、消息分页、后台回答、持久片段 SSE、收藏备注、教师短评与学生原线程回复；全入口按当前授权及合并答案政策投影。
- `/jobs`、`budgets`：后台优先队列、租约代次、取消/确认恢复、用量与费用分开对账。调用未知结果不能盲目重发，迟到费用独立保存。
- `/reports`：四类报告从一个冻结快照导出 HTML/PDF/XLSX/JSON/多表CSV ZIP；规则和数据修订可追溯。
- `/input/math`：公式模板与显式确认。`/input/audio/transcriptions` 在供应商未配置时明确返回503，音频不保存/不外发。

所有正式写请求须携带同源 `Origin`；HTTP 客户端也需明确发送。错误含稳定code，不返回供应商完整异常或秘密。演示身份头不参与正式授权。

## 学伴生成、流式与恢复契约

`GET /api/v1/companion/answer-policy` 可带 `task_id`、`question_version_id`，由服务端合并当前任务与学生全局政策；两者均获准才允许生成。已出分任务不能绕过其他进行中任务。首诊及活跃作答仍走固定 `safe_guidance`，不创建付费模型调用；R29 逐步解题提示、课程检索和记忆增强尚未实施，本轮不标记完整阶段 E 验收。

`POST /api/v1/companion/conversations/{id}/messages` 仅保存消息、回答意图与 outbox，返回后由真实 worker 处理；同一 `request_id` 的同载荷重试不重复生成。任务／题目范围须属于当前学生及会话课程，客户端不能指定其他学生或班级来改变权限和扣账归属。作答页带入的题目、选项和个人尝试可编辑并须确认发送；返回作答时重新读取服务端权威任务并获取编辑租约，未修改的本地缓存不遮蔽服务端草稿，该修复已有自动回归及双视口 GUI 复验。

`GET /api/v1/companion/responses/{id}/stream` 提供 `start/reset/delta/end/snapshot/revoked` SSE。每次获准连接／重连的第一帧都是按当前授权和答案政策投影的权威 `snapshot`，不因 `Last-Event-ID`／`after` 已在尾部而跳过；回答文本、代次和事件游标在同一读取事务中取得。客户端先替换旧前缀，再接收该快照之后已提交的片段，按代次和事件 ID 去重；代次变化、游标失配或可见范围变化再次发快照，终态读取补齐快照之后的当前代事件。队列状态、`cancel_requested`、`reconciliation_required` 或 `cost_pending` 改变时，即使回答事件游标不变也会通知，不能只按游标丢弃状态更新。

历史消息带关联回答状态，刷新后使用消息历史、`GET /responses/{id}` 和 `/events` 读取同一回答；这些 GET 仅读取持久事实，不重发消息、不创建付费调用。前端流异常时使用有界退避 GET 恢复。回答因 `reconciling`／`awaiting_confirmation` 暂停时显示核对调用结果、核对费用或等待确认的原因，并提供“刷新状态”“取消本次回答”。费用未结清或调用结果仍待对账时，前后端均阻止恢复；仅在 `awaiting_confirmation` 且没有待结算、待对账或取消请求时，显示“确认恢复（可能产生费用）”，向原作业的 `POST /api/v1/jobs/{job_id}/resume` 发送 `confirm_paid: true`，后续读取／订阅原回答，不新建消息或回答。

取消使用原作业的 `POST /api/v1/jobs/{job_id}/cancel`，回答终态为 `cancelled`，不误报为 `failed`；已发生及待对账费用继续保存，未知调用的队列状态仍可为 `reconciling`。取消不会清零费用，也不会在稍后对账时复活回答。新增暂停／取消／确认恢复控件仅经自动化验证，未追加 GUI 验收。

HTTP、SSE、作业读取与 worker 共享当前访问检查。上游发送前、增量保存和最终提交再次检查账号、课程／班级归属以及全局和任务门禁；历史与事件重放同样重新授权，不沿用旧许可。失权时停止输出并返回 `revoked`，政策收紧或失败时前端清除不再可见的旧前缀。上下文只纳入当前仍可见、任务范围兼容的有限历史轮次；这不是课程检索或长期记忆功能。

## 数据与版本

学习库 v2 在原学习事实表上增加请求幂等、题族元数据、源修订、诊断、计划、异议和通知出站记录。任务提交与评分outbox同事务；跨库投递采用稳定业务键。题目正文、提交答案、原始评分与更正历史分别保存。分值100单位/分，费用为显式整数计费单位。

后台 worker 在ASGI生命周期启动，导入应用不打开数据库。评分按既定规则自动恢复；其他付费任务先完成调用结果对账和费用结算，再由用户明确确认恢复原作业。界面关闭不取消后台。SSE仅读取已提交片段，每次连接／重连先发当前投影快照，不新发调用，循环校验当前会话和答案门禁。

旧压缩SQLite/JSON/ZIP只读解码与dry-run保留，不在启动时实例化旧Store迁库。没有明确账号/班级映射的数据不能自动转真实学生事实。实际旧库导入适配仍禁用；旧样例库保留原状。

备份覆盖六库与附件、schema和SHA256清单，仅恢复到隔离目录。活库读取保留已提交WAL可见性，完整独立快照用immutable只读校验，恢复返回时不生成WAL/SHM旁文件。恢复包的`databases/`与附件根需按Runtime布局装配后启动；正常运行的WAL库之后可以生成旁文件。六库逐库SQLite快照不能保证在持续跨库写入时具有同一业务时点；正式备份仍应停写后执行，恢复后需核对会话、租约和未决调用。

## 模型与预算

教师交互／出题／审题固定 `gpt-5.6-sol/high`；学生、评分和结果总结固定 `gpt-5.6-terra/high`。独立 `ModelGateway` 已支持 `teacher/student/grading/summary` 分组及非流式／SSE；旧 `LLMClient` 单独保留，不作正式网关隐式回退。`grading`、`summary` 未单列时可使用 `student` 通道，仍固定 Terra/high，不能因失败静默换型号或密钥。

任务绑定消息由服务端从真实归属解析并冻结 `student_id/course_id/class_id/task_id` 到 outbox 和作业，预占与结算据此使用班级学生日额。无任务自由聊天使用明确配置的个人全局日额与 `project/platform` 总额，不任意挑选班级。额度通过 `PUT /api/v1/budgets/limits` 设置：管理员配置项目和个人全局额度，班级负责人配置所属学生的班级日额；`fund` 钱包不是正式额度来源。

价格按 provider/model 保存版本化 `version`、`currency` 和 `input_units_per_million`／`output_units_per_million`，单价为每百万 token 的整数计费单位。`currency` 必须与 `BUDGET_CURRENCY` 一致；缺价格、缺适用预算或币种不一致时阻断付费调用。usage 缺失保留 `cost_pending` 等待对账，不以0结算，也不盲目重发未知请求。名义 `site-credit` 不保证法币兑换或供应商现金收费上限。

非敏感配置参考 `server/.env.example`；`api_key_env` 只引用环境变量名，凭据由进程环境变量或密钥服务注入，本机 `.env` 仅放非敏感项。凭据、Cookie、数据库和运行产物不得提交。真实 Terra／价格／预算联调本轮**未执行**，因为单价、币种和总调用额度尚未确认。2026-11-10 Sol/high HTTP 200 仅为历史最小连接记录，真实线路、实际账单和教学质量仍待实测。

## 保留的验收边界

本机 GUI 夹具从仓库根目录启动，输出必须指定 `.scratch/` 下不存在的新忽略目录：

```powershell
.\server\.venv\Scripts\python.exe -I -B -X utf8 server/tests/verify_companion_live.py --serve --stream-seconds 8 --output-dir .scratch/companion-gui-NEW
```

`service-info.json` 提供实际 origin、场景和本地凭据位置；`free/guided/diagnostic` 账号分别覆盖自由对话、任务固定引导和独立首诊，口令每次本机生成，不输出或提交。夹具使用真实应用／认证／worker，注入 `FakeGateway` 且无真实上游。最新 smoke（不加 `--serve`）于2026-11-15通过**8阶段、88次真实回环 HTTP 请求，24.469秒**，脱敏证据 `.scratch/companion-live-final-20261115-02/evidence.json`；关闭退出码0、`reaped: true`，临时数据库和凭据均已清理。旧冻结 smoke 的91次／21.594秒及独立 `--serve` 生命周期正常启停清理仅保留为前次证据。

Node联合命令144/144通过，0失败，572.848毫秒（学生72、传输34、教师38），含同游标队列状态通知和暂停／取消／确认恢复控件自动化验证。冻结版本完整Python回归 `run_tests.py` 417/417通过，137.333秒，退出码0；恢复门禁worker＋queue专项51/51通过，15.026秒，覆盖直接resume路由在费用／对账未完成时拒绝恢复、拒绝后队列／回答／事件／调用／钱包／项目预算不变，以及结算后恢复同一作业、两次合法调用分别记账。旧Python411/411（127.234秒）、Node139/139（577.147毫秒）和专项73/73（61.240秒）仅作历史记录。`node --check` 与 `git diff --check` 此前已通过。

官方浏览器前轮已有1440×900桌面证据，并已确认103会话分页、110条历史消息分页、新建发送与追问后刷新同会话两轮消息；390×844输入区在底栏上方约11px，带题可编辑草稿及固定 `safe_guidance`、可访问行内命名（替换 `prompt`）和归档恢复通过。本轮补验确认返回作答在390×844与1366×900均保留B选中及1/1进度，桌面三栏布局正常；桌面中文输入、Tab聚焦发送／Enter发出后两轮固定安全引导终态及输入框清空通过。

停服后的新发送显示“连不上服务，请确认后端已启动”，保留中文草稿并提供发送结果未确认说明及重试原请求入口；未重启验证恢复，也未做生成过程中断线重连的 GUI 测试。当前浏览器运行时不支持减少动态、真实手机软键盘／IME组合输入及控制台监听，未做移动实机测试；中文文本输入成功不等于真实输入法组合验证。

旧 GUI 夹具 `.scratch/companion-live-browser-20261115-01` 因编排中断结束；确认进程与监听不存在后手动清理其隔离凭据和运行目录，元数据为 `stopped_after_orchestrator_interruption`，没有正常关闭退出码证据。新夹具 `.scratch/companion-live-browser-final-20261115-01` 已正常停止：`evidence.json` 记录 `status: stopped`、退出码0、`reaped: true` 和临时数据已删除，`service-info.json` 确认凭据已删除；补验报告为该目录 `gui-report.md`，附 `screenshots/` 八张截图；报告保留当时411／139／91的自动化计数，并已补充最终结果及后续控件未做 GUI 复验的说明，最新结果以上文为准。serve 的 `http_requests: 0` 不统计浏览器请求，不能当作独立 HTTP smoke 或 GUI 通过计数。

激活虚拟环境后可复现回归；仓库根目录没有 `package.json`，直接使用 Node 内置测试运行器：

```powershell
python -I -B -X utf8 server/tests/run_tests.py
node --test server/tests/frontend-regression.cjs server/tests/companion-api-regression.cjs server/tests/teacher-library-regression.cjs server/tests/teacher-planning-regression.cjs
```

2026-11-12的**345项完整隔离回归、27阶段／399次真实 HTTP**保留为历史平台验证，覆盖资料、首诊、发布、后台客观评分、报告、持续计划、异议及六库恢复后复验；详情见[后端修复与实际流程验证结果](后端重构统一测试结果.md)。它们不是本轮新增学伴的最终计数。

代码落盘与本机模拟验证不等于完整P1或阶段 E验收。扫描OCR／任意公式还原、ASR供应商、真实模型线路与教学质量、正式资料许可、算法校准及尚未覆盖的浏览器／移动实机体验仍待相应证据；上述桌面和窄屏主流程通过不替代这些验证，活跃题固定引导也不代表 R29 已完成。持续计划只在已确认题池／轮数／期限内推进，缺题受阻，不自动无限补题。不能将有界合成场景推广为全部产品路径或真实教学验收。
