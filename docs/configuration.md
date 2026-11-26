# 配置项

> 本页由 `scripts/gen_config.py` 根据 `course2md.config.schema` 生成，请勿手改。

每一项既可以写成环境变量（或写进 `.env`），也可以写进配置文件 `config.toml` 的对应表中。
优先级：命令行参数 > 环境变量（含 `.env`）> 配置文件 > 默认值。
配置文件默认位于用户配置目录（`course2md doctor` 会显示），也可以用 `COURSE2MD_CONFIG` 指定。

## 大模型接口（校对、长课证据与内置整理）

配置文件中的表：`[llm]`

| 环境变量 | 配置键 | 默认值 | 说明 |
|---|---|---|---|
| `COURSE2MD_API_KEY` | `api_key` | `（空）` | OpenAI 兼容接口的 API Key。也可用 DEEPSEEK_API_KEY 或 OPENAI_API_KEY。 |
| `COURSE2MD_LLM_BASE` | `base` | `https://api.deepseek.com/v1` | OpenAI 兼容接口地址，例如 https://api.openai.com/v1、http://127.0.0.1:11434/v1（Ollama）。 |
| `COURSE2MD_LLM_MODEL` | `model` | `deepseek-chat` | 内置整理与校对使用的模型名。 |
| `COURSE2MD_CHUNK_CHARS` | `chunk_chars` | `12000` | 每次送给模型的最大字符数，课程过长时自动分块。 |
| `COURSE2MD_LLM_VISION` | `vision` | `0` | 内置接口所用模型支持图像输入时开启。 |

## 整理后端（DSH / Codex / 内置）

配置文件中的表：`[agent]`

| 环境变量 | 配置键 | 默认值 | 说明 |
|---|---|---|---|
| `COURSE2MD_AGENT` | `backend` | `（空）` | 整理后端：dsh、codex、none（内置单次调用）或自定义命令；留空时装了 DSH 就用 DSH。 |
| `COURSE2MD_DSH` | `dsh_command` | `（空）` | DSH 启动命令，留空自动探测桌面版或 PATH。 |
| `COURSE2MD_DSH_HOME` | `dsh_home` | `（空）` | DeepSeek Harness 桌面版安装目录，留空自动探测。 |
| `COURSE2MD_DSH_PROVIDER` | `dsh_provider` | `（空）` | DSH 使用的提供商（须已在 DSH 中登记），留空沿用 DSH 默认设置。 |
| `COURSE2MD_DSH_MODEL` | `dsh_model` | `（空）` | DSH 使用的模型，留空沿用 DSH 默认设置。 |
| `COURSE2MD_CODEX` | `codex_command` | `（空）` | Codex CLI 启动文件，留空从 PATH 探测。 |
| `COURSE2MD_CODEX_PROVIDER` | `codex_provider` | `（空）` | Codex 使用的提供商（须已在 ~/.codex/config.toml 登记），留空沿用其默认设置。 |
| `COURSE2MD_CODEX_MODEL` | `codex_model` | `（空）` | Codex 使用的模型，留空沿用其默认设置。 |
| `COURSE2MD_CODEX_EFFORT` | `codex_effort` | `medium` | Codex 推理强度。可选：minimal, low, medium, high, xhigh。 |
| `COURSE2MD_AGENT_TIMEOUT` | `timeout` | `1800` | Agent 单次任务超时（秒）。 |

## 转写（faster-whisper）

配置文件中的表：`[asr]`

| 环境变量 | 配置键 | 默认值 | 说明 |
|---|---|---|---|
| `COURSE2MD_WHISPER_MODEL` | `model` | `large-v3` | faster-whisper 模型名（large-v3 / medium / small）或本地模型目录。 |
| `COURSE2MD_DEVICE` | `device` | `cuda` | 转写设备。可选：cuda, cpu。 |
| `COURSE2MD_COMPUTE_TYPE` | `compute_type` | `（空）` | 计算精度，留空时 GPU 用 int8_float16、CPU 用 int8。 |
| `COURSE2MD_LANGUAGE` | `language` | `zh` | 转写语言。 |
| `COURSE2MD_BEAM_SIZE` | `beam_size` | `5` | 解码束宽。 |

## 笔记与画面

配置文件中的表：`[notes]`

| 环境变量 | 配置键 | 默认值 | 说明 |
|---|---|---|---|
| `COURSE2MD_DETAIL` | `detail` | `1` | 首选展示详细版笔记；关闭时首选缩略版，两版均会生成。 |
| `COURSE2MD_FRAMES` | `frames` | `1` | 筛选并保存课件画面，供整理与 Markdown 配图使用。 |
| `COURSE2MD_FRAME_INTERVAL` | `frame_interval` | `5.0` | 画面采样间隔（秒），全课扫描最高使用 1 秒。 |
| `COURSE2MD_FRAME_SELECTOR` | `frame_selector` | `classroom` | 画面筛选策略：classroom（教室投影）、general（整幅画面变化）或已安装的插件名称。 |
| `COURSE2MD_MAX_IMAGES` | `max_images` | `12` | 每批最多读取的截图数，整课画面总数不限。 |
| `COURSE2MD_SCREEN_THRESHOLD` | `screen_threshold` | `40` | 去重后的画面超过该张数时，先用缩略图总览初筛，只读取入选画面的原图；0 表示关闭初筛。 |
| `COURSE2MD_VISION_CONCURRENCY` | `vision_concurrency` | `3` | 读图时同时发出的请求数（初筛各轮、读原图各批）；接口限流或本机资源紧张时调小，1 表示逐个发送。 |

