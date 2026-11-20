# 插件开发

course2md 支持来源解析、画面筛选和 Agent 预设三类插件，三者共用同一套发现规则（内置优先、入口点、重名停用，见 `course2md.plugins`）。课程平台解析器的示例见下文与 `tests/fake_resolvers.py`。按视频场景更换筛选策略见[画面筛选插件](#画面筛选插件)。

## 解析器做什么

解析器把用户粘贴的来源变成可以转写的媒体：

1. `matches(source)`：判断来源是否归自己处理；
2. `list_sessions(source)`：列出课次，供用户选择；
3. `resolve(source, session_id, views)`：给出选中课次各路机位的下载地址或本地路径。

下载（断点续传、校验长度）、抽音频、转写、抽帧、整理和输出都由核心完成。

## 在网页中安装与删除

本地应用的「插件」页可以直接管理当前 course2md Python 环境中的来源解析插件：

- **GitHub 链接**：填写插件仓库的 HTTPS 地址，可指定 `/tree/分支或版本`；安装工具需要能够访问 GitHub，并且本机已安装 Git。
- **本地安装包**：选择 `.whl`、`.zip` 或 `.tar.gz` 文件，上传大小不超过 32 MB。
- **本地目录 / 路径**：填写包含 `pyproject.toml` 或 `setup.py` 的插件项目目录，也可填写安装包的完整路径。
- **删除**：卸载对应 Python 插件包，保留课程、笔记及历史记录；内置本地解析器不能删除。通过环境变量开发加载的解析器仍由环境变量管理。

插件必须声明 `course2md.resolvers` 入口点。安装前会在临时目录检查插件元数据，正式安装会限制 course2md 的版本不被更改。
操作使用当前环境对应的 `uv pip`，找不到 uv 时使用 pip；依赖会随插件安装，删除时只卸载插件本身。

请先等待正在处理的课程结束，再修改插件。安装或删除后需要重启应用；完成重启前不能继续解析或提交课程任务。
插件可以执行代码，只安装你信任的来源。服务器形态继续由 `COURSE2MD_PLUGINS` 部署配置管理插件，网页只读；
远程计算节点的插件需要在各节点单独安装。

## 最小示例

```python
# src/course2md_mysite/__init__.py
from course2md.resolvers.api import BaseResolver, MediaRef, ResolverError, SessionInfo, ViewOption


class MySiteResolver(BaseResolver):
    name = "mysite"                     # 唯一标识：小写字母、数字、- 或 _，最长 32 位
    label = "某某课堂"                   # 网页与 doctor 中显示的名称
    source_hint = "粘贴课程回放页地址"     # 网页输入框旁的说明
    priority = 10                       # 数值大的先匹配，内置本地解析器为 -100

    def matches(self, source: str) -> bool:
        return source.startswith("https://class.example.edu/")

    def list_sessions(self, source: str) -> list[SessionInfo]:
        return [SessionInfo(session_id="1024", date="2026-11-09", title="数据结构 2026-11-09 08:00",
                            views=2, course="数据结构", meta={"begin": "2026-11-09 08:00"})]

    def resolve(self, source, session_id=None, views=None) -> list[MediaRef]:
        if session_id not in (None, "1024"):
            raise ResolverError("课次不存在，请重新解析")
        return [MediaRef(url="https://cdn.example.edu/1024/teacher.mp4", name="数据结构_2026-11-09_教师.mp4",
                         view=1, primary=True, meta={"referer": "https://class.example.edu/"})]

    def view_options(self, source: str) -> list[ViewOption]:
        return [ViewOption(1, "教师机位", default=True), ViewOption(2, "屏幕录制")]
```

`pyproject.toml` 中登记入口点：

```toml
[project]
name = "course2md-mysite"
dependencies = ["course2md>=0.2"]

[project.entry-points."course2md.resolvers"]
mysite = "course2md_mysite:MySiteResolver"
```

`pip install` 到 course2md 所在环境并重启，`course2md doctor` 的「来源解析器」一行会列出它。

入口点可以指向解析器类（每次匹配新建实例，推荐）、无参工厂函数或已创建的对象。

## 接口说明

插件只从 `course2md.resolvers.api` 导入。该模块的名称在 `RESOLVER_API_VERSION` 不变时保持兼容；接口有不兼容改动时版本号加一，旧插件会被跳过并在 doctor 与网页启动提示中说明原因。插件的 `api_version` 默认取安装时的版本，继承 `BaseResolver` 即可。

### 必需方法

| 方法 | 说明 |
|---|---|
| `matches(source) -> bool` | 只认领确定属于自己的来源。抛出异常视为不匹配，不影响其它解析器 |
| `list_sessions(source) -> list[SessionInfo]` | 按时间倒序返回课次。本地单文件可返回一个课次 |
| `resolve(source, session_id=None, views=None) -> list[MediaRef]` | `session_id` 为空时取最近一节；`views` 为空时返回全部默认机位 |

失败时抛出 `ResolverError`，消息会原样展示给用户，写明原因和处理办法，例如「登录凭证已过期，请重新复制播放页链接」。

### 可选钩子（`BaseResolver` 已提供默认实现）

| 钩子 | 默认行为 | 何时需要覆盖 |
|---|---|---|
| `public_source(source)` | 去掉 URL 查询串与片段 | **来源中带登录凭证时必须覆盖**。返回值会写入日志、`job.json` 与对话记录，例如 `mysite:course=1024` |
| `course_identity(source)` | 返回来源本身 | 同一门课的链接会变化（凭证过期、重新复制）时覆盖，返回稳定标识，课程记录据此合并 |
| `view_options(source)` | 空列表（网页不显示机位） | 平台提供多路机位时覆盖 |

`public_source` 返回 `"<name>:..."` 形式时，核心会把这个公开描述交回同名解析器的 `course_identity` 识别课程，因此 `course_identity` 需要同时接受原始来源与公开描述。

### 数据结构

**SessionInfo**

| 字段 | 说明 |
|---|---|
| `session_id` | 课次编号，`resolve` 收到的就是它 |
| `date` | `YYYY-MM-DD`，用于推算教学周和输出目录 |
| `title` | 显示标题 |
| `views` | 机位数，仅用于展示 |
| `course` | 课程名，决定输出目录的课程文件夹与侧栏分组 |
| `meta` | 可选：`begin`（`YYYY-MM-DD HH:MM`，用于输出目录名）、`term_start`（开课日期，用于推算第 1 周）、`teacher` 等 |

**MediaRef**

| 字段 | 说明 |
|---|---|
| `url` | `http(s)://` 地址由核心下载，其它值视为本地路径 |
| `name` | 下载后的文件名，同时作为转写文件名 |
| `view` | 机位编号 |
| `primary` | 笔记使用的主音轨。都未标记时依次取机位 1、第一路有讲话的媒体 |
| `meta` | `referer` 会作为下载请求头；`subject`、`begin` 写入转写元信息 |

### 工具

- `download(media, dest, progress=None, session=None)`：与核心相同的断点续传下载，插件需要自行预取时使用。
- `tls_verify()`：遵循用户的 `COURSE2MD_INSECURE_TLS` 设置。插件发起 HTTPS 请求时用 `verify=tls_verify()`，证书错误时抛出 `ResolverError(TLS_HINT)`。
- `parse_date(text)`：把常见日期写法解析为 `date`。

## 开发与调试

不安装也能加载插件，便于调试：

```powershell
$env:PYTHONPATH = "D:\path\to\course2md-mysite\src"
$env:COURSE2MD_RESOLVERS = "course2md_mysite:MySiteResolver"
course2md doctor
course2md list "https://class.example.edu/..."
```

`COURSE2MD_DISABLED_RESOLVERS=mysite` 可临时停用某个解析器。

加载失败（依赖缺失、接口版本不符、名称冲突）的插件不会影响其它解析器，原因显示在 `course2md doctor` 和网页启动提示中。

测试中可以直接通过核心注册表验证插件的接入效果：

```python
from course2md import resolvers

def test_registered(monkeypatch):
    monkeypatch.setenv("COURSE2MD_RESOLVERS", "course2md_mysite:MySiteResolver")
    resolvers.reload()
    assert resolvers.get_resolver("https://class.example.edu/x").name == "mysite"
    assert "token" not in resolvers.public_source("https://class.example.edu/x?token=abc")
```

## 规范

- 只访问用户本来就有权限的内容，不绕过登录、付费或下载限制。
- 不在插件中硬编码任何人的账号或凭证；凭证只来自用户提供的来源。
- 带凭证的来源必须实现 `public_source`。
- 网络请求设置超时，并使用 `tls_verify()`。
- 包名建议使用 `course2md-<站点>`，模块名 `course2md_<站点>`，便于用户搜索。

## 画面筛选插件

视频采样、音轨时间对齐、高清截图保存和索引由核心处理。筛选器接收灰度图，返回要保留的帧。任务在视频抽帧和整理前的截图筛选中使用同一种策略，每路视频或每组截图分别创建实例。

| 策略 | 筛选方式 |
|---|---|
| `classroom`（默认） | 定位投影区域，忽略边缘时间戳和人影遮挡，合并相同课件及动画中间状态，过滤空白页和签到二维码。 |
| `general` | 保留首帧、相邻整幅灰度画面变化较大的采样帧，以及靠近转写重点的帧。整理前合并同组连续的近似截图。 |

```sh
course2md run lecture.mp4 --frame-selector general
course2md refine lecture.srt --media lecture.mp4 --frame-selector general
```

网页「整理选项」中可为下一次整理选择策略，列出内置策略和本机已安装的插件。默认选中项由 `.env` 中的 `COURSE2MD_FRAME_SELECTOR=general` 或 `config.toml` 的 `[notes]` 中的 `frame_selector = "general"` 设置。任务记录所选策略，重跑时沿用该任务的配置。

### 注册自己的策略

入口点可指向接收 `SelectionOptions` 的类或工厂函数。以下示例保留每组的最后一张图。

```python
from course2md.frame_selectors.api import SelectionOptions


class LastFrame:
    def __init__(self, options: SelectionOptions):
        self.last = None

    def add(self, key, gray):
        self.last = key

    def selected(self):
        return [] if self.last is None else [self.last]

    def report(self):
        return {"rule": "last-frame"}
```

```toml
[project.entry-points."course2md.frame_selectors"]
last-frame = "my_frame_plugin:LastFrame"
```

类或工厂上可选的 `label` 与 `description` 属性会显示在网页的策略选择中。

`add(key, gray)` 按顺序接收二维 `uint8` NumPy 数组，按只读输入处理。视频的 `key` 是相对首条音轨（无音轨时相对视频起点）的秒数，截图的 `key` 是输入序号。`selected()` 返回已收到的 key，`report()` 返回可序列化为 JSON 的字典。`SelectionOptions` 提供帧差阈值、最小间隔、数量上限和重点时间供策略参考。超过视频抽帧上限时，核心在已选时间点中均匀取样。

用 `pip` 或 `uv pip` 将插件安装到运行 course2md 的 Python 环境，再用 `--frame-selector last-frame` 或配置项启用。分布式部署把插件装到实际执行抽帧和整理的节点，重启节点后由能力标签参与派发。

## Agent 预设插件

Agent 预设描述如何驱动一个命令行 Agent 整理课程：怎样定位可执行文件、拼装参数与标准输入、把 JSON 事件转成网页事件。
内置 `dsh` 与 `codex`；进程管理、超时、取消、`agent.log` 与会话记录由核心统一处理，插件只描述差异。

```python
import shutil
from course2md.agents.api import AgentPreset, AgentRequest, Invocation

class MyAgent(AgentPreset):
    name = "myagent"
    label = "My Agent"
    template = ("{prefix}", "run", "--json", "-")
    native_schema = False  # CLI 能自行按 JSON Schema 约束输出时设为 True

    def locate(self):
        path = shutil.which("myagent")
        return [path] if path else None

    def prepare(self, cmd, cfg, request: AgentRequest) -> Invocation:
        if request.resume_id:
            cmd += ["--resume", request.resume_id]
        return Invocation(cmd, f"{request.instruction}

{request.input_text}")

    def decode_event(self, event):
        if event.get("type") == "message":
            return {"kind": "assistant", "text": event["text"]}
        return None  # 不认识的事件按日志处理
```

```toml
[project.entry-points."course2md.agents"]
myagent = "course2md_myagent:MyAgent"
```

- 只从 `course2md.agents.api` 导入；`AGENT_API_VERSION` 不变时接口保持兼容，版本不符的插件不会启用。
- `decode_event` 返回的 `kind` 取 `session`（带 `session_id`）、`status`、`assistant`、`tool`、`error`；模型的推理内容不要原样外传。
- 结果默认从工作目录中的 `cfg.output_notes` 读取；CLI 只在事件中给出结果时设 `final_event_is_result = True`，并在最后一条事件里带 `final: True`。
- 安装后选择 `--agent myagent` 或 `COURSE2MD_AGENT=myagent`；节点会报告 `agent:myagent` 能力，整理任务只派给装有该 CLI 的节点。

