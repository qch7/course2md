# course2md 改进路线

本文汇总项目审查、问题排查与架构讨论的结论，作为后续改造的依据。分为四部分：已完成的工作、目标形态、实施阶段、待决事项。

- 更新日期：2026-11-19
- 当前版本：course2md 1.0.0（含未发布改进）
- 验证方式与质量边界见 [测试与评估](testing.md)。第一节按阶段保留开发历史，当前改进见 1.15。

---

## 一、已完成

### 1.1 仓库建立

| 提交 | 内容 |
|---|---|
| `c3456d9` | 初始基线。`.env`、`asrlibs/`、模型缓存、输出目录与临时文件均不进入版本库 |

### 1.2 审查问题修复（`e66748b`）

**安全**

| 问题 | 修复 |
|---|---|
| 任意网页可借助浏览器向本地服务提交任务、读取结果（CSRF / DNS 重绑定），并可借 `llm_base` 外泄 API Key | 校验 `Host`（回环地址、监听地址或 `COURSE2MD_ALLOWED_HOSTS`）；写操作要求同源 `Origin` 与 JSON 请求体；`llm_base` 只接受 http(s) |
| 关闭了 HTTPS 证书校验却发送登录凭证 | 默认校验；`COURSE2MD_INSECURE_TLS=1` 显式关闭，证书错误给出处理提示 |
| 播放页链接中的凭证写入日志、`job.json`、`web-chat.json` | 只记录解析器提供的公开描述；完整链接仅保存在工作区数据库，用于刷新课次 |
| DSH 免审批运行且无沙箱 | 结构化调用使用 `read-only`，分段模式使用 `workspace-write` |

**正确性与性能**

| 问题 | 修复 |
|---|---|
| 下载大文件整体读入内存 | 分块流式输出 |
| 非法输出格式要等转写结束才报错 | 提交时校验格式、采样间隔、设备、机位与接口地址 |
| 转写过程中取消无效 | 每个转写片段后检查取消 |
| 每节课重新加载 Whisper 模型 | 队列复用同一模型，配置变化才重新加载 |
| 抽帧对每个采样点重新定位解码 | 稠密扫描改为顺序解码，稀疏时间点仍逐点定位；测试确认两种方式取帧一致 |
| 续接会话时每轮重发全部课程材料 | 续接时只发送当前课程记录与本轮要求，会话失效时自动改用完整材料 |
| 一轮对话失败会把课程标记为失败 | 课程保持可用，仅该条消息标记失败 |

**细节**：`.env` 的 `export` 前缀解析、带引号的 Windows 路径、Agent 超时异常、DSH 安装目录可配置、请求体大小与 JSON 校验、聊天 Markdown 渲染缓存。

### 1.3 DSH 权限问题排查（`8be8e58`）

现象：DSH 运行输出要求切换「完全权限」，所有 PowerShell 命令失败，读图正常。

结论：DSH 执行命令前要为工作目录授予沙箱身份写权限。course2md 服务若在受限的沙箱终端中启动，子进程继承受限令牌，授权失败（Win32 错误 5）。与代码、路径、中文目录、目录权限均无关，从普通终端启动即正常。

处理：`course2md doctor` 与网页启动时检测授权能力；运行中出现该错误时在「运行输出」给出中文说明。解决办法是在普通 PowerShell 或资源管理器中运行 `start.cmd`，不需要开启完全权限。

### 1.4 Agent 提供商与模型（`736f7b5`）

- DSH 通过临时 `--patch` 覆盖 `agent-default-model`；Codex 通过 `--model` 与 `-c model_provider=…` 传入。
- 设置来源（优先级从高到低）：网页「整理选项」、命令行 `--agent-provider` / `--agent-model`、环境变量 `COURSE2MD_{DSH,CODEX}_{PROVIDER,MODEL}`；留空沿用 CLI 自身配置。
- 网页根据 `dsh --dump-config` 与 `~/.codex/config.toml` 提供候选项；课程顶栏显示实际使用的提供商与模型。
- Agent 失败时显示 CLI 给出的原因（例如未登记的模型或提供商），不再只有退出码。

### 1.5 来源解析插件化

