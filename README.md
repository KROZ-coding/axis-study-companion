# 阿克西斯 · 智慧学伴

[![License: PolyForm NC 1.0.0](https://img.shields.io/badge/License-PolyForm_NC_1.0.0-d97706)](LICENSE)

第二届北京市大学生“人工智能+”创新大赛第 10 题：智慧学伴学情诊断与个性化学习智能体构建。

本仓库承接 [`KROZ-coding/audit-companion`](https://github.com/KROZ-coding/audit-companion) 的 FastAPI 开发基础，并在其上新增七年级数学虚构样例诊断演示。上游代码来源提交：`d01ab02d287641e5c62b731a7941b9a5c4e07b29`。上游许可为 PolyForm Noncommercial 1.0.0；完整声明见根目录 `LICENSE` 与 `THIRD_PARTY_NOTICES.md`。

## 当前可运行内容

- FastAPI 同源应用，默认地址 `http://127.0.0.1:8000/`。
- 教师/学生样例视图、材料查找、班级与个人诊断、证据展开、建议材料和 JSON/CSV 样例导出。
- 练习答案复用上游 SQLite 存储，服务重启后恢复；新练习与固定历史诊断分开保存。
- 诊断由服务端确定性规则计算；有效记录少于 3 条时标为“依据不足”，不将缺失视为答错。
- 所有演示学生、作答和材料均为虚构数据。当前尚未导入正式赛事资料，也未完成正式数据字段映射与独立评测。
- 无需模型密钥或外部服务即可运行。此版本的新增样例链路不访问外网。

上游可复用的 SQLite 存储、配置、文件校验、密码工具、知识库及部分课程/账户/图谱/批改模块已迁入 `server/app/`。主应用使用上游 SQLite `Store` 并覆盖审计课程种子；模型/检索适配器保留但未注册到网页路由。当前已适配数学样例 API 的是诊断、证据、材料建议、练习保存和导出；登录、审计平台测验/批改、图谱及正式赛事数据尚未转为本赛题功能。详见[承接与本地部署说明](docs/本地部署与承接说明.md)。

## 本地启动

要求 Windows 和 Python 3.11 或更高版本。首次启动需要安装依赖：

```powershell
cd server
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
cd ..
.\start_study_companion.bat
```

启动后打开 <http://127.0.0.1:8000/>。直接运行方式：

```powershell
cd server
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

端口可通过环境变量 `STUDY_COMPANION_PORT` 指定。`/health` 返回本地样例模式状态。服务默认只绑定回环地址，不是公网部署配置。

## 目录

| 路径 | 用途 |
| --- | --- |
| `server/app/` | 上游 FastAPI 基础及本项目样例 API/规则诊断 |
| `web/` | 由 FastAPI 提供的前端入口和 API 数据适配 |
| `demo/` | 原始静态交互原型，保留供对照 |
| `docs/` | 赛题要求、制作方向和本地部署记录 |
| `start_study_companion.bat` | Windows 本地启动脚本 |
| `LICENSE` | 上游 PolyForm Noncommercial 1.0.0 许可全文 |

正式赛题资料、个人信息、模型密钥、数据库、上传和生成文件不应提交到仓库。使用正式材料前需按赛题授权确认其可用于本地导入及允许的模型服务。
