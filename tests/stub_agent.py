"""假 Agent：验证 course2md 的 agent 通道（读 task.md → 写 notes.md / proofread.md）。"""

import pathlib

ws = pathlib.Path.cwd()
transcript = (ws / "transcript.md").read_text(encoding="utf-8")
lines = [ln for ln in transcript.splitlines() if ln.strip()]
fixed = [ln.replace("对列", "队列").replace("QRT", "QUIT") for ln in lines]
(ws / "proofread.md").write_text("\n".join(fixed) + "\n", encoding="utf-8")

(ws / "notes.md").write_text(
    """# 测试课程

> 讲解 FTP 的两种连接与命令查看方式。

## 本节导航

- [00:00:00] 早期协议的共同特点
- [00:00:24] 用 CMD 查看 FTP 命令

## 核心概念

| 概念 | 一句话解释 | 课堂细节 |
| --- | --- | --- |
| 控制连接 | 传命令 | 登录与命令走这条连接 [00:00:59] |
| 数据连接 | 传文件 | 传完即断开 [00:00:59] |

## 术语与缩写

| 术语 | 含义 | 首次出现 |
| --- | --- | --- |
| FTP | 文件传输协议 | [00:00:24] |

## 机制与流程

1. 建立控制连接
2. 按需建立数据连接

## 实操与演示

- CMD 输入 `ftp` 后打 `?` 查看命令 [00:00:24]

## 易错点与考点

- 两种连接的分工 [00:00:59]

## 复习清单

- [ ] 说出控制连接与数据连接的区别

## 存疑与待补充

- 无
""",
    encoding="utf-8",
)
print("stub agent done")