- 核心只内置本地文件与目录解析，具体课程平台解析器移至独立插件仓库。
- 稳定接口 `course2md.resolvers.api`：`BaseResolver`（必需方法 `matches` / `list_sessions` / `resolve`，可选钩子 `public_source` / `course_identity` / `view_options`）、`ViewOption`、`MediaRef.primary`、`download`、`tls_verify`、`RESOLVER_API_VERSION`。
- 发现方式：入口点组 `course2md.resolvers`；开发时可用 `COURSE2MD_RESOLVERS=模块:类`；`COURSE2MD_DISABLED_RESOLVERS` 停用。
- 隔离：接口版本不符、名称冲突、依赖缺失、匹配时抛出异常的插件只会被跳过，原因显示在 doctor 与网页启动提示中。
- 原先散落在核心中的平台特例（凭证脱敏、课程标识、机位、主音轨、界面文案）全部改由钩子提供。
- 插件开发文档：[plugins.md](plugins.md)。

### 1.6 阶段 1：拆分阶段（`ad42cc3`）

- 新增 `course2md/stages/`：`media`（解析、下载、抽音频、转写）、`frames`、`proofread`、`output`（转写文件与元信息）、`organize`（两版笔记）。每个阶段声明 `name` 与所需能力 `requires`，通过 `StageContext` 汇报进度、检查取消、记录警告。
- 产物格式（`stages/artifacts.py`）：`MediaFile`、`Transcript`、`FrameSet`、`TranscriptFiles`、`Notes`，均可与带 `kind` 和 `version` 的 JSON 互转，`load_artifact` 按类型还原。
- 执行器接口 `Executor`，目前只有在当前进程内运行的 `LocalExecutor`；`run_job(executor=…)` 可替换。
- `pipeline.run_job` 只负责编排：依次交给执行器、选主音轨、计时、写 `job.json`、成功后清理下载视频。`JobOptions`、`JobResult`、`Cancelled`、`TRANSCRIPT_FORMATS` 仍从 `course2md.pipeline` 导入。
- 行为变化：画面扫描改在全部媒体转写之后进行（原来逐路交替），转写进度区间相应调整为 0.34–0.56。
- 测试：216 项通过（新增 `tests/test_stages.py`；原测试只把替身位置改到 `course2md.stages.media`）。
- 尚未做：产物落盘与按内容哈希存储（阶段 3）；`organize` 的能力需随整理后端区分 `agent:dsh` / `agent:codex`（阶段 4 按能力派发时补上）。

### 1.7 阶段 0：仓库规范化

- `src/` 布局；uv 管理依赖与 `uv.lock`；核心依赖不含转写，`asr`（faster-whisper、PyAV、OpenCV）与 `cuda` 为可选依赖。测试确认核心模块在未装 `asr` 时可以导入。
- 去掉 `asrlibs` 与 `COURSE2MD_LIB`：启动脚本使用项目 `.venv`（首次运行时 `uv sync` 创建）；pip 安装的 cuBLAS / cuDNN 自动登记 DLL 目录。
- 统一配置：`config/schema.py` 定义全部配置项，生成 `config.example.toml`、`.env.example`、`docs/configuration.md`；用户配置目录下的 `config.toml`（或 `COURSE2MD_CONFIG`）；`course2md config [--init]`；doctor 检查配置文件与取值。优先级：命令行 > 环境变量（含 .env）> 配置文件 > 默认值。
- ruff（检查与格式化，一次性格式化全部代码）、mypy（先覆盖 `stages`）、pre-commit、测试标记 `gpu` / `network` / `agent` / `integration`（前三者默认跳过）。
- GitHub Actions：Windows / Linux × Python 3.10–3.13；Dependabot；Issue 与 PR 模板。
- MIT 许可证、CONTRIBUTING、SECURITY、CHANGELOG、README.en.md。
- OpenCV 暂限制在 4.x：5.0 下有一项画面筛选测试结果不同，需单独排查后再放开。
- 干净的公开历史在全部阶段完成后建立（见阶段 7）。

### 1.8 阶段 2：API v1

