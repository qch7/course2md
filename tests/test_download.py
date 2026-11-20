"""下载完整响应、续传响应和损坏响应的文件状态。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from course2md.resolvers.base import MediaRef, ResolverError, download


class Response:
    """提供流式下载所需的响应字段。"""

    def __init__(self, status, headers, chunks):
        self.status_code = status
        self.headers = headers
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def iter_content(self, size):
        yield from self.chunks


@pytest.mark.parametrize("headers", [{"Content-Length": "7"}, {}])
def test_resume_restarts_when_server_returns_full_response(tmp_path, headers):
    target = tmp_path / "video.mp4"
    part = target.with_suffix(".mp4.part")
    part.write_bytes(b"ABC")
    session = Mock()
    session.get.return_value = Response(200, headers, [b"ABCD", b"EFG"])
    download(MediaRef("https://example.invalid/video", "video.mp4"), target, session=session)
    assert session.get.call_args.kwargs["headers"]["Range"] == "bytes=3-"
    assert target.read_bytes() == b"ABCDEFG"
    assert not part.exists()


def test_resume_appends_matching_partial_response(tmp_path):
    target = tmp_path / "video.mp4"
    part = target.with_suffix(".mp4.part")
    part.write_bytes(b"ABC")
    session = Mock()
    session.get.return_value = Response(206, {"Content-Range": "bytes 3-6/7", "Content-Length": "4"}, [b"DEFG"])
    download(MediaRef("https://example.invalid/video", "video.mp4"), target, session=session)
    assert target.read_bytes() == b"ABCDEFG"
    assert not part.exists()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Content-Range": "bytes 0-3/7"},
        {"Content-Range": "bytes 3-7/7"},
        {"Content-Range": "bytes 3-6/7", "Content-Length": "5"},
        {"Content-Length": "invalid"},
    ],
)
def test_resume_rejects_invalid_range_without_changing_existing_files(tmp_path, headers):
    target = tmp_path / "video.mp4"
    target.write_bytes(b"previous-complete-file")
    part = target.with_suffix(".mp4.part")
    part.write_bytes(b"ABC")
    session = Mock()
    session.get.return_value = Response(206, headers, [b"DEFG"])
    with pytest.raises(ResolverError):
        download(MediaRef("https://example.invalid/video", "video.mp4"), target, session=session)
    assert part.read_bytes() == b"ABC"
    assert target.read_bytes() == b"previous-complete-file"


def test_short_response_stays_partial_until_successful_resume(tmp_path):
    target = tmp_path / "nested" / "video.mp4"
    session = Mock()
    session.get.return_value = Response(200, {"Content-Length": "7"}, [b"ABC"])
    media = MediaRef("https://example.invalid/video", "video.mp4")
    with pytest.raises(ResolverError, match="字节数"):
        download(media, target, session=session)
    assert not target.exists()
    assert target.with_suffix(".mp4.part").read_bytes() == b"ABC"
    session.get.return_value = Response(206, {"Content-Range": "bytes 3-6/7"}, [b"DEFG"])
    download(media, target, session=session)
    assert target.read_bytes() == b"ABCDEFG"


def test_oversized_resume_chunk_preserves_original_partial_file(tmp_path):
    target = tmp_path / "video.mp4"
    part = target.with_suffix(".mp4.part")
    part.write_bytes(b"ABC")
    session = Mock()
    session.get.return_value = Response(206, {"Content-Range": "bytes 3-6/7"}, [b"DEFGH"])
    with pytest.raises(ResolverError, match="超出声明"):
        download(MediaRef("https://example.invalid/video", "video.mp4"), target, session=session)
    assert part.read_bytes() == b"ABC"
    assert not target.exists()
