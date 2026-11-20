"""测试用的第三方解析器，模拟插件的各种写法与常见错误。"""

from __future__ import annotations

from course2md.resolvers.api import BaseResolver, MediaRef, SessionInfo, ViewOption


class ClassroomResolver(BaseResolver):
    """完整插件：带凭证的链接、课程标识、多机位。来源形如 fake://course/42?token=SECRET。"""

    name = "fakeclass"
    label = "示例课堂"
    source_hint = "粘贴示例课堂链接"
    priority = 5

    def matches(self, source):
        return source.startswith("fake://")

    def _course(self, source):
        if source.startswith("fakeclass:course="):
            return source.removeprefix("fakeclass:course=")
        return source.removeprefix("fake://course/").split("?", 1)[0]

    def public_source(self, source):
        return f"fakeclass:course={self._course(source)}"

    def course_identity(self, source):
        return f"fakeclass:{self._course(source)}"

    def view_options(self, source):
        return [ViewOption(1, "讲台", True), ViewOption(2, "黑板")]

    def list_sessions(self, source):
        return [
            SessionInfo("s2", "2026-11-09", "示例课 2026-11-09", views=2, course="示例课"),
            SessionInfo("s1", "2026-11-02", "示例课 2026-11-02", views=2, course="示例课"),
        ]

    def resolve(self, source, session_id=None, views=None):
        return [
            MediaRef(f"https://cdn.example/{session_id}/{view}.mp4", f"{view}.mp4", view=view, primary=view == 2)
            for view in (views or [1, 2])
        ]


class MinimalResolver:
    """不继承基类、只实现必需方法的插件，核心用默认行为补齐可选钩子。"""

    name = "minimal"

    def matches(self, source):
        return source.startswith("minimal://")

    def list_sessions(self, source):
        return []

    def resolve(self, source, session_id=None, views=None):
        return []


class FutureApiResolver(MinimalResolver):
    name = "future"
    api_version = 99


class BadNameResolver(MinimalResolver):
    name = "Bad Name"


class DuplicateLocalResolver(MinimalResolver):
    name = "local"


class CrashingResolver(MinimalResolver):
    name = "crashing"
    priority = 50

    def matches(self, source):
        raise RuntimeError("插件缺陷")


def not_a_resolver():
    raise ImportError("缺少依赖 somepackage")