- `course2md.hub`：FastAPI 应用（`app.py`）、路由（`api/v1.py`）、请求模型（`schemas.py`）、与框架无关的业务逻辑（`service.py`）、本地来源检查中间件（`security.py`）。`http.server` 处理器已删除，`course2md web` 由 uvicorn 提供服务。
- 同一组路由挂在 `/api/v1`（写入 OpenAPI）与 `/api`（旧页面使用）；新增 SSE 端点 `/api/v1/jobs/{id}/events`，日志与运行输出增量推送，任务结束后发送 `end` 事件。
- 错误统一为 `{"error": "中文说明"}`，状态码与旧接口一致（400 / 403 / 404 / 409 / 500）；请求体字段类型保持宽松，取值仍由服务层校验，提示文字不变。
- OpenAPI：`/api/v1/openapi.json`、Swagger 页面 `/api/docs`；`scripts/export_openapi.py` 导出到 `docs/api/openapi.json`，CI 检查是否最新。
- 网页测试改为在 uvicorn 上通过真实 HTTP 连接运行，用例本身未改；打包测试从 wheel 启动新应用。
- 响应体目前是未声明结构的 JSON 对象，前端生成客户端时（阶段 6）补充响应模型。

### 1.9 阶段 3：任务与产物

- 数据库：`core/db.py`（SQLAlchemy，本地 SQLite WAL）与 Alembic 迁移 `core/migrations`。`courses` / `jobs` 沿用旧结构，旧 `workspace.sqlite3` 原地升级；新增 `tasks`、`task_events`、`artifacts`、`workers`。
- 租约队列 `core/queue.py`：带状态条件的 UPDATE 领取，按能力与节点绑定（affinity）筛选；续约时得知取消；过期租约回到队列，超过次数上限标记失败；节点正常退出时立即标记离线。
- 产物存储 `storage/`：按 SHA-256 内容寻址的本地文件存储；阶段输出以确定的 JSON 保存，`artifacts` 记录阶段、节点、模型。
- `QueueExecutor`：`run_job` 不变，阶段经队列交给节点执行；转发进度、运行输出、警告与取消；节点不在线时提示「等待具备某能力的节点上线」；异常类型（ResolverError、LlmError 等）原样还原。
- 节点 `worker/`：`WorkerRunner`（领取、心跳续约、执行、回传，内置与远程共用）与能力检测（`asr`、`frames`、`llm-api`、`agent:*`、`resolver:*`、`hub`）。本地应用启动一个内置节点，Whisper 模型缓存随之移到节点内。
- 阶段复用：缓存键 = 阶段 + 输入产物 + 该阶段相关的选项；`POST /api/v1/jobs/{id}/rerun` 指定起始阶段，例如换整理后端只重新整理。
- 用合成的中文语音视频实测：经队列与内置节点完成真实转写（CPU，large-v3，25 秒音频耗时约 28 秒）。
- 当时的限制（已在 1.15 修复）：视频被清理后，从「画面」阶段重跑缺少原视频；绑定原节点的阶段无法由其他节点接手。

### 1.10 阶段 4：独立 Worker

- 节点接口 `/api/v1/worker/*`（注册、心跳、领取、续约、进度、运行输出、文件下载、产物上传、完成、失败、退出），节点令牌鉴权（只存 SHA-256），不受本地页面来源检查限制。令牌由 `course2md worker-token`、`POST /api/v1/workers` 生成，`DELETE /api/v1/workers/{id}` 停用。
- `course2md worker`：`HttpTransport` + `RemoteWorker`（复用 `WorkerRunner`）。每个任务一个本地目录；路径以 `job://相对路径` 在 Hub 与节点之间转换；执行前下载输入中引用的 Hub 文件，执行后上传本阶段新产生的文件（`media/`、`audio/` 留在节点），Hub 校验摘要后写回任务目录。
- 派发规则：本地文件来源需要 `hub` 能力（只由内置节点处理）；画面阶段绑定执行媒体阶段的节点；输出阶段需要 `hub`；整理可由具备 `agent:<名称>` 或 `llm-api` 的远程节点执行，笔记与配图随结果回传。
- 测试模拟「服务器 Hub（只有 hub 能力）+ 个人电脑节点」：转写、画面、整理在节点完成，截图与笔记回到 Hub，视频不离开节点；命令行实测节点注册与能力上报。
- 当时的限制（已在 1.15 修复）：课程对话在 Hub 进程内运行；节点上下载的视频需要手动清理。

### 1.11 阶段 5：服务器部署