## 输出

配置文件中的表：`[output]`

| 环境变量 | 配置键 | 默认值 | 说明 |
|---|---|---|---|
| `COURSE2MD_OUT_DIR` | `dir` | `course2md-output` | 输出目录。 |
| `COURSE2MD_FORMATS` | `formats` | `srt,txt,md` | 转写输出格式，可选 srt、vtt、txt、md、json。 |
| `COURSE2MD_DELETE_VIDEO` | `delete_video` | `1` | 网页任务成功写出产物后清理本任务下载的视频。 |
| `COURSE2MD_TERM_START` | `term_start` | `（空）` | 学期第 1 周起始日（YYYY-MM-DD）；留空时按课程接口或最早课次推断。 |

## 来源解析插件

配置文件中的表：`[resolvers]`

| 环境变量 | 配置键 | 默认值 | 说明 |
|---|---|---|---|
| `COURSE2MD_RESOLVERS` | `extra` | `（空）` | 开发时直接加载的解析器（模块:类），不必安装插件。 |
| `COURSE2MD_DISABLED_RESOLVERS` | `disabled` | `（空）` | 按名称停用的解析器。 |

## 运行环境

配置文件中的表：`[runtime]`

| 环境变量 | 配置键 | 默认值 | 说明 |
|---|---|---|---|
| `COURSE2MD_FFMPEG` | `ffmpeg` | `（空）` | ffmpeg 路径；未安装时用 PyAV 解码音频。 |
| `HF_HOME` | `hf_home` | `（空）` | 模型缓存目录，留空使用 Hugging Face 默认位置。 |
| `HF_ENDPOINT` | `hf_endpoint` | `（空）` | 模型下载镜像。 |

## 网页服务与安全

配置文件中的表：`[server]`

| 环境变量 | 配置键 | 默认值 | 说明 |
|---|---|---|---|
| `COURSE2MD_INSECURE_TLS` | `insecure_tls` | `0` | 关闭 HTTPS 证书校验，仅在可信网络排查证书问题时使用。 |
| `COURSE2MD_ALLOWED_HOSTS` | `allowed_hosts` | `（空）` | 网页服务额外接受的主机名；用 --host 0.0.0.0 从局域网访问时填写。 |

## 服务器部署（course2md server）

配置文件中的表：`[deploy]`

| 环境变量 | 配置键 | 默认值 | 说明 |
|---|---|---|---|
| `COURSE2MD_SECRET_KEY` | `secret_key` | `（空）` | 加密保存来源链接等凭证的密钥，服务器形态必须设置为随机字符串并妥善备份；更换后旧数据无法解密。 |
| `COURSE2MD_DATABASE_URL` | `database_url` | `（空）` | 数据库地址，例如 postgresql+psycopg://user:pass@db/course2md；留空使用输出目录中的 SQLite。 |
| `COURSE2MD_REGISTRATION` | `registration` | `closed` | 是否开放自助注册。closed 时由管理员在「管理」页创建账号；首个管理员凭服务启动日志中的初始化令牌创建，不受此项影响。可选：open, closed。 |
| `COURSE2MD_TRUSTED_PROXIES` | `trusted_proxies` | `127.0.0.1` | 信任其 X-Forwarded-* 头的反向代理地址，逗号分隔；容器部署中 Hub 不对外暴露时可设为 *。 |

## 计算节点

配置文件中的表：`[worker]`

| 环境变量 | 配置键 | 默认值 | 说明 |
|---|---|---|---|
| `COURSE2MD_HUB_URL` | `hub` | `（空）` | 计算节点连接的 Hub 地址。 |
| `COURSE2MD_WORKER_TOKEN` | `token` | `（空）` | 计算节点令牌，由 Hub 的计算服务页或 course2md worker-token 生成。 |
| `COURSE2MD_WORKER_NAME` | `name` | `（空）` | 节点名称，默认取主机名。 |
| `COURSE2MD_WORKER_DIR` | `work_dir` | `（空）` | 节点工作目录，下载的视频与中间文件放在这里。 |
| `COURSE2MD_WORKER_CACHE_DAYS` | `cache_days` | `7` | 节点本地视频缓存（media/）自动清理天数，超过该天数未更新的任务缓存将被删除；设为 0 关闭自动清理。 |
