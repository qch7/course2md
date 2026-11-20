"""来源解析器插件机制：发现、校验、优先级、可选钩子与故障隔离。"""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest

from course2md import resolvers
from course2md.resolvers import registry


@pytest.fixture
def plugins(monkeypatch):
    """按需登记测试插件，结束后恢复注册表。"""

    def use(*specs: str, entry_points: tuple[EntryPoint, ...] = ()):
        monkeypatch.setenv("COURSE2MD_RESOLVERS", ",".join(f"fake_resolvers:{spec}" for spec in specs))
        monkeypatch.setattr("importlib.metadata.entry_points", lambda group: list(entry_points))
        resolvers.reload()

    yield use
    monkeypatch.delenv("COURSE2MD_RESOLVERS", raising=False)
    resolvers.reload()


def test_core_only_ships_local_resolver(plugins):
    plugins()
    assert [(info.name, info.origin) for info in resolvers.available()] == [("local", "builtin")]
    assert resolvers.load_errors() == []


def test_entry_point_plugin_is_discovered_and_ordered(plugins):
    point = EntryPoint("fakeclass", "fake_resolvers:ClassroomResolver", registry.ENTRY_POINT_GROUP)
    plugins(entry_points=(point,))
    infos = resolvers.available()
    assert [info.name for info in infos] == ["fakeclass", "local"]
    assert infos[0].label == "示例课堂" and infos[0].source_hint == "粘贴示例课堂链接"
    assert resolvers.get_resolver("fake://course/42?token=SECRET").name == "fakeclass"


def test_optional_hooks_route_to_plugin(plugins):
    plugins("ClassroomResolver")
    link = "fake://course/42?token=SECRET"
    assert resolvers.public_source(link) == "fakeclass:course=42"
    # 公开描述交回同名解析器识别，换凭证后课程标识不变
    assert resolvers.course_identity(link) == resolvers.course_identity("fake://course/42?token=NEW")
    assert resolvers.course_identity("fakeclass:course=42") == "fakeclass:42"
    resolver = resolvers.get_resolver(link)
    assert [(o.id, o.default) for o in resolvers.view_options(resolver, link)] == [(1, True), (2, False)]


def test_minimal_plugin_gets_default_hooks(plugins):
    plugins("MinimalResolver")
    resolver = resolvers.get_resolver("minimal://x?sig=abc")
    assert resolver.name == "minimal"
    assert resolvers.view_options(resolver, "minimal://x") == []
    assert resolvers.course_identity("minimal://x") == "minimal://x"
    assert resolvers.public_source("https://cdn.example/a.mp4?sig=abc") == "https://cdn.example/a.mp4"


def test_invalid_plugins_are_reported_without_breaking_others(plugins):
    plugins(
        "FutureApiResolver",
        "BadNameResolver",
        "DuplicateLocalResolver",
        "not_a_resolver",
        "missing_attribute",
        "ClassroomResolver",
    )
    assert [info.name for info in resolvers.available()] == ["fakeclass", "local"]
    errors = "\n".join(resolvers.load_errors())
    assert "接口版本 99" in errors
    assert "name 须为" in errors
    assert "已被其它解析器使用" in errors
    assert "缺少依赖 somepackage" in errors
    assert "missing_attribute" in errors


def test_crashing_matcher_does_not_hide_other_resolvers(plugins, tmp_path):
    plugins("CrashingResolver")
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"")
    assert resolvers.get_resolver(str(video)).name == "local"


def test_disabled_resolver_is_skipped(plugins, monkeypatch):
    monkeypatch.setenv("COURSE2MD_DISABLED_RESOLVERS", "fakeclass")
    plugins("ClassroomResolver")
    assert [info.name for info in resolvers.available()] == ["local"]
    with pytest.raises(resolvers.ResolverError, match="没有能处理该来源的解析器"):
        resolvers.get_resolver("fake://course/1")


def test_unmatched_source_error_lists_enabled_resolvers(plugins):
    plugins("ClassroomResolver")
    with pytest.raises(resolvers.ResolverError, match="示例课堂、本地视频"):
        resolvers.get_resolver("https://unknown.example/v?token=x")


def test_pipeline_uses_plugin_primary_flag(plugins, tmp_path, monkeypatch):
    from course2md import pipeline
    from course2md.asr.formats import Segment
    from course2md.resolvers.base import MediaRef
    from course2md.stages import media as media_stage

    media = []
    for view in (1, 2):
        path = tmp_path / f"{view}.mp4"
        path.write_bytes(b"x")
        media.append(MediaRef(str(path), path.name, view=view, primary=view == 2))

    class Fake:
        def resolve(self, *args):
            return media

    class Backend:
        def __init__(self, cfg):
            self.cfg, self.progress, self.check_cancel = cfg, None, None

        def transcribe(self, samples):
            return [Segment(0, 5, "讲课内容")]

    monkeypatch.setattr(media_stage, "get_resolver", lambda source: Fake())
    monkeypatch.setattr(media_stage, "WhisperBackend", Backend)
    monkeypatch.setattr(media_stage.audio_mod, "prepare", lambda media_path, wav: (wav, 60.0))
    monkeypatch.setattr(media_stage.audio_mod, "read_wav", lambda wav: None)
    result = pipeline.run_job(pipeline.JobOptions(source="fake", out_dir=tmp_path / "out", ai=False))
    assert result.primary_media == "2"