- 账号 `core/accounts.py`：scrypt 加盐口令；首个管理员凭启动日志中的一次性初始化令牌创建（`/api/v1/auth/setup`）；`COURSE2MD_REGISTRATION` 控制开放自助注册；管理页支持改角色与重置口令；会话与 API 令牌只存摘要；停用用户时清除其会话。接口 `/api/v1/auth/*`、`/api/v1/users`；同一地址 10 分钟内登录失败 10 次后返回 429。
- `course2md server`：必须设置 `COURSE2MD_SECRET_KEY`；`COURSE2MD_DATABASE_URL` 指向 PostgreSQL（留空用 SQLite）；信任 `COURSE2MD_TRUSTED_PROXIES` 的转发头；服务器模式只检查写请求来源，不限制 Host。旧页面在 401 时跳转到简单的登录页 `login.html`。
- 隔离：课程、任务带归属；列表与读取只返回自己的记录（旧记录只对管理员可见）；同一来源不同用户各有课程记录；输出目录固定为 `<输出目录>/users/<用户编号>`。任务表带 `owner_id`，个人节点只领取主人的任务，管理员生成的共享节点领取全部。
- 限制普通用户：本地文件来源、自定义整理命令、更换模型接口地址返回 403；配置接口不显示服务器路径。
- 凭证加密 `core/crypto.py`：课程与任务的 `source`、阶段任务的 `options.source` 以 `enc:v1:` 密文保存，领取任务时解密。
- 部署 `deploy/`：多目标 Dockerfile（Hub 精简镜像，计算节点带 cuBLAS / cuDNN 并设置 `LD_LIBRARY_PATH`，可用 `EXTRA_PACKAGES` 预装插件）、`compose.yaml`（Hub + PostgreSQL 17 + Caddy，GPU 节点为可选 profile）、`Caddyfile`（自动 HTTPS、SSE 立即转发）；CI 构建两个镜像并检查 Compose 配置。
- 测试：账号与会话、隔离、限制、加密、节点归属、限流；PostgreSQL 测试在设置 `COURSE2MD_TEST_DATABASE_URL` 时运行。
- 当时未验证：本机 Docker 守护进程未运行，镜像构建与 PostgreSQL 测试未在本地执行（`docker compose config` 已验证）。PostgreSQL 已在 1.15 使用本机临时实例验证，镜像仍由 CI 构建。
- 未做：MinIO / S3 产物存储（产物仍在 Hub 数据卷中）、每个用户自己的模型 API Key。

---

### 1.12 阶段 6：新前端与清理

- `web/`：Vue 3 + Vite + TypeScript、vue-router、TanStack Query；接口类型由 `docs/api/openapi.json` 生成（openapi-typescript + openapi-fetch），任务进度经 SSE 推送并增量合并。页面：新建课程与课次选择、课程视图（对话 / 笔记 / 运行输出 / 阶段与重跑 / 版本历史）、计算服务、插件、账号与用户管理、登录。构建产物输出到 `src/course2md/web_dist`，随 Python 包发布，由 Hub 在 `/` 提供并处理客户端路由。
- 后端：笔记版本历史（`note_versions` 表，查看与恢复）；主要实体的响应模型（OpenAPI 与 TypeScript 类型）。
- 按「上线前不保留向下兼容」清理：删除旧版原生 JS 页面与 `/classic`、`/static`、不带版本号的 `/api`；四个迁移合并为一个初始迁移，早期版本创建的数据库直接拒绝并提示移走；删除从输出目录恢复旧记录、`web-chat.json` 副本、无归属记录、Agent 分段整理接口（plan_parts / run_parts 等）、旧的内置单次整理（build_notes）、`COURSE2MD_MODEL` 别名、`COURSE2MD_AGENT_PARTS` / `WORKERS`、pipeline 的转发导出与零散无用代码；DSH 一律以只读、结构化方式运行。
- CI 增加前端任务：API 类型与 OpenAPI 一致、ESLint + Prettier、vue-tsc、Vitest、构建。
- 浏览器实测：新前端经内置节点创建并运行真实任务，进度经 SSE 更新；本机 `.venv` 未安装 `cuda` 可选依赖时 GPU 转写会失败并在页面显示原因。

### 1.13 界面重做

