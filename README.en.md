<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg" />
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/logo.svg" />
    <img alt="course2md" src="docs/assets/logo.svg" width="108" height="108" />
  </picture>

  <h1>course2md</h1>

  <p><strong>Turn lecture recordings into structured Markdown notes</strong></p>

  <p>Local transcription · Slide frame selection · AI proofreading & note synthesis · Detailed & brief editions</p>

  <p>
    <a href="https://github.com/qch7/course2md/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/qch7/course2md/ci.yml?style=flat-square&label=CI" /></a>
    <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" />
    <img alt="License MIT" src="https://img.shields.io/badge/License-MIT-ad573d?style=flat-square" />
    <img alt="ASR faster-whisper" src="https://img.shields.io/badge/ASR-faster--whisper-5c5a52?style=flat-square" />
    <img alt="Export Obsidian & Anki" src="https://img.shields.io/badge/Export-Obsidian%20%26%20Anki-6b5b95?style=flat-square" />
  </p>

  <p>
    <a href="README.md">简体中文</a> ·
    English ·
    <a href="docs/index.md">Docs</a> ·
    <a href="docs/getting-started.md">Getting Started</a> ·
    <a href="docs/deploy.md">Deployment</a>
  </p>
</div>

---

Turn lecture recordings (local videos or course-platform links) into structured Markdown notes.

course2md downloads the recording, transcribes it locally with faster-whisper, picks representative slides from the video,
optionally proofreads the transcript with an LLM, and produces two versions of notes (detailed and brief) with figures —
using a built-in OpenAI-compatible API call or an agent CLI (DeepSeek Harness / Codex).

The project and its UI are primarily in Chinese; this page is a short English overview. Full documentation (Chinese) is in [docs/](docs/index.md).

## Install

```bash
uv tool install "course2md[asr,cuda]"   # drop ",cuda" without an NVIDIA GPU
course2md web                          # opens http://127.0.0.1:8765
```

From source (the web UI is built with pnpm):

```bash
uv sync --extra asr
(cd web && pnpm install && pnpm build)
uv run course2md web
```

## Configure

Priority: command-line flags > environment variables (including `.env`) > config file > defaults.
Run `course2md config --init` to create a commented `config.toml`; all settings are listed in [docs/configuration.md](docs/configuration.md).

## Forms

| Form | For | Command |
|---|---|---|
| Local app | personal use | `course2md web` |
| Server | multi-device sync, shared use | `docker compose up` in `deploy/` |
| Compute node | GPU machines, machines with an agent CLI | `course2md worker --hub URL --token ...` |

## Plugins

Course platforms are supported through resolver plugins (`course2md-<site>` packages). See [docs/plugins.md](docs/plugins.md).

## License

[MIT](LICENSE)
