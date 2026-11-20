# 参与贡献

感谢你愿意改进 course2md。下面是提交改动前需要知道的事。

## 开发环境

```powershell
uv sync --extra asr
uv run pre-commit install
```

- 代码在 `src/course2md/`，测试在 `tests/`。
- `uv run pytest -q` 只跑快速测试。需要显卡、外网或真实 DSH / Codex 的测试分别用 `gpu`、`network`、`agent` 标记，默认跳过，用 `-m` 显式开启。
- `uv run ruff check src tests scripts`、`uv run ruff format src tests scripts`、`uv run mypy`、`uv run lint-imports` 与 CI 一致。
- 工程结构、分层、扩展点与编码规范见 [AGENTS.md](AGENTS.md)。
- 真实 PostgreSQL 测试与固定课程评估见 [测试与评估](docs/testing.md)。

## 约定

- **配置项**只在 `src/course2md/config/schema.py` 中定义，改动后运行 `uv run python scripts/gen_config.py` 并一起提交生成的文件。
- **流水线阶段**位于 `src/course2md/stages/`，继承 `BaseStage` 并自行声明调度信息，在 `stages/registry.py` 登记；阶段之间只传递 `stages/artifacts.py` 中定义的产物；新增产物字段时保持 `from_dict` 向后兼容，不兼容时提升 `ARTIFACT_VERSION`。
- **HTTP 接口**在 `/api/v1` 下，破坏性改动只进入下一个主版本。
- **来源解析器、画面筛选策略、Agent 预设**都可以作为独立插件发布（见 [docs/plugins.md](docs/plugins.md)）；核心仓库只内置本地文件解析、两种筛选策略，以及 DSH / Codex 预设。
- 用户可见的改动写进 [CHANGELOG.md](CHANGELOG.md) 的「未发布」一节。
- 界面文字与文档以中文为主。

## 提交 PR

1. 从 `main` 新建分支。
2. 保证测试与检查通过，补充覆盖新行为的测试。
3. PR 描述写清楚改了什么、为什么、如何验证。

请不要在 Issue、PR 或测试数据中包含 API Key、课程平台的登录凭证、播放链接或课程画面。

## 许可

提交即表示你同意以 [MIT 许可证](LICENSE) 发布你的贡献。
