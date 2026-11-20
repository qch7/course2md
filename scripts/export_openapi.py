"""导出 Hub 的 OpenAPI 文档到 docs/api/openapi.json（前端据此生成 TypeScript 客户端）。

用法：python scripts/export_openapi.py [--check]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "api" / "openapi.json"
sys.path.insert(0, str(ROOT / "src"))


def render() -> str:
    from course2md.hub.app import create_app

    schema = create_app(None).openapi()
    schema["info"]["version"] = "v1"  # 文档描述接口版本，不随包版本变化
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    content = render()
    current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else None
    if "--check" in sys.argv[1:]:
        if current != content:
            print("docs/api/openapi.json 已过期，运行 python scripts/export_openapi.py")
            return 1
        return 0
    if current != content:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(content, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