- 新样式：暖色纸面、衬线标题、居中的阅读栏与浮起的输入框，浅色 / 深色两套主题（默认跟随系统，侧栏可切换）；自定义下拉框；新建课程页的来源框与两行启动面板等宽对齐；按钮文字去掉装饰箭头。

### 1.14 阶段 7：发布准备

- 文档站点（MkDocs Material，`mkdocs build --strict` 纳入 CI）：首页、快速开始、工作原理、计算节点、服务器部署、配置项、插件开发、架构、安全与隐私；README 精简为入口，删除过时说明与旧界面截图。
- 发布工作流 `.github/workflows/release.yml`：推送 `v*` 标签时构建前端与 wheel / sdist（检查版本号与标签一致、包中含前端），经 Trusted Publishing 发布 PyPI，推送 GHCR 的 Hub 与节点镜像，按 CHANGELOG 创建 GitHub Release。
- 版本号 1.0.0；`uv.lock` 要求同时满足 Windows 与 Linux，保证 `cuda` 可选依赖在 Windows 上可装。
- 公开历史：统一使用 `main` 的完整提交历史，提交信息采用 Conventional Commits；旧的独立发布快照分支已清理。
- 发布前需要维护者完成：在 PyPI 登记 Trusted Publishing（仓库与 `release` 环境）、确认仓库地址（`github.com/qch7/course2md`）、推送 `main` 并在待发布提交上打版本标签（如 `v1.0.0`）。

### 1.15 可靠性、导出与测试改进

- 切换账号时清空课程、SSE、查询缓存与整理选项；旧请求返回时不再写回新会话。
- 各课次独立执行，保存运行编号并在重启后接回任务；节点正常退出归还任务，执行与上传期间持续续约。
- 输入文件按内容摘要校验与刷新；远程追问交给计算节点，工作区可迁移到其他节点；抽帧重跑时重新下载缺失的视频，成功后通过原节点清理远程视频。
- 支持 Anki 卡片及保留个人批注的 Obsidian 增量同步，见 [笔记导出与同步](exports.md)。
- CI 加入真实 PostgreSQL 服务；测试隔离 schema，覆盖并发领取、目录去重、过期恢复与续约竞争；增加固定课程评估脚本和内容退化回归。
- 本轮未进行真实 ASR、LLM 或 GPU 课程质量实测；固定样本只能约束结构与内容保留。Docker 镜像构建仍需 CI 验证。

## 二、目标形态

目标是一个规范的开源项目：**一套代码，三种使用形态**。只想本地使用的人一条命令即可运行；需要多设备同步的人可以部署服务器；有算力的机器可以作为计算节点加入。

### 2.1 三种形态

| 形态 | 面向 | 安装 | 组成 |
|---|---|---|---|
| 本地应用 | 个人使用 | `uv tool install "course2md[asr]"` 或 `pipx install`，运行 `course2md` 打开浏览器 | 单进程：Hub + 内置 Worker + 前端，SQLite，数据在用户目录 |
| 服务器 | 多设备同步、多人 | `docker compose up`，配置写在 `.env` | Hub（不含 ASR）+ PostgreSQL + 可选 MinIO + Caddy（自动 HTTPS） |
| 计算节点 | GPU 机器、装有 DSH / Codex 的电脑 | 同一个包，`course2md worker --hub URL --token …`，或 CUDA 镜像 | 领取任务、执行阶段、回传产物 |

本地用户不需要 Node 或 Docker：前端构建产物随 Python 包发布。

### 2.2 架构

```
        浏览器 / 其它客户端（前端 SPA）
                   │ HTTPS · REST /api/v1 + SSE
                   ▼
┌──────────── 中心服务 Hub ─────────────────────────────┐
│ 账号与鉴权 · 课程 / 课次 / 任务 / 笔记版本 · 对话记录  │
│ 阶段任务队列（租约）· 产物存储 · 实时事件               │
└───────────────────────────────────────────────────────┘
          ▲ 节点主动连接 Hub：注册能力、领取任务、回传产物
   ┌──────┴────────────┬──────────────────────┐
 个人电脑 Worker        GPU 服务器 Worker         轻量 Worker
 asr, frames,          asr, frames              llm-api
 agent:dsh, resolver:example
```

**节点主动连接 Hub（拉取模式）**，而不是由 Hub 调用节点地址：

