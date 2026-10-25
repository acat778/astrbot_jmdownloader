"""requirements.txt 结构测试：每行必须是可直接被 pip 解析的依赖声明。

历史问题：一次提交把换行写成了字面量 ``\\n``，导致文件第 6 行是
``\\nPillow>=9\\n``，AstrBot 依赖安装整体失败、插件因缺 jmcomic 无法导入。
"""

from __future__ import annotations

import re
from pathlib import Path

REQUIREMENTS_PATH = Path(__file__).resolve().parents[1] / "requirements.txt"

# 依赖名（可选 extras）+ 可选版本约束；覆盖本插件使用的全部写法。
_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(\[[A-Za-z0-9._,-]+\])?"
    r"(?:(?:>=|<=|==|!=|~=|>|<)[0-9A-Za-z.*+!_-]+(?:,(?:>=|<=|==|!=|~=|>|<)[0-9A-Za-z.*+!_-]+)*)?$"
)


def _lines() -> list[str]:
    text = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def test_requirements_file_has_trailing_newline_and_no_escaped_newlines():
    raw = REQUIREMENTS_PATH.read_bytes()
    assert raw.endswith(b"\n"), "requirements.txt 必须以换行结尾"
    text = raw.decode("utf-8")
    assert "\\n" not in text, "requirements.txt 不允许出现字面量 \\n"
    assert "\\r" not in text
    for line in text.splitlines():
        assert line == line.strip(), f"依赖行不应带首尾空白：{line!r}"


def test_every_requirement_line_is_parseable():
    lines = _lines()
    assert lines, "requirements.txt 不能为空"
    for line in lines:
        assert _REQUIREMENT_RE.fullmatch(line), f"非法依赖声明：{line!r}"


def test_required_dependencies_are_declared():
    names = {re.split(r"[<>=!~\[ ]", line, maxsplit=1)[0].lower() for line in _lines()}
    assert {
        "jmcomic",
        "img2pdf",
        "pypdf",
        "py7zr",
        "pyzipper",
        "pillow",
    } <= names
