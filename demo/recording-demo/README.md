# 伴星导学 · 独立演示版

本文件夹保存全流程演示副本，无需切换分支。正式版继续在仓库根目录的 `server/`、`web/` 开发；此处的代码和运行数据独立保留，不自动跟随正式版改动。

## 使用

双击本目录的 [`start_recording_demo.bat`](start_recording_demo.bat)，打开 <http://127.0.0.1:8010/prototype/>。通过顶部“演示工具”切换教师陈宁、学生林夏与周予。

完整顺序、参考答案、短评及真实/模拟边界见 [全流程演示操作指南](docs/全流程演示操作指南.md)。录屏由操作者自行完成。

启动器优先使用本目录的 `server/.venv`；没有时复用仓库根目录的 `server/.venv`。复用的仅是解释器和依赖，运行代码、前端资源和默认数据始终来自本目录。每次启动创建新的 `server/data/recording-demo/run-*`，保留旧运行，不读取正式版 `.env`，默认不调用外部模型。

## 独立安装依赖（可选）

如需单独移动此文件夹，或希望正式版升级依赖也不影响演示，在本目录执行：

```bat
py -3 -m venv server\.venv
server\.venv\Scripts\python.exe -m pip install -r server\requirements.txt
start_recording_demo.bat
```

本目录的 `server/requirements.txt` 是演示依赖快照。不要把 `.venv`、`.env`、数据库、运行记录或凭据提交到 Git。

## 验证

从仓库根目录执行（若已创建演示专用虚拟环境，可换用其解释器）：

```bat
server\.venv\Scripts\python.exe -I -B -X utf8 demo\recording-demo\server\tests\test_recording_demo.py
node --test demo/recording-demo/server/tests/recording-reports-regression.cjs
```

Python 测试自行创建临时数据库并禁止外部网络；覆盖首诊、草稿、提示、交卷、报告导出、师生讨论与权限隔离。Node 测试覆盖报告修订链接、错误展示和身份切换。

## 开发边界

- 正式功能：修改根目录 `server/`、`web/` 及相应正式测试。
- 演示维护：明确需要调整演示时，修改本目录。
- `demo/` 下原有静态样例继续保留；本演示从这里的启动脚本运行，不双击 HTML。