- 个人电脑在 NAT 后面也能接入，不需要公网地址与开放端口；
- 视频留在下载它的节点上，在本地完成抽音频、转写与抽帧，只回传转写与截图；
- 节点离线时任务显示「等待转写服务上线」，而不是失败。

「服务地址」的含义因此分为两处：节点配置 Hub 地址（本地模式即 `http://127.0.0.1:8765`）；前端「计算服务」页按能力为各阶段选择节点（自动或指定）。服务器跑不了 ASR 时，由个人电脑上的节点承担转写，服务器只负责存储与同步。

### 2.3 流水线阶段与产物

| 阶段 | 内容 | 所需能力 | 输入 → 输出 |
|---|---|---|---|
| 媒体 | 解析、下载、抽音频、转写、空隙回填 | `asr` + `resolver:<name>` | 来源 → 分段转写 |
| 画面 | 全课扫描、内容筛选、截图 | `frames`（与媒体同节点） | 视频 → 截图与索引 |
| 校对 | AI 校对 | `llm-api` | 转写 → 校对后转写 |
| 整理 | 读图、课程记录、课程对话 | `agent:dsh` / `agent:codex` / `llm-api` | 转写 + 截图 → `lesson.json` |
| 渲染 | 两版 Markdown 与 ZIP | Hub 内置 | `lesson.json` → 笔记 |

产物按内容哈希存储，并记录生成它的阶段、节点与模型。好处：单独重跑某一阶段（例如只换模型重新整理）、失败阶段单独重试、多个客户端看到同一份结果。

### 2.4 Hub 技术选型

| 方面 | 选择 |
|---|---|
| Web 框架 | FastAPI（Pydantic 定义接口，自动生成 OpenAPI 与前端 TypeScript 客户端） |
| 数据库 | SQLAlchemy + Alembic；本地 SQLite，服务器 PostgreSQL |
| 任务队列 | 数据库表 + 租约（领取后定期续约，超时转交其它节点），不引入 Celery / Redis |
| 实时 | SSE 推送进度与运行输出，取代轮询 |
| 同步 | 服务器为准；笔记带版本号，修改带 `If-Match`，冲突时提示重新加载 |
| 鉴权 | 用户令牌与节点令牌分离；上线后以正式鉴权取代回环地址检查，跨域只允许自己的前端 |

### 2.5 凭证与安全

- 来源链接（可能含凭证）加密保存在 Hub，只在派发时交给执行下载的节点；也可选择只保存在节点本地。
- DSH / Codex 的登录状态与 API Key 留在节点本机；内置模型的 Key 可加密存于 Hub 或节点本地。
- Agent 继续使用只读沙箱；节点不要在受限的沙箱终端中启动。
- 服务器部署默认启用 HTTPS（Caddy）与鉴权；本地模式默认只监听回环地址。

### 2.6 前端

- Vue 3 + Vite + TypeScript（无偏好时的建议）；接口客户端由 OpenAPI 生成；TanStack Query 管理服务端状态；SSE 接收事件。
- 新增页面：计算服务（节点、在线状态、能力、按阶段指定）、插件、账号、笔记版本历史。
- 前后端分开开发、同一个域名部署（Hub 或 Caddy 提供构建产物），省去大部分跨域与 Cookie 问题。现有原生 JS 页面在新前端功能追平前保留。

---

## 三、开源仓库规范

### 3.1 目录结构

```
course2md/
├── src/course2md/
│   ├── core/          领域模型：课程、课次、任务、产物、笔记版本
│   ├── stages/        流水线阶段：media, frames, proofread, organize, render
│   ├── resolvers/     来源解析：插件接口、注册表、本地文件
│   ├── backends/      整理后端：builtin_llm, dsh, codex
│   ├── asr/           faster-whisper 与抽帧
│   ├── hub/           FastAPI：api/v1、鉴权、队列、存储、SSE
│   ├── worker/        节点：注册、心跳、租约、执行、回传
│   ├── storage/       产物存储：本地文件系统 / S3
│   ├── config/        统一配置：TOML + 环境变量，Pydantic 校验
│   ├── testing/       插件兼容性检查工具
│   ├── cli/           app | server | worker | doctor | run | list | refine | sync
│   └── web_dist/      前端构建产物（打包时生成，不进版本库）
├── web/               前端（pnpm）
├── deploy/            Dockerfile（hub 精简 / worker CUDA）、compose.yaml、Caddyfile
├── docs/              MkDocs 文档站：快速开始、部署、配置、架构、插件开发
├── tests/             unit（默认）/ integration / e2e，GPU、网络、真实 CLI 用标记隔离
├── scripts/           生成 API 客户端、发布
├── .github/           CI、Issue 与 PR 模板、Dependabot
├── pyproject.toml  uv.lock  .pre-commit-config.yaml
└── LICENSE  README.md  README.en.md  CONTRIBUTING.md  SECURITY.md  CHANGELOG.md
```

