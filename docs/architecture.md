# 架构

```
        浏览器（web/ 构建的前端）
                   │ REST /api/v1 + SSE
                   ▼
┌──────────── Hub（course2md web / server）─────────────┐
│ 账号与鉴权 · 课程 / 任务 · 笔记版本 · 课程对话        │
│ 阶段任务队列（租约） · 产物存储                       │
└───────────────────────────────────────────────────────┘
          ▲ 节点主动连接：注册能力、领取任务、回传产物
   ┌──────┴──────────────┬─────────────────────┐
 内置节点（Hub 进程内）   个人电脑节点            GPU 服务器节点
```

`pipeline.run_job` 依次把阶段交给执行器：命令行使用进程内执行器，Hub 使用队列执行器，阶段由具备相应能力的节点领取执行。
阶段之间只传递可序列化的产物；产物按内容哈希保存，相同输入与设置的阶段可以直接复用之前的结果。

Hub 为各课次独立推进流水线，同一课次的整理与后续对话按顺序执行；等待离线节点的课次不会阻塞其他课次。
运行编号、当前操作、取消意图与阶段任务保存在数据库，Hub 重启后接回原来的阶段任务。
节点执行和上传期间持续续约，正常退出时归还任务；远程追问与视频清理由节点队列执行。
回传文件先在课程目录外暂存，完成任务时在数据库锁内校验租约，再发布文件。
复用结果前检查截图、笔记、配图和转写文件；关联文件已丢失时重新派发该阶段。

Anki 导出使用课程工作区中的结构化记录；Obsidian 同步只复制笔记及其引用的图片，并保留目标中被用户修改的文件。

## 源码

| 目录 | 职责 |
|---|---|
| `src/course2md/stages/` | 流水线阶段（media、frames、proofread、output、organize）、chat、cleanup 与 sessions（在节点上列出课次）任务、阶段注册表、任务选项、产物格式、执行器 |
| `src/course2md/pipeline.py` | 编排阶段、选择主音轨、写出 `job.json`、清理下载的视频 |
| `src/course2md/core/` | 数据库与迁移、阶段任务队列、账号、凭证加密、笔记版本 |
| `src/course2md/storage/` | 按内容寻址的产物存储 |
| `src/course2md/worker/` | 节点执行循环、能力检测、HTTP 客户端（`course2md worker`） |
| `src/course2md/hub/` | FastAPI 应用、`/api/v1` 路由、节点接口、登录、请求与响应模型、业务逻辑 |
| `src/course2md/jobs/` | 任务管理（课程与任务记录、按课次执行、重启恢复、课程对话） |
| `src/course2md/export.py` | Anki 导出与保留本地修改的 Obsidian 增量同步 |
| `src/course2md/asr/` | 音频解码、faster-whisper 转写与空隙回填、幻觉过滤、抽帧与画面筛选、输出格式 |
| `src/course2md/agents/` | Agent 预设接口与注册表、内置 DSH / Codex 预设、统一的 CLI 运行器 |
| `src/course2md/refine/` | AI 校对、课程记录与两版笔记、读图、课程对话 |
| `src/course2md/frame_selectors/` | 画面筛选策略接口、内置策略与注册表 |
| `src/course2md/plugins.py` | 各扩展点共用的插件发现（内置、入口点、环境变量） |
| `src/course2md/errors.py` | 业务错误基类，节点失败后在 Hub 还原为原类型 |
| `src/course2md/resolvers/` | 来源解析插件接口、注册表、本地文件解析 |
| `src/course2md/config/` | 配置 Schema、配置文件与 `.env` 加载 |
| `src/course2md/util/` | 文本、ffmpeg、子进程、`job://` 路径转换等无业务依赖的工具 |
| `web/` | 前端（Vue 3 + TypeScript），接口类型由 `docs/api/openapi.json` 生成 |
| `deploy/` | Dockerfile、Docker Compose、Caddyfile |

## 分层

上层可以导入下层，反之不行；同一行的模块彼此独立。约束写在 `pyproject.toml` 的 `[tool.importlinter]`，由 `uv run lint-imports` 在 CI 中检查。

```
cli
hub
jobs
worker
pipeline
stages
export | doctor
refine
asr
config
agents | resolvers | frame_selectors | core
plugins | storage | util | errors
```

## 接口

所有接口位于 `/api/v1`，运行中的 Hub 在 `/api/docs` 提供交互式文档；OpenAPI 文件随仓库保存在 `docs/api/openapi.json`，
由 `scripts/export_openapi.py` 生成，前端据此生成 TypeScript 类型。任务进度通过 `GET /api/v1/jobs/{id}/events`（SSE）推送。
