"""把已有课程记录导出到 Anki，或增量同步笔记到 Obsidian。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from filelock import FileLock
from markdown_it import MarkdownIt

from .refine.conversation import load_context
from .storage import file_digest
from .util.text import safe_name


def anki_text(workspace: Path) -> str:
    context, record, _segments, _figures = load_context(workspace)
    markdown = MarkdownIt("commonmark", {"html": False})
    output = io.StringIO()
    output.write("#separator:tab\n#html:true\n#columns:Front\tBack\tTags\n")
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    for topic in record["topics"]:
        writer.writerow(
            [
                markdown.render(f"{context['title']} · {topic['title']} [{topic['timestamp']}]"),
                markdown.render(
                    topic["explanation"] + "\n\n" + "\n".join(f"- {point}" for point in topic["key_points"])
                ),
                "course2md topic",
            ]
        )
    for example in record["examples"]:
        writer.writerow(
            [
                markdown.render(
                    f"{context['title']} · {example['title']} [{example['timestamp']}]\n\n{example['question']}"
                ),
                markdown.render(
                    "\n\n".join(
                        [
                            *example["steps"],
                            example["answer"],
                            *example["pitfalls"],
                            f"待核对：{example['uncertainty']}" if example["uncertainty"] else "",
                        ]
                    )
                ),
                "course2md example",
            ]
        )
    return output.getvalue()


def job_workspace(source: Path) -> Path:
    manifest = json.loads((source / "job.json").read_text(encoding="utf-8"))
    workspace = Path(manifest.get("note_workspace") or "")
    if not workspace.is_absolute():
        workspace = source / workspace
    if not workspace.resolve().is_relative_to(source.resolve()):
        raise ValueError("课程工作区不在指定输出目录内")
    return workspace


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=target.parent, prefix=".course2md-", suffix=".part")
    try:
        with os.fdopen(handle, "wb") as output, source.open("rb") as input_file:
            for block in iter(lambda: input_file.read(1024 * 1024), b""):
                output.write(block)
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def sync_obsidian(source: Path, vault: Path, folder: str = "course2md") -> dict:
    """只同步两版笔记与它们引用的本地图片；保留用户修改并返回冲突。"""
    source, vault = source.resolve(), vault.resolve()

    namespace = f"{safe_name(source.name)}-{hashlib.sha256(str(source).encode()).hexdigest()[:8]}"
    target = (vault / folder / namespace).resolve()
    if not target.is_relative_to(vault) or target == vault or target.is_relative_to(source):
        raise ValueError("同步目录必须位于 vault 内且不能位于课程输出目录中")
    notes = sorted((source / "notes").glob("*.notes.md")) + sorted((source / "notes").glob("*.brief.md"))
    if not notes:
        raise ValueError("指定课程没有可同步的笔记")
    files = set(notes)
    markdown = MarkdownIt("commonmark")
    for note in notes:
        if not note.resolve().is_relative_to(source):
            raise ValueError("笔记路径超出课程目录")
        for block in markdown.parse(note.read_text(encoding="utf-8")):
            for token in block.children or []:
                if token.type != "image":
                    continue
                address = urlparse(str(token.attrGet("src") or ""))
                path = (note.parent / unquote(address.path)).resolve()
                if (
                    not address.scheme
                    and not address.netloc
                    and path.is_relative_to(source)
                    and path.is_file()
                    and path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif")
                ):
                    files.add(path)
    target.mkdir(parents=True, exist_ok=True)
    with FileLock(str(target / ".course2md-sync.lock")):
        return _sync_files(source, target, files)


def _sync_files(source: Path, target: Path, files: set[Path]) -> dict:
    manifest = target / ".course2md-sync.json"
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    conflicts, updated = [], []
    for file in sorted(files):
        if not file.resolve().is_relative_to(source):
            raise ValueError("笔记路径超出课程目录")
        relative = file.relative_to(source).as_posix()
        dest = (target / relative).resolve()
        if not dest.is_relative_to(target):
            raise ValueError("同步路径超出目标目录")
        digest = file_digest(file)
        if dest.is_file():
            current = file_digest(dest)
            if current == digest:
                previous[relative] = digest
                continue
            if current != previous.get(relative):
                conflicts.append(relative)
                continue
        _atomic_copy(file, dest)
        previous[relative] = digest
        updated.append(relative)
    temporary = manifest.with_suffix(".json.part")
    temporary.write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest)
    return {"path": str(target), "updated": updated, "conflicts": conflicts}
