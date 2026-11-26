# 笔记导出与同步

## Anki

课程整理完成后，在「笔记」页点击“导出 Anki 卡片”，把下载的 UTF-8 TSV 文件导入 Anki，字段依次为 Front、Back、Tags。
卡片包含知识点的关键结论及例题的题干、步骤、答案，保留公式、时间戳与待核对说明。
通用复习问题没有可靠的标准答案，因此不自动生成卡片。

命令行也可导出：

```powershell
course2md export "D:\课程输出\某课次" --format anki -o "D:\复习卡片.tsv"
```

## Obsidian 增量同步

```powershell
course2md sync "D:\课程输出\某课次" --vault "D:\我的知识库"
```

源路径是含 `notes/` 的课次输出目录。笔记及引用的本地图片同步到 vault 的 `course2md/<课次名>-<路径摘要>/`，保留相对链接。
重复执行只写入变化的文件，来源凭证、转写工作区及运行日志不参与同步。用 `--folder` 改变 vault 内的父目录。

如果你在 Obsidian 中修改了已同步的文件，再次同步会保留修改、列出冲突并返回退出码 1；手动合并后再同步。
已从课程中移除的文件不会自动从 vault 删除，便于保留个人整理内容。

## Hub 间课程产物互传与离线产物包

可以将已完成整理或转写的课程与课次产物（笔记、转写、关键帧、版本历史、追问记录与 `job.json` 元数据）在不同 Hub 之间直接推送/拉取，或导出为可移植的 `.c2m.zip` 产物包离线迁移。
打包时会自动跳过原视频、音频与阶段缓存，并剥离平台 Cookie、API Key 及播放链接等敏感信息；导入时校验校验和与路径安全，且不会覆盖目标端已有课次的已认证来源地址。

### 网页操作

1. 在左侧栏打开「课程互传」（或从课次页顶栏点击“互传” / “导出产物包”）。
2. **对端 Hub 管理**：保存常用的远端 Hub 地址与 API 令牌（令牌在本地使用 `FieldCipher` 加密保存，页面仅显示脱敏提示）。服务器形态的 Hub 需在对应账号的「账号」页创建 API 令牌，本地形态（`course2md web`）无需令牌。
3. **推送 / 拉取**：选择已保存的对端或临时填写地址，勾选课程或课次并指定同名冲突策略（跳过已有课次 / 覆盖已有课次 / 另存为新副本）。
4. **离线产物包（`.c2m.zip`）**：两个 Hub 无法直接互通时，可先导出整门课程或单节课次的 `.c2m.zip`，再到目标 Hub 上传导入。

### 命令行互传

```powershell
# 查看指定 Hub 上可传输的课程与课次
course2md transfer list --hub https://lab.example.com --token "<API令牌>"

# 在两个 Hub 之间直接复制整门课程或指定课次
course2md transfer copy --from http://127.0.0.1:8765 --to https://lab.example.com --to-token "<API令牌>" --course <课程ID>
course2md transfer copy --from http://127.0.0.1:8765 --to https://lab.example.com --to-token "<API令牌>" --job <任务ID> --conflict overwrite

# 导出或导入离线 .c2m.zip 产物包
course2md transfer export --hub http://127.0.0.1:8765 --course <课程ID> -o "D:\高等数学.c2m.zip"
course2md transfer import "D:\高等数学.c2m.zip" --hub https://lab.example.com --token "<API令牌>" --conflict skip
```

### 服务器形态的网络安全限制

在服务器形态（`course2md server`）下，为防止 SSRF，默认禁止普通用户通过 Hub 向内网或本机回环地址发起推送/拉取（管理员不受此限，云元数据与链路本地地址始终禁止）。
如果部署在受信任内网且需要允许普通用户与内网 Hub 互传，可在环境变量或配置文件中设置 `COURSE2MD_ALLOW_PRIVATE_TRANSFER=1`。
