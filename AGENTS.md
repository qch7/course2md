# AGENTS.md

本文件写给在本仓库工作的 AI 编程助手和新加入的开发者，说明工程结构、扩展方式与必须遵守的规范。
面向用户的说明见 [README.md](README.md)，贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，架构细节见 [docs/architecture.md](docs/architecture.md)。

## 项目一句话

course2md 把课程回放（视频文件或平台链接）转成结构化 Markdown 笔记：解析来源 → 下载 → 转写 → 抽帧筛图 → AI 校对 → 输出转写 → 整理笔记。
可以作为本地应用、带登录的服务器（Hub）或远程计算节点（Worker）运行；每个阶段是一条可派发到不同节点的任务。

## 常用命令

```powershell
uv sync --extra asr --group docs        # 开发环境（服务器相关再加 --extra server）
uv run pre-commit install               # 提交前自动检查
uv run pytest -q                        # 快速测试（gpu / network / agent 标记默认跳过）
uv run ruff check src tests scripts     # 代码检查
uv run ruff format src tests scripts    # 格式化
uv run mypy                             # 类型检查（覆盖整个 course2md 包）
uv run lint-imports                     # 模块分层约束
uv run python scripts/gen_config.py     # 改配置 Schema 后重新生成示例与文档
uv run python scripts/export_openapi.py # 改 Hub 接口后重新导出 OpenAPI
```

前端在 `web/`：`pnpm install`、`pnpm lint`、`pnpm typecheck`、`pnpm test`、`pnpm build`；接口改动后运行 `pnpm gen:api`。

**完成一项改动前，必须让 ruff、ruff format --check、mypy、lint-imports、pytest 全部通过。** CI 在 Ubuntu 与 Windows、Python 3.10–3.13 上运行同样的检查。

## 目录与分层

上层可以导入下层，反之不行；同一行的模块彼此独立。约束写在 `pyproject.toml` 的 `[tool.importlinter]`，违反会让 CI 失败。

| 层 | 模块 | 职责 |
|---|---|---|
| 入口 | `cli` | 命令行 |
| 接口 | `hub` | FastAPI 应用、`/api/v1`、节点接口、登录；`hub/service/` 是与 HTTP 无关的业务封装，按领域拆成子模块 |
| 应用 | `jobs` | 任务与课程生命周期：`manager` 编排，`job` 运行时对象，`records` 记录结构，`serialization` 入库转换，`trash` 回收站，`bundle` 产物包 |
| 节点 | `worker` | 领取并执行阶段任务、能力检测、与 Hub 通信 |
| 编排 | `pipeline` | 按顺序把阶段交给执行器，写 `job.json` |
| 阶段 | `stages` | 各阶段实现、阶段注册表、任务选项、产物、执行器、编解码 |
| 领域 | `export`、`doctor` → `refine` → `asr`、`frames` | 导出与自检；校对、笔记生成与读图（`refine/vision`）；音频与转写；抽帧与截图筛选 |
| 配置 | `config` | 配置 Schema、环境变量、`.env` |
| 扩展点与存储 | `agents`、`resolvers`、`frame_selectors`、`core` | Agent 预设、来源解析、画面筛选；数据库、队列、账号 |
| 基础 | `plugins`、`storage`、`util`、`errors` | 插件发现、产物存储、工具函数、业务错误基类 |

需要从下层调用上层时，说明设计有问题：把共享的东西下沉（例如 `JobOptions` 放在 `stages/options.py`，`job://` 路径工具放在 `util/job_refs.py`），或者改为由上层注入回调。不要用函数内延迟导入来绕过分层检查。

## 扩展点

新增能力时优先找下面的扩展点，不要在调用处加 `if name == "xxx"` 分支。

### 新增流水线阶段

1. 在 `src/course2md/stages/` 新建模块，继承 `BaseStage[输入, 输出]`（`stages/base.py`）。
2. 声明调度信息，执行器、编解码和节点都只读这些声明：
   - `name`：稳定标识，写进任务队列和缓存键，改名会使旧缓存失效；
   - `label`：中文展示名；
   - `cache_options`：输出依赖的 `JobOptions` 字段，其它字段变化时可以复用旧输出；
   - `requires(options, inputs)`：执行需要的节点能力，能力名见 `worker/capabilities.py`；
   - `model(options)`：记录在产物上的生成模型；
   - `anchors_node` / `pinned_to_anchor` / `fallback_requires`：需要与持有原视频的节点同机运行时使用；
   - `create(**resources)` / `resources()`：需要在节点上跨任务复用资源（如已加载的模型）时覆盖。
3. 在 `stages/registry.py` 的 `_STAGES` 中登记；属于主流程、需要支持从该阶段重跑时，同时加进 `PIPELINE_STAGES`。
4. 输入输出只用 `stages/artifacts.py` 中可 `to_dict` / `from_dict` 的产物，阶段的输入输出要能跨节点序列化。
5. 在 `pipeline.run_job` 中编排，`tests/test_extensibility.py` 的阶段约定测试会检查声明是否完整。

### 新增插件类扩展点

来源解析器（`resolvers`）、画面筛选策略（`frame_selectors`）和 Agent 预设（`agents`）都通过 `course2md.plugins` 发现：内置实现优先，再读取入口点，同名插件全部停用，单个插件加载失败只记录原因。
需要新的扩展点时：

1. 建一个 `api.py`，放公开接口和版本号常量（如 `AGENT_API_VERSION`），只依赖基础层；
2. 用 `PluginRegistry(kind, group, builtins, validate=...)` 建注册表；
3. 在 `pyproject.toml` 的 import-linter「Plugin APIs」约束中加入新的 `api` 模块；
4. 在 `docs/plugins.md` 写插件开发说明，在 `tests/test_extensibility.py` 补接入测试。

