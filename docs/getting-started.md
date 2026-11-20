# 快速开始

## 安装

推荐使用 [uv](https://docs.astral.sh/uv/)：

```powershell
uv tool install "course2md[asr,cuda]"   # 没有 NVIDIA 显卡时去掉 cuda
course2md doctor                        # 检查依赖、模型、显卡与密钥
course2md web                           # 打开 http://127.0.0.1:8765
```

| 可选依赖 | 内容 | 适用 |
|---|---|---|
| `asr` | faster-whisper、PyAV、OpenCV | 本地应用、计算节点（转写与抽帧） |
| `cuda` | cuBLAS / cuDNN（CUDA 12） | 用 NVIDIA 显卡转写 |
| `server` | PostgreSQL 驱动、凭证加密 | 服务器形态 |

音频解码走 PyAV，不需要系统安装 ffmpeg。pip 安装的 CUDA 运行库会在启动时自动登记。

### 从源码运行

```powershell
git clone <仓库地址> course2md
cd course2md
uv sync --extra asr --extra cuda
cd web; pnpm install; pnpm build; cd ..   # 构建前端
uv run course2md web
```

Windows 上也可以直接双击 `start.cmd`：首次运行用 uv 创建 `.venv`，然后自检、选择空闲端口并打开浏览器。
`start.ps1 -Cpu` 强制 CPU 转写，`-Check` 只做自检。Linux / macOS 使用 `start.sh`。

## 配置

优先级：命令行参数 > 环境变量（含 `.env`）> 配置文件 > 默认值。

- `course2md config --init` 在用户配置目录生成带注释的 `config.toml`，`course2md config` 显示它的位置。
- 也可以在当前目录放一个 `.env`，模板见仓库中的 `.env.example`。

常用配置：大模型接口 `COURSE2MD_API_KEY` / `COURSE2MD_LLM_BASE` / `COURSE2MD_LLM_MODEL`，整理后端 `COURSE2MD_AGENT`，
转写设备 `COURSE2MD_DEVICE`，输出目录 `COURSE2MD_OUT_DIR`。全部配置项见 [配置项](configuration.md)。

## 网页

1. 在「新建课程」中粘贴课程回放链接或本地视频路径，解析课次。
2. 勾选要整理的课次，选择整理方式，开始整理。已有结果的课次会先确认是否覆盖。
3. 课程视图中可以对话、阅读两版笔记、查看运行输出与各阶段的执行记录、浏览笔记版本。

每个课次写入 `<输出目录>/<课程名>/<课次>/`，课程与任务记录保存在输出目录下的 `.course2md/workspace.sqlite3`。
服务重启时未完成的任务标为「已中断」，从课程中重新选择该课次即可继续；下载阶段会复用已有视频与 `.part` 分片。

教学周按「第 1 周从周一算起」推导：优先使用课程接口提供的开课日期，其次是最早课次所在周，也可以在课次列表上方手动设置，或用 `COURSE2MD_TERM_START` 固定。

## 命令行

```powershell
course2md list "<课程回放链接或视频目录>"                         # 查看课次与机位
course2md run "<课程回放链接>" -o .\计算机网络 --session <课次> --views 1,5
course2md run .\lecture.mp4 -o .\out --no-ai --formats srt,txt,md   # 只转写
course2md refine .\out\lecture.srt -o .\out\lecture.notes.md        # 已有转写稿，只重新整理
course2md refine .\out\lecture.srt --agent codex --media .\lecture.mp4
```

`--llm-base` 可指向任意 OpenAI 兼容地址（DeepSeek、OpenAI、vLLM、Ollama）。多机位任务分别输出各路转写，
笔记采用解析器标记的主音轨，未标记时依次采用机位 1、第一路有讲话的音轨。
