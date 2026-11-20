# 工作原理

## 输出结构

```
<课次目录>/
├── media/                    下载的原始视频（本地来源时为空；可在完成后清理）
├── audio/<名字>.wav           中间音频（选择保留时）
├── frames/                   课堂截图与时间索引
├── <名字>.srt / .vtt / .txt
├── <名字>.md                 带 **[时:分:秒]** 锚点的转写稿
├── <名字>.meta.json          课程名、节次、机位、转写引擎等
├── notes/<名字>.notes.md     详细版
├── notes/<名字>.brief.md     缩略版
├── notes/<名字>.assets/      笔记引用的课堂截图
├── notes/<名字>.notes.zip    两版笔记及所用配图
├── notes/agent-workspace/    每次整理的输入、课程记录与运行日志
└── job.json                  本次任务的汇总（主音轨、各机位文件、耗时、警告）
```

配图使用相对链接，移动笔记时解压 ZIP 即可保持目录关系。下载先写入 `.part`，校验长度后再重命名。

## 画面参考

转写稿只能还原老师说了什么，PPT 的表格、终端命令、拓扑图、板书都在画面里。

1. 以最多 1 秒的间隔扫描全片，把画面交给筛选策略。默认的 `classroom` 识别投影区域并比较课件内容，`general` 根据整幅画面的变化选图。快速翻页可用 `--frame-interval 0.5` 加密采样。[画面筛选插件](plugins.md#画面筛选插件)
2. AI 按 `COURSE2MD_MAX_IMAGES` 分批读取代表图：Codex 通过 `--image` 接收，DSH 按路径打开，内置接口在 `COURSE2MD_LLM_VISION=1` 时启用视觉输入。每张图返回内容与可读状态，遗漏的图最多重试三次，覆盖报告保存为 `vision-coverage.json`。内容相同的截图复用读取结果。
   去重后的代表图超过 `COURSE2MD_SCREEN_THRESHOLD`（默认 40）张时先做两段式读图：每 12 张缩略图拼成一张带编号与时间的总览图，每次请求最多附 4 张总览图，由模型把每张图判为细读、重复（指向内容更完整的一张）或跳过（人像、空白、转场等）；之后只读入选画面的原图，重复图沿用所指画面的结果，跳过的图记为 irrelevant。初筛拿不准或回复无效时一律读原图，所以初筛只省预算，不会在没有依据时丢掉画面。教室全景里有板书或屏幕外内容时不判为重复，初筛只允许查看总览图。设为 0 关闭初筛。
   初筛各轮、读原图各批互不依赖，按 `COURSE2MD_VISION_CONCURRENCY`（默认 3）同时发出；任一请求失败时其余请求随之停止。Agent CLI 的启动阶段逐个进行（输出第一行后再启动下一个），避免 DSH 同时改写 `~/.dsh` 中的配置。
3. 模型生成包含主题、完整例题、教师原话引用、复习问题与待核对项的课程记录，程序检查字段、时间戳与图像引用。
4. 详细版在对应位置嵌入选中的截图，缩略版每个主题保留最多三条结论，另列例题答案与复习清单。

## 整理后端

| | DeepSeek Harness | Codex CLI | 内置模型 |
|---|---|---|---|
| 调用方式 | `dsh headless`，材料经标准输入传入 | `codex exec`，文字经标准输入、截图经 `--image` | OpenAI 兼容接口 |
| 模型与鉴权 | DSH 自身配置 | Codex 自身配置与登录态 | `COURSE2MD_LLM_*` 与 API Key |
| 权限 | 只读 | 只读沙箱 | — |

装有 DSH 时默认使用它；`COURSE2MD_AGENT=none` 或 `--agent none` 强制内置模型。DSH 的查找顺序为 `COURSE2MD_DSH`、PATH 中的 `dsh`、
桌面版安装目录（`COURSE2MD_DSH_HOME`、`%LOCALAPPDATA%\Programs\DeepSeek Harness`、`C:\Program Files\DeepSeek Harness`），以及 Windows 安装记录中的自定义安装目录。

提供商与模型默认沿用各自 CLI 的配置，可在网页「整理选项」、命令行 `--agent-provider` / `--agent-model`，或
`COURSE2MD_DSH_PROVIDER` / `COURSE2MD_DSH_MODEL`、`COURSE2MD_CODEX_PROVIDER` / `COURSE2MD_CODEX_MODEL` 中指定，优先级依次降低。
提供商须已在对应 CLI 中登记。

转写不超过 `COURSE2MD_CHUNK_CHARS` 时，读图后做一次整课结构化整理；更长的课先分段提取例题、原话与规则证据，再整课整理。
两版 Markdown 由程序渲染，共用课程事实。

整理工作目录（`notes/agent-workspace/<编号>/`）保存 `context.json`（转写、课程信息、图像索引）、`lesson.json` 与
`lesson.current.json`（原始回复与当前有效的课程记录）、`frame-selection.json`、`vision-coverage.json`、`turns/`（每轮对话）
与 `agent.log`。格式或引用检查失败时保留回复与输入，任务报告失败，可以从「整理」阶段重新运行。

## AI 校对

校对按时间戳分块发给模型，只纠正同音字、专有名词、英文缩写与明显错乱的数字。模型返回 `{ts, old, new}` 修改清单，
时间戳与原文必须共同唯一匹配一个片段，缺少原文、匹配歧义或重复修改的条目被拒绝，原片段保留。
`--no-proofread` 只关闭校对，`--no-ai` 关闭全部 AI 步骤。

## 转写参数

| 参数 | 取值 | 原因 |
|---|---|---|
| `condition_on_previous_text` | `False` | 长音频里开头一句幻觉会被当作上下文，导致整片复读 |
| 幻觉过滤 | 仅文本黑名单 | 远场录音的真实讲话 `no_speech_prob` 也常高于 0.85，按概率过滤会误删 |
| 空隙回填 | 间隔超过 3 秒时重转 | VAD 会把低声讲话整段切掉，只有在整段时间轴上才看得出来 |
| 响度归一化 | RMS −20 dBFS | 教室远场电平低，归一化后漏句明显减少 |

## 已知限制

- 转写准确率受音质影响，远场录音里的同音字、学生姓名仍会出错，人工复核仍有必要。
- 模型可能漏掉细节或误读符号，可对照工作目录中的转写、课程记录与视频时间核对；短暂展示的画面可能漏采。
- 同一节点上的转写串行执行（显存考虑），多个任务会依次处理；多台计算节点可以并行。
- 课程对话在 Hub 进程内运行，使用 DSH / Codex 对话时 Hub 所在电脑需要装有对应 CLI。