第三方插件**只能**从各自的 `api` 模块导入。改动 `api` 模块的公开名称属于破坏性改动，需要提升对应的 API 版本号。

### 新增 Agent 预设

继承 `agents.api.AgentPreset`，实现 `locate`、`prepare`、`decode_event`，可选实现 `model_options`，并按需设置 `native_schema`、`final_event_is_result`。
内置预设在 `agents/registry.py` 的 builtins 中登记，第三方预设通过 `course2md.agents` 入口点登记。
进程管理、超时、取消、日志与会话记录都在 `agents/runner.py`，预设里不要另起子进程逻辑。

### 新增配置项

只在 `config/schema.py` 定义，然后运行 `scripts/gen_config.py`，并提交它生成的 `config.example.toml`、`.env.example` 和 `docs/configuration.md`。
需要随任务下发的选项加进 `stages/options.py` 的 `JobOptions`，并检查哪些阶段的 `cache_options` 需要包含它；密钥类字段在 `codec.options_to_dict` 中去掉，由节点从本机环境读取。

### 新增 HTTP 接口

路由写在 `hub/api/`，业务逻辑写在 `hub/service/` 下对应领域的子模块（并在其 `__init__.py` 导出）或 `jobs/`，请求与响应模型写在 `hub/schemas.py` 和 `hub/responses.py`。
改动后导出 OpenAPI，并在 `web/` 重新生成类型。`/api/v1` 的破坏性改动只能进入下一个主版本。

## 编码规范

- **语言**：注释、文档字符串、界面文字、错误消息和文档都用中文；标识符用英文。
- **注释写“为什么”**：说明约束、实测结论和取舍，不复述代码在做什么。
- **类型**：新代码完整标注类型；不要用 `Any` 掩盖结构，跨模块传递的数据用 dataclass、TypedDict 或 pydantic 模型。`# type: ignore` 必须带错误码，并说明原因。
- **函数规模**：ruff 的 `PLR0915`（语句数）与 `PLR0912`（分支数）保持开启。已有的长函数带有 `# noqa: PLR0915  历史遗留的长函数，拆分后移除` 标记；新代码不要新增这类标记，拆分旧函数时顺手删掉对应标记。
- **跨模块访问任务管理器**：`hub`、`jobs/bundle` 等模块只用 `JobManager` 的公开方法（`locked()`、`save_job()`、`record_version()`、`shared()`）和 `Job.locked()`，不要访问 `_lock`、`_save` 等私有成员。锁顺序固定为先管理器锁、后任务锁。
- **平台判断**：用 `sys.platform == "win32"`，mypy 能据此在各平台收窄类型。子进程使用 `util/proc.py` 的 `NO_WINDOW` 和 `kill_tree`。
- **路径**：使用 `pathlib.Path`；写文件显式指定 `encoding="utf-8"`；覆盖写入时先写临时文件，再原子替换。
- **错误**：
  - 用户能理解、能处理的失败，抛 `errors.Course2mdError` 的子类（如 `ResolverError`、`LlmError`、`AgentError`），消息写清原因和下一步；
  - 子类会按类名自动登记，节点上的失败到 Hub 后还原为同一类型，所以类名在包内必须唯一；
  - 程序缺陷不要包装成业务错误；
  - 只在边界处使用 `except Exception`（插件调用、任务主循环、后台线程），并写 `# noqa: BLE001  原因`。
- **可选依赖**：`faster_whisper`、`av`、`cv2`、`psycopg`、`cryptography` 只能在函数内导入，保证核心依赖下也能导入模块（见 `tests/test_optional_deps.py`）。
- **取消**：阶段在每个耗时步骤之间调用 `ctx.check()`，长循环接受 `check_cancel` 回调。
- **进度**：通过 `ctx.progress(阶段, 总进度, 说明, 子进度)` 汇报，不要直接 `print`。
- **安全**：日志、产物和错误消息中的来源一律经过 `resolvers.public_source()`；不记录 API Key、Cookie 或播放链接；节点回传的路径必须校验仍在任务目录内。
- **兼容**：产物新增字段时保持 `from_dict` 向后兼容，不兼容时提升 `ARTIFACT_VERSION`；数据库改动通过 `core/migrations` 新增 Alembic 迁移。

## 测试规范

- 每个行为改动都要有测试覆盖。修复缺陷时先写能复现问题的测试。
- 默认测试不能访问网络、显卡或真实 Agent CLI；确实需要时分别标记 `network`、`gpu`、`agent`。
- 替换实现时，patch 定义它的模块，例如 `monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", ...)`，不要 patch 重新导出的名称。
- 测试插件时伪造入口点：`monkeypatch.setattr("importlib.metadata.entry_points", ...)`，并在测试结束后调用对应注册表的 `reload()`。
- 跨模块或启动子进程的慢测试标记 `integration`。
- PostgreSQL 相关改动要在真实实例上运行 `tests/test_postgres.py`，步骤见 [docs/testing.md](docs/testing.md)。

## 提交与变更记录

- 提交信息使用 Conventional Commits：`feat(scope): …`、`fix(scope): …`、`refactor: …`、`test: …`、`docs: …`。
- 用户可见的改动写进 `CHANGELOG.md` 的「未发布」一节。
- 不提交 `course2md-output/`、`output/`、`hf-cache/`、`asrlibs/`、`tmp-test/`、`pytest-of-*/` 等本地数据，也不提交课程视频、截图或任何凭证。
