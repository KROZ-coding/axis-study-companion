# 正式部署与上线验收

本目录提供单机 Linux 的 systemd + Caddy 模板，适用于一个正式服务进程、SQLite 六库和进程内 worker。模板已经加入源代码；只有在实际服务器按下列流程完成检查，才能登记为部署验收。公开范围、域名和获准资料由项目负责人落实。

## 目录与配置

| 位置 | 用途 |
| --- | --- |
| `/opt/axis-study-companion` | 受控版本的源代码与 `server/.venv`，服务账号只读 |
| `/etc/axis-study-companion/production.env` | 根据 `production.env.example` 填写的非敏感配置 |
| `/etc/axis-study-companion/credentials.env` | 单独注入的凭据，禁止提交，限制为 root 可读 |
| `/var/lib/axis-study-companion` | 服务账号 `axis` 所有，0700；六库、资料原件和附件 |
| 独立备份位置 | 停写备份，限制访问，与服务数据目录分开 |

使用 Python 3.12 创建虚拟环境，安装 `server/requirements.txt`。创建无交互登录权限的 `axis` 系统账号，源代码由部署账号管理。模板中的 `/opt/axis-study-companion`、域名与数据目录如需调整，必须同时更新 service、配置与 Caddyfile。systemd 的 `StateDirectory` 会为服务账号准备数据目录，手动初始化管理员前也应建立同一目录。

将 `production.env.example` 安装到 `/etc/axis-study-companion/production.env` 后：

- 保持 `APP_ENV=production`、`STUDY_DEMO_ENABLED=false`、`COOKIE_SECURE=true`、`SKIP_ENV_FILE=1`。
- `DATA_DIR` 使用源码目录外的绝对路径。`ALLOWED_HOSTS` 填实际域名，不带协议、路径、端口或通配符。
- 前端与 `/api/v1` 使用同一 HTTPS origin，`CORS_ORIGINS` 默认留空。写请求仍由应用的同源检查控制。
- 初始四通道和价格留空，付费能力不会启用。启用前参考 [模型约定](../docs/模型分工与接入约定.md)，通过独立服务环境注入密钥引用，配置真实整数价格及币种。不要把密钥写进 `LLM_PROVIDERS_JSON`、启动参数或日志。
- systemd 的 `EnvironmentFile` 不是 shell 脚本，不能 `export`，也不进行 `$VAR` 变量展开；JSON 配置保持一行。不要直接用 `source` 执行未知环境文件。

## 离线预检与首次管理员

在拥有相同部署环境的进程中，从 `server/` 执行：

```bash
.venv/bin/python -B -m app.deployment_check --profile production
.venv/bin/python -B -m app.deployment_check --profile production --require-models
```

第一条允许尚未启用模型的正式实例；第二条要求四通道、确切型号、高推理强度及每个候选线路的匹配价格。退出码 0 表示静态配置通过，1 表示有阻断项。工具不发网络请求，不打开数据库，不回显凭据，不证明预算余额、DNS、TLS、实际型号执行、账单或质量已经验证。项目、教学及适用学生日额仍须通过认证后的管理界面设置并验证。

第一次部署时，以 `axis` 账号在相同配置环境中执行：

```bash
.venv/bin/python -B -m app.init_admin --username YOUR_ADMIN --display-name 管理员
```

密码由终端隐藏输入；没有默认密码。确保初始化与 service 使用同一个 `DATA_DIR`，已有管理员时不要重复初始化。对运行中实例的账号管理使用正式界面。

## HTTPS 与进程管理

1. 域名解析到服务器，开放 80/443 供 Caddy 的 HTTPS 和证书管理使用；后端 8000 保持 loopback，不对外开放。
2. 安装 `axis-study-companion.service`，使用 `systemd-analyze verify` 检查实际安装文件。service 的 `ExecStartPre` 每次启动先运行生产预检。
3. 安装并用 `caddy validate --config /etc/caddy/Caddyfile` 检查实际 Caddy 配置。模板自动 HTTPS、根路径转 `/prototype/`、SSE 即时刷新；不得在其他代理重新开启流缓冲。
4. 启动 Caddy 和 AXIS 服务，用真实域名打开 `/health`，核对 `mode: platform`、worker 启用且 running 与配置并发一致、`needs_attention: false`。
5. 从 HTTPS 页面登录，核对 `sid` 含 Secure、HttpOnly、SameSite；同源写入成功、跨 origin 写入拒绝。检查反向代理后的应用 scheme 为 HTTPS。

uvicorn 只运行 **1 个应用进程**；并发由 `STUDY_WORKER_CONCURRENCY` 控制。`--proxy-headers --forwarded-allow-ips 127.0.0.1` 只信任本机 Caddy。若换远程代理，不可直接信任 `*`，必须重新核对网络和可信代理地址。不要加 `--reload`。

service 给予正常模型批处理和关停足够时间：uvicorn graceful 420 秒，systemd 450 秒。强制终止或宕机仍可能留下未决调用，应按原调用 ID 核对费用／结果，不盲目重发。服务和 Caddy 默认不记录访问正文；实际安装后检查日志策略，防止凭据、Cookie、学生文本或供应商原始输出进入日志。

## 停写升级、备份与恢复

按 [六库备份恢复操作](../docs/六库备份恢复操作.md) 执行。先停止外部写入口和全部服务进程，等待退出，确认监听、worker 和运维脚本均停止，再运行停写备份。`--stopped` 是操作者声明，不能自动证明服务器已经停写。

恢复只在新隔离目录中进行，保留原数据与旧版本。恢复后使用 `STUDY_WORKER_ENABLED=false` 的隔离实例核对六库、附件、会话、租约、提示 checkpoint、未决调用和费用，再决定正式切换；暂停期间的未知付费调用不能按未执行或零费用处理。一次恢复演练需记录备份 hash、源代码提交、schema、耗时、核对结果与回切步骤。

升级顺序为：停止服务 → 停写备份与校验 → 更新已验证代码和依赖 → 离线预检 → 启动与健康检查 → 核心师生流程。如果失败，停止新版本，在隔离副本恢复旧包并使用兼容旧代码复核；不要将较旧代码直接指向已经迁移的新库。

## 交付证据

正式回归位于 [formal-regression.yml](../.github/workflows/formal-regression.yml)，运行 Windows/Linux 隔离 Python、Node 与 Linux 回环 HTTP。只上传三份脱敏 `evidence.json`，不上传凭据、数据库、原件或完整运行目录。CI 通过不替代浏览器、真实模型或生产恢复演练。

逐项验收与外部条件登记见 [第五阶段验收清单](../docs/第五阶段验收清单.md)。当前目录的模板不表示已经有服务器、域名、证书或真实账单。
