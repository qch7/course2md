# 测试与固定课程评估

## 常规检查

```powershell
uv sync --extra asr --extra server --group docs
uv run pytest -q --cov    # 覆盖率下限见 pyproject.toml 的 [tool.coverage.report]
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy
uv run lint-imports
uv run python scripts/gen_config.py --check
uv run python scripts/export_openapi.py --check
uv run --group docs mkdocs build --strict
```

在 `web/` 执行 `pnpm lint`、`pnpm typecheck`、`pnpm test`、`pnpm build`。
修改接口后运行 `pnpm gen:api` 并提交生成文件。

## 测试目录

`tests/` 按被测的包分子目录（`hub`、`jobs`、`stages`、`worker`、`core`、`refine`、`frames`、`asr`、`agents`、`config`、`resolvers`、`export`、`util`），新测试放进对应目录。
跨包的约定检查（可选依赖、打包、扩展点）留在 `tests/` 根目录。`conftest.py`、`live_server.py`、`fake_resolvers.py`、`stub_agent.py`、`resolver_patch.py` 是公共辅助，子目录里直接 `import` 即可（`pythonpath` 已包含 `tests`）。

## 真实 PostgreSQL

CI 单独启动 PostgreSQL 17 服务，安装 `server` 依赖并运行 `tests/core/test_postgres.py`。
本地可连接专用测试实例：

```powershell
$env:COURSE2MD_TEST_DATABASE_URL = "postgresql+psycopg://test_user:test_password@127.0.0.1:55432/test_db"
uv run pytest -q tests/core/test_postgres.py
```

每个测试创建随机命名的 schema，结束时删除；测试账号需要对应权限。
覆盖迁移后重新打开、课程目录原子去重、多个节点并发领取、过期任务接管、续约与回收竞争及取消后的完成拒绝。
未设置连接时明确跳过这些测试。2026-11-16 在本机临时 PostgreSQL 18.1 实例上验证通过；CI 配置使用部署所用的 17 版本。

## 固定课程内容检查

`tests/fixtures/review-course.json` 是人工构造的固定样本，包含转写片段、公式、例题步骤、教师原话引用和不确定项。
测试使用生成的小图片验证引用和导出路径，检查缺少公式、步骤、原话、图片或待核对内容时能发现退化。

对同一课程保存的整理工作区执行：

```powershell
uv run python scripts/evaluate_course.py "<课次目录>/notes/agent-workspace/<编号>" --expectations tests/fixtures/review-course.json
```

脚本检查 Schema 和引用有效性、两版笔记中的指定词句、例题与步骤数量、配图文件和教师原话覆盖，输出 JSON 报告；不满足时退出码为 1。
新课程应单独建立包含 `expected` 的预期文件，再用同一课程在不同模型或配置下的输出进行比较。

这项检查不调用模型或 ASR，也不判断转写准确率、推导是否正确、截图是否清晰。
真实课程质量需在有授权的视频上运行 ASR 和整理流程，由人工对照视频核验，并记录模型、配置、耗时与成本。
