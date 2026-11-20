"""根据 course2md.config.schema 生成 config.example.toml、.env.example 与 docs/configuration.md。

用法：python scripts/gen_config.py [--check]
--check 只检查文件是否最新，不一致时退出码为 1（CI 与 pre-commit 使用）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from course2md.config.files import GENERATED  # noqa: E402


def main() -> int:
    check = "--check" in sys.argv[1:]
    stale = []
    for relative, render in GENERATED.items():
        path = ROOT / relative
        content = render()
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == content:
            continue
        stale.append(relative)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    if check and stale:
        print("以下文件需要重新生成（python scripts/gen_config.py）：" + "、".join(stale))
        return 1
    if stale and not check:
        print("已生成：" + "、".join(stale))
    return 0


if __name__ == "__main__":
    sys.exit(main())
