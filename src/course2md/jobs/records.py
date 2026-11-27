"""课程与课次记录的结构。

这些记录整行以 JSON 存进数据库（见 :mod:`.workspace_store`），也原样返回给前端，所以用 ``TypedDict``
描述而不是改成类：运行时仍是普通字典，旧数据与旧导出包不需要迁移，但类型检查能发现拼错的键。
新增字段时在这里登记；解析器额外附带的字段（``resolved`` 里的其它内容）会被原样保留，不在此列。
"""

from __future__ import annotations

from typing import Any, TypedDict


class _MessageRequired(TypedDict):
    id: str
    role: str
    text: str
    status: str


class MessageRecord(_MessageRequired, total=False):
    """任务里的一条对话消息；助手消息的 ``status`` 记录该轮修改的进展，``updated`` 表示这一轮是否改动了笔记。"""

    updated: bool


class ViewOption(TypedDict):
    """解析器提供的机位。"""

    id: int
    label: str
    default: bool


class ExistingResult(TypedDict):
    """课次目录里已有的上一次结果。"""

    path: str
    complete: bool
    file_count: int
    updated_at: float
    notes: list[str]


class SessionRecord(TypedDict):
    """课程下的一个课次。"""

    id: str
    date: str
    title: str
    views: int
    week: int | None
    course: str
    meta: dict[str, Any]
    folder: str


class SessionView(SessionRecord):
    """返回给前端的课次：在记录之外带上磁盘上已有的结果与对应任务。"""

    existing: ExistingResult | None
    job_id: str
    status: str


class ResolvedCourse(TypedDict):
    """解析来源后得到的课程信息，由 :func:`remember_course` 合并进课程记录。"""

    course: str
    sessions: list[SessionRecord]
    term_start: str
    term_source: str


class _CourseRequired(TypedDict):
    id: str
    owner: str
    source: str
    out_dir: str
    title: str
    updated_at: float


class _CourseCommon(_CourseRequired, total=False):
    course: str
    term_start: str
    term_source: str
    resolver: str
    resolver_label: str
    view_options: list[ViewOption]
    deleted_at: float | None


class CourseRecord(_CourseCommon, total=False):
    """课程记录。必填项由 ``remember_course`` 保证，其余按解析结果出现。"""

    sessions: list[SessionRecord]


class CourseView(_CourseCommon, total=False):
    """返回给前端的课程：课次带有现状。"""

    sessions: list[SessionView]