可选依赖按用途拆分：`asr`（faster-whisper、PyAV、OpenCV）、`cuda`（cuBLAS / cuDNN）、`server`（PostgreSQL 驱动、S3 客户端）。服务器 Hub 镜像不装 `asr`。

### 3.2 配置

- 一份 Pydantic Settings Schema，同时生成 `config.example.toml` 与文档中的配置表，避免 `.env.example` 与 README 各写一份。
- 优先级：命令行 > 环境变量（`COURSE2MD_*`）> 配置文件 > 默认值。本地配置文件放在用户配置目录，服务器使用 `.env`。
- 去掉个人路径与 `asrlibs` 特殊加载方式，统一使用标准虚拟环境。
- 首次启动向导：检测 GPU、下载 ASR 模型、选择整理后端与提供商、测试 API Key（把 doctor 做进网页）。

### 3.3 工程规范

| 方面 | 规范 |
|---|---|
| Python | uv 与锁文件；ruff（检查与格式化）；mypy（先覆盖 core 与 hub）；pre-commit |
| 前端 | pnpm、ESLint、Prettier、Vitest；CI 检查生成的 API 客户端与 OpenAPI 一致 |
| 测试 | 默认只跑快速单元测试；GPU、网络、真实 DSH / Codex 测试需显式开启 |
| CI | PR 在 Windows / Linux、Python 3.10–3.13 上运行检查、测试、前端构建、镜像构建 |
| 发布 | 打 tag 后发布 PyPI 与 GHCR 镜像；语义化版本；Release 说明取自 CHANGELOG |
| 数据库 | Alembic 迁移，本地版升级时自动迁移 SQLite |
| API | `/api/v1`，破坏性改动只进入 v2 |

### 3.4 开源前清理

1. **版本历史**：公开前已从当前代码重建干净历史，不包含历史开发中的私有凭据与课程画面。
2. **许可证**：Apache-2.0（含专利条款）或 MIT；核对 faster-whisper、PyAV 等依赖的兼容性，CUDA 镜像注明运行库许可。
3. **个人数据**：`tmp-test/`、截图文档中的课程画面不进入公开仓库。
4. **文档**：中文为主，英文 README 提供概要与快速开始；现有长 README 拆入文档站。
5. **SECURITY.md**：漏洞报告方式与安全边界（本地默认回环、服务器必须鉴权与 HTTPS）。

---

## 四、插件生态

### 4.1 插件类型

所有扩展共用同一套索引、安装与兼容性检查流程。

| 类型 | 入口点组 | 状态 | 例子 |
|---|---|---|---|
| 来源解析 | `course2md.resolvers` | 已实现 | 各高校录播平台、在线课程网站 |
| 整理后端 | `course2md.backends` | 规划 | DSH、Codex、其它 Agent CLI |
| 转写引擎 | `course2md.asr` | 规划 | faster-whisper、FunASR、云端 ASR |
| 导出 | `course2md.exporters` | 规划 | Obsidian、Anki、Notion |

### 4.2 分阶段建设

暂不做托管式市场（上传、审核、评分、下载统计）。原因：生态规模尚小，PyPI 已提供分发与版本管理；插件即任意代码且会接触凭证；托管并主动分发平台解析插件会扩大法律责任。

