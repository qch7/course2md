<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg" />
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/logo.svg" />
    <img alt="course2md" src="docs/assets/logo.svg" width="108" height="108" />
  </picture>

  <h1>course2md</h1>

  <p><strong>把课程回放变成可以复习的结构化 Markdown 笔记</strong></p>

  <p>本地语音转写 · 课件画面筛选 · AI 校对与整理 · 详细版 / 缩略版双笔记</p>

  <p>
    <a href="https://github.com/qch7/course2md/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/qch7/course2md/ci.yml?style=flat-square&label=CI" /></a>
    <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" />
    <img alt="License MIT" src="https://img.shields.io/badge/License-MIT-ad573d?style=flat-square" />
    <img alt="ASR faster-whisper" src="https://img.shields.io/badge/ASR-faster--whisper-5c5a52?style=flat-square" />
    <img alt="Export Obsidian & Anki" src="https://img.shields.io/badge/Export-Obsidian%20%26%20Anki-6b5b95?style=flat-square" />
  </p>

  <p>
    简体中文 ·
    <a href="README.en.md">English</a> ·
    <a href="docs/index.md">文档</a> ·
    <a href="docs/getting-started.md">快速开始</a> ·
    <a href="docs/deploy.md">服务器部署</a>
  </p>
</div>

---

把课程回放（视频文件或平台链接）转成结构化 Markdown 笔记：本地转写，筛选课件画面，AI 校对与整理，生成共用一份课程记录的详细版与缩略版，并支持导出 Anki 卡片与增量同步到 Obsidian。

- **本地应用**：一条命令安装运行，数据留在自己电脑上。
- **服务器**：Docker Compose 部署，同学共用，按账号隔离。
- **计算节点**：有显卡或装有 DSH / Codex 的电脑接入 Hub，承担转写与整理。

## 安装与运行

```powershell
uv tool install "course2md[asr,cuda]"   # 没有 NVIDIA 显卡时去掉 cuda
course2md doctor                        # 检查依赖、模型、显卡与密钥
course2md web                           # 打开 http://127.0.0.1:8765
```

从源码运行：

```powershell
uv sync --extra asr --extra cuda
cd web; pnpm install; pnpm build; cd ..
uv run course2md web                    # Windows 也可以双击 start.cmd
```

AI 校对与整理需要配置大模型接口（`course2md config --init` 生成配置文件），或安装 DeepSeek Harness / Codex CLI。

## 文档

| 主题 | 内容 |
|---|---|
| [快速开始](docs/getting-started.md) | 安装、配置、网页与命令行 |
| [工作原理](docs/how-it-works.md) | 输出结构、画面参考、整理后端、校对与转写参数 |
| [笔记导出与同步](docs/exports.md) | Anki 卡片导出与 Obsidian 增量同步 |
| [计算节点](docs/workers.md) | 让其它电脑承担转写与整理 |
| [服务器部署](docs/deploy.md) | Docker Compose、账号、HTTPS |
| [配置项](docs/configuration.md) | 全部配置项（由 Schema 生成） |
| [插件开发](docs/plugins.md) | 为课程平台编写来源解析插件 |
| [架构](docs/architecture.md) | 模块划分与接口 |
| [安全与隐私](docs/security.md) | 安全边界与合规提醒 |

文档站点：`uv run --group docs mkdocs serve`。

## 开发

```powershell
uv sync --extra asr                  # 运行与开发依赖
uv run pytest -q                     # 快速测试（默认跳过 gpu / network / agent 标记）
uv run ruff check src tests scripts  # 代码检查；格式化用 ruff format
uv run mypy                          # 类型检查
cd web; pnpm lint; pnpm typecheck; pnpm test
```

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题见 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT](LICENSE)。课程录播通常带有版权声明，仅可用于个人复习；本项目只应访问你本来就有权限的内容。