| 阶段 | 内容 | 前置条件 |
|---|---|---|
| 一：规范与索引 | 包名 `course2md-<站点>` 与 PyPI 关键词 `course2md-plugin`；`course2md.testing.check_resolver` 兼容性检查；`course2md-plugins` 索引仓库，`index.json` 记录包名、类型、简介、主页、兼容接口版本、维护状态，收录 PR 由 CI 安装并检查 | 无，可随时开始 |
| 二：应用内插件页 | 已安装（版本、来源、加载失败原因）、可安装、可升级；安装前确认包名、作者、主页、校验状态与「插件可执行任意代码」提示；装入当前环境后 `reload()` 即时生效；uv tool / pipx 安装的用户给出对应命令；索引获取可关闭 | API v1 与新前端 |
| 三：完整市场 | 发布者认证、签名校验、已验证标识、使用量、问题反馈 | 插件数量与外部贡献者达到一定规模 |

### 4.3 服务器与节点

- 服务器默认禁止网页安装插件（`COURSE2MD_ALLOW_PLUGIN_INSTALL=1` 才开放），插件由管理员在 Dockerfile 或部署配置中声明，保证可重现。
- 解析器运行在执行下载的节点上。节点上报已装插件与版本（如 `resolver:example@0.1.0`），Hub 按能力把任务派给装有对应插件的节点。
- 平台解析插件可以不进入公开索引，由作者自行分发。

---

## 五、实施阶段

每个阶段完成后项目都能照常使用。

| 阶段 | 内容 | 结果 |
|---|---|---|
| 0 仓库规范化 | src 布局、uv、ruff、pre-commit、测试分层、CI、统一配置 Schema、去掉个人路径与 `asrlibs`、确定许可证、建立干净历史 | 可公开的仓库 |
| 1 拆分阶段 | `run_job` 拆成独立阶段，定义产物格式，抽象执行器接口（先只有本地执行器），现有测试保证行为不变 | 后续改造的基础 |
| 2 API v1 | FastAPI 替换 `http.server`，迁移现有接口，生成 OpenAPI；旧页面继续可用 | 前端可以并行开发 |
| 3 任务与产物 | 阶段级任务表、租约队列、产物存储抽象、内置 Worker | 本地应用形态成型 |
| 4 独立 Worker | `course2md worker`、注册与心跳、产物上传下载、按能力派发（含 `resolver:*`） | 「服务器 + 个人电脑」组合可用 |
| 5 服务器部署 | 账号与权限、凭证加密、Docker Compose、HTTPS | 多设备同步 |
| 6 新前端 | 阶段 2 后开始，与 3–5 并行；计算服务、插件、版本历史页面 | 替换现有页面 |
| 7 发布 1.0 | PyPI、GHCR、文档站 | 正式版本 |

插件生态第一步可与阶段 0 同时进行；第二步在阶段 2 与 6 之后。Obsidian 增量同步已实现：`course2md sync <课次输出目录> --vault <路径>`。

工作量最大的是阶段 1 与阶段 4。阶段 1 只重构内部、不改变功能，适合最先做。

---

## 六、待决事项

2026-11-15 已确定：

| 事项 | 决定 | 影响 |
|---|---|---|
| 用户规模 | 同学共用 | 多用户注册、按用户隔离课程；服务器默认 PostgreSQL |
| 视频存放 | 只留在下载节点 | 服务器不存视频；换节点重跑转写需要重新下载 |
| 前端框架 | Vue 3 | Vue 3 + Vite + TypeScript + TanStack Query |
| 许可证 | MIT | 见 LICENSE |
| 公开历史 | 从干净基线公开 | 提交信息采用 Conventional Commits，在发布提交上打版本标签 |

---

## 附：当前使用须知

- 用普通 PowerShell 或资源管理器运行 `start.cmd` 启动服务，不要在受限的沙箱终端中启动。
- 运行环境是项目内的 `.venv`：`uv sync --extra asr --extra cuda`；从源码运行前在 `web/` 中执行 `pnpm install && pnpm build`。`asrlibs` 与 `.venv-asr` 已不再使用，可以删除。
- 早期版本创建的 `workspace.sqlite3` 不再兼容，启动时会提示移走该文件；课程笔记文件不受影响，课程需要重新解析。
- 第三方解析插件装进 `.venv`：`uv pip install -e <插件路径>`。之后再执行 `uv sync` 会移除它，需要重新安装；开发时也可用 `COURSE2MD_RESOLVERS=模块:类` 直接加载源码。
- 运行测试：`uv run pytest -q`；前端：`cd web; pnpm lint; pnpm typecheck; pnpm test`。
