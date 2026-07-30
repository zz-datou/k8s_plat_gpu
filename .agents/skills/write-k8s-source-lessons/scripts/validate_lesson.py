#!/usr/bin/env python3
"""Validate the mechanical contract of Kubernetes source-learning Markdown.

This script intentionally does not decide whether a source excerpt is truthful.
That still requires comparison with the pinned Kubernetes checkout.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import unquote, urlsplit


FENCE_OPEN_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+\S")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
INLINE_CODE_RE = re.compile(r"(`+)(?:(?!\1).)*\1")
INLINE_LINK_RE = re.compile(r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
REFERENCE_LINK_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(?P<target><[^>]+>|\S+)")
UNFINISHED_RE = re.compile(r"\b(?:TODO|FIXME|TBD)\b|待补(?:充)?|待完善|补充中", re.IGNORECASE)
SUSPECT_MOJIBAKE_RE = re.compile(r"(?:Ã.|Â.|â€|ðŸ){2,}")

GO_LANGS = {"go", "golang"}
SHELL_LANGS = {"bash", "sh", "shell", "console", "powershell", "pwsh"}
EXTERNAL_SCHEMES = {"http", "https", "mailto", "data", "ftp"}

SIGNALS = {
    "DESIGN": re.compile(r"设计|为什么|不变量|边界|取舍|契约"),
    "SCENARIO": re.compile(r"生产|事故|值班|场景|白板|案例|Java Pod|game-api|rollout", re.IGNORECASE),
    "SOURCE": re.compile(r"源码|调用链|固定提交|文件[：:]|commit", re.IGNORECASE),
    "PLAIN": re.compile(r"大白话|白话|人话|直觉"),
    "GO": re.compile(r"Go 语法|顺手学 Go|语法复习|语法索引", re.IGNORECASE),
    "VERIFY": re.compile(r"验证|证据|命令|观察|反事实|验收"),
    "DEPTH": re.compile(r"深读|读深|源码深度|第二遍|二遍"),
    "DEFER": re.compile(r"略过|跳过|首遍|第一遍|暂不展开|只认用途|不要求|后续再"),
    "GPU": re.compile(r"GPU|CUDA|NVIDIA|device plugin|显卡", re.IGNORECASE),
}


@dataclass(frozen=True)
class Diagnostic:
    path: str
    line: int
    column: int
    severity: str
    code: str
    message: str


@dataclass
class FencedBlock:
    start_line: int
    end_line: int
    marker: str
    marker_len: int
    info: str
    language: str
    content: list[tuple[int, str]]


def diagnostic(
    path: Path,
    line: int,
    column: int,
    severity: str,
    code: str,
    message: str,
) -> Diagnostic:
    return Diagnostic(str(path), line, column, severity, code, message)


def normalize_language(info: str) -> str:
    if not info.strip():
        return ""
    token = info.strip().split()[0].strip("{}").lstrip(".").lower()
    return token


def parse_fences(path: Path, lines: Sequence[str]) -> tuple[list[FencedBlock], list[Diagnostic], set[int]]:
    blocks: list[FencedBlock] = []
    findings: list[Diagnostic] = []
    fenced_lines: set[int] = set()
    current: FencedBlock | None = None

    for number, line in enumerate(lines, start=1):
        if current is None:
            match = FENCE_OPEN_RE.match(line)
            if not match:
                continue
            marker_text = match.group("fence")
            marker = marker_text[0]
            info = match.group("info").strip()
            if marker == "`" and "`" in info:
                findings.append(
                    diagnostic(path, number, line.index("`") + 1, "ERROR", "FENCE_INVALID_INFO", "backtick fence info contains a backtick")
                )
            current = FencedBlock(
                start_line=number,
                end_line=0,
                marker=marker,
                marker_len=len(marker_text),
                info=info,
                language=normalize_language(info),
                content=[],
            )
            fenced_lines.add(number)
            continue

        fenced_lines.add(number)
        close_re = re.compile(rf"^ {{0,3}}{re.escape(current.marker)}{{{current.marker_len},}}\s*$")
        if close_re.match(line):
            current.end_line = number
            blocks.append(current)
            current = None
        else:
            current.content.append((number, line))

    if current is not None:
        findings.append(
            diagnostic(path, current.start_line, 1, "ERROR", "FENCE_UNCLOSED", "fenced code block is not closed with a compatible marker")
        )
        current.end_line = len(lines)
        blocks.append(current)

    return blocks, findings, fenced_lines


def mask_inline_code(line: str) -> str:
    return INLINE_CODE_RE.sub(lambda match: " " * len(match.group(0)), line)


def mask_go_literals(line: str, state: str | None) -> tuple[str, str | None]:
    """Mask Go string/rune/raw-string contents while preserving columns."""
    chars = list(line)
    index = 0
    active = state
    escaped = False

    while index < len(chars):
        char = chars[index]
        if active is None:
            if char == '"':
                active = '"'
                chars[index] = " "
            elif char == "'":
                active = "'"
                chars[index] = " "
            elif char == "`":
                active = "`"
                chars[index] = " "
            index += 1
            continue

        chars[index] = " "
        if active == "`":
            if char == "`":
                active = None
            index += 1
            continue

        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == active:
            active = None
        index += 1

    return "".join(chars), active


def classify_go_ellipsis(masked_line: str, offset: int) -> str | None:
    comment_at = masked_line.find("//")
    if comment_at >= 0 and offset >= comment_at:
        comment = masked_line[comment_at:]
        if re.fullmatch(r"//\s*\.\.\.\s*", comment):
            return "ERROR"
        return None

    immediate_before = masked_line[offset - 1] if offset > 0 else ""
    immediate_after = masked_line[offset + 3] if offset + 3 < len(masked_line) else ""
    before = masked_line[:offset].rstrip()
    after = masked_line[offset + 3 :].lstrip()

    # Valid array length inference: [...]T.
    if immediate_before == "[" and immediate_after == "]":
        return None
    # Valid variadic declaration: ...T, ...*T, ...[]T.
    if immediate_after and (immediate_after.isidentifier() or immediate_after in "_*["):
        return None
    # Valid variadic expansion: expression... followed by comma, right paren, or end.
    if immediate_before and (immediate_before.isalnum() or immediate_before in "_])") and (not after or after[0] in ",)"):
        return None

    compact_before = before[-1:] if before else ""
    compact_after = after[:1] if after else ""
    if (compact_before in "({" and compact_after in ")}") or (not before and not after):
        return "ERROR"
    if re.search(r"\breturn\s*$", before) or re.fullmatch(r"\.\.\.", masked_line.strip()):
        return "ERROR"
    return "WARNING"


def is_business_statement(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("//") or stripped.startswith("/*") or re.match(r"^\*\s", stripped):
        return False
    if re.fullmatch(r"[{}()[\],;]+", stripped):
        return False
    return True


def go_block_checks(path: Path, block: FencedBlock) -> list[Diagnostic]:
    findings: list[Diagnostic] = []
    nonblank = [(number, line) for number, line in block.content if line.strip()]
    if not nonblank:
        return [diagnostic(path, block.start_line, 1, "ERROR", "GO_EMPTY", "Go fenced block is empty")]
    if all(
        line.strip().startswith(("//", "/*")) or re.match(r"^\*\s", line.strip())
        for _, line in nonblank
    ):
        findings.append(diagnostic(path, block.start_line, 1, "WARNING", "GO_COMMENT_ONLY", "Go fenced block contains comments but no code"))

    string_state: str | None = None
    statement_count = 0
    annotated_count = 0
    previous_chinese_comment = False

    for number, line in block.content:
        masked, string_state = mask_go_literals(line, string_state)
        for match in re.finditer(r"\.\.\.", masked):
            classification = classify_go_ellipsis(masked, match.start())
            if classification == "ERROR":
                findings.append(
                    diagnostic(path, number, match.start() + 1, "ERROR", "GO_PLACEHOLDER", "Go block contains a placeholder ellipsis instead of explicit source")
                )
            elif classification == "WARNING":
                findings.append(
                    diagnostic(path, number, match.start() + 1, "WARNING", "GO_ELLIPSIS_AMBIGUOUS", "ellipsis may be a placeholder; verify that it is real Go syntax")
                )

        stripped = line.strip()
        if UNFINISHED_RE.search(stripped):
            findings.append(
                diagnostic(path, number, 1, "WARNING", "TODO_IN_SOURCE", "Go excerpt contains an upstream TODO/FIXME/TBD marker; verify it is intentional")
            )

        if stripped.startswith("//"):
            previous_chinese_comment = bool(CJK_RE.search(stripped))
            continue
        if not is_business_statement(line):
            if not stripped:
                previous_chinese_comment = False
            continue

        statement_count += 1
        inline_comment = line.split("//", 1)[1] if "//" in line else ""
        if previous_chinese_comment or CJK_RE.search(inline_comment):
            annotated_count += 1
        previous_chinese_comment = False

    if statement_count >= 5 and annotated_count / statement_count < 0.35:
        findings.append(
            diagnostic(
                path,
                block.start_line,
                1,
                "WARNING",
                "GO_LOW_CHINESE_COMMENT_COVERAGE",
                f"only {annotated_count}/{statement_count} business statements have adjacent Chinese teaching comments",
            )
        )
    return findings


def visible_lines_before_references(lines: Sequence[str], fenced_lines: set[int]) -> list[tuple[int, str]]:
    visible: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        if number in fenced_lines:
            continue
        if HEADING_RE.match(line) and re.search(r"参考资料|References", line, re.IGNORECASE):
            break
        visible.append((number, line))
    return visible


def signal_score(lines: Iterable[tuple[int, str]], pattern: re.Pattern[str]) -> int:
    score = 0
    for _, line in lines:
        clean = mask_inline_code(line)
        clean = re.sub(r"\]\([^)]+\)", "]", clean)
        if pattern.search(clean):
            score += 2 if HEADING_RE.match(clean) or "**" in clean else 1
    return score


def check_links(path: Path, lines: Sequence[str], fenced_lines: set[int], root: Path) -> list[Diagnostic]:
    findings: list[Diagnostic] = []
    root = root.resolve()

    for number, line in enumerate(lines, start=1):
        if number in fenced_lines:
            continue
        clean = mask_inline_code(line)
        matches = list(INLINE_LINK_RE.finditer(clean))
        reference_match = REFERENCE_LINK_RE.match(clean)
        if reference_match:
            matches.append(reference_match)

        for match in matches:
            raw = match.group("target").strip("<>")
            if not raw or raw.startswith("#") or raw.startswith("//"):
                continue
            parsed = urlsplit(raw)
            if parsed.scheme.lower() in EXTERNAL_SCHEMES:
                continue
            if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("/", "\\")):
                continue
            if "\\" in raw:
                findings.append(
                    diagnostic(path, number, match.start("target") + 1, "WARNING", "LINK_NONPORTABLE", "relative Markdown link uses backslashes")
                )

            relative = unquote(parsed.path.replace("\\", "/"))
            if not relative:
                continue
            target = (path.parent / relative).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                findings.append(
                    diagnostic(path, number, match.start("target") + 1, "ERROR", "LINK_ESCAPES_ROOT", f"relative link escapes validation root: {raw}")
                )
                continue
            if not target.exists():
                findings.append(
                    diagnostic(path, number, match.start("target") + 1, "ERROR", "LINK_MISSING", f"relative link target does not exist: {raw}")
                )
    return findings


def check_go_sections(path: Path, lines: Sequence[str], blocks: Sequence[FencedBlock], fenced_lines: set[int]) -> list[Diagnostic]:
    findings: list[Diagnostic] = []
    headings = [
        (number, len(line) - len(line.lstrip("#")), line)
        for number, line in enumerate(lines, start=1)
        if number not in fenced_lines and HEADING_RE.match(line)
    ]
    warned: set[int] = set()
    go_blocks = [block for block in blocks if block.language in GO_LANGS]

    for block in go_blocks:
        prior = [item for item in headings if item[0] < block.start_line]
        heading, level, title = prior[-1] if prior else (1, 1, lines[0] if lines else "")
        if heading in warned:
            continue
        next_heading = min((number for number, _, _ in headings if number > heading), default=len(lines) + 1)
        section = "\n".join(lines[heading - 1 : next_heading - 1])
        syntax_section = bool(re.search(r"Go 语法|语法复习|语法索引", title, re.IGNORECASE))
        if not syntax_section and "大白话总结" not in section:
            findings.append(
                diagnostic(path, heading, 1, "WARNING", "LESSON_GO_SECTION_NO_PLAIN_SUMMARY", "section with Go source has no 大白话总结")
            )

        parent_candidates = [item for item in prior if item[1] <= 2]
        parent_heading, parent_level, _ = parent_candidates[-1] if parent_candidates else (heading, level, title)
        parent_end = min(
            (number for number, candidate_level, _ in headings if number > parent_heading and candidate_level <= parent_level),
            default=len(lines) + 1,
        )
        parent_section = "\n".join(lines[parent_heading - 1 : parent_end - 1])
        if not syntax_section and not re.search(r"顺手学 Go|Go 语法", parent_section, re.IGNORECASE):
            findings.append(
                diagnostic(path, heading, 1, "WARNING", "LESSON_GO_SECTION_NO_SYNTAX_NOTE", "section with Go source has no local Go syntax note")
            )
        warned.add(heading)
    return findings


def validate_content(
    path: Path,
    text: str,
    root: Path,
    require_java: bool,
    require_gpu: bool,
) -> list[Diagnostic]:
    findings: list[Diagnostic] = []
    lines = text.splitlines()

    if "\x00" in text or "\ufffd" in text or "锟斤拷" in text:
        findings.append(diagnostic(path, 1, 1, "ERROR", "ENC_CORRUPT", "file contains a NUL or high-confidence corrupted-text marker"))
    if SUSPECT_MOJIBAKE_RE.search(text):
        findings.append(diagnostic(path, 1, 1, "WARNING", "ENC_SUSPECT_MOJIBAKE", "file contains text that resembles mojibake"))

    blocks, fence_findings, fenced_lines = parse_fences(path, lines)
    findings.extend(fence_findings)

    if not any(number not in fenced_lines and re.match(r"^#\s+\S", line) for number, line in enumerate(lines, start=1)):
        findings.append(diagnostic(path, 1, 1, "ERROR", "LESSON_H1_MISSING", "lesson has no level-one title"))

    for block in blocks:
        if block.content and not block.language:
            findings.append(diagnostic(path, block.start_line, 1, "WARNING", "FENCE_NO_LANGUAGE", "non-empty fenced block has no language label"))

    go_blocks = [block for block in blocks if block.language in GO_LANGS]
    if not go_blocks:
        findings.append(diagnostic(path, 1, 1, "ERROR", "GO_MISSING", "Kubernetes source lesson contains no Go fenced block"))
    for block in go_blocks:
        findings.extend(go_block_checks(path, block))

    for number, line in enumerate(lines, start=1):
        if number in fenced_lines:
            continue
        clean = mask_inline_code(line)
        match = UNFINISHED_RE.search(clean)
        if match:
            findings.append(
                diagnostic(path, number, match.start() + 1, "ERROR", "TODO_UNFINISHED", "visible lesson text contains an unfinished marker")
            )

    visible = visible_lines_before_references(lines, fenced_lines)
    for name, pattern in SIGNALS.items():
        score = signal_score(visible, pattern)
        if score >= 2:
            continue
        severity = "ERROR" if name == "GPU" and require_gpu else "WARNING"
        findings.append(
            diagnostic(path, 1, 1, severity, f"LESSON_SIGNAL_{name}_MISSING", f"lesson lacks a strong {name.lower()} teaching signal")
        )

    if require_java and not re.search(r"Java|Spring|JVM|game-api", "\n".join(line for _, line in visible), re.IGNORECASE):
        findings.append(diagnostic(path, 1, 1, "ERROR", "LESSON_JAVA_REQUIRED", "platform lesson does not contain a Java-specific scenario"))
    if require_gpu and not SIGNALS["GPU"].search("\n".join(line for _, line in visible)):
        # Avoid duplicating the generic GPU signal finding.
        pass

    if go_blocks and not re.search(r"\b[0-9a-fA-F]{40}\b", text):
        findings.append(diagnostic(path, 1, 1, "WARNING", "SOURCE_SHA_MISSING", "source lesson does not record a full 40-character commit SHA"))
    if go_blocks and not re.search(r"阅读约定|教学注释版|中文.{0,8}注释", text):
        findings.append(diagnostic(path, 1, 1, "WARNING", "SOURCE_READING_CONVENTION_MISSING", "lesson does not state how teaching comments and excerpts relate to upstream source"))

    shell_blocks = [block for block in blocks if block.language in SHELL_LANGS]
    design_lines = [number for number, line in visible if SIGNALS["DESIGN"].search(mask_inline_code(line))]
    if shell_blocks and design_lines and min(block.start_line for block in shell_blocks) < min(design_lines):
        findings.append(diagnostic(path, shell_blocks[0].start_line, 1, "WARNING", "COMMANDS_BEFORE_DESIGN", "shell commands appear before the lesson establishes its design problem"))

    findings.extend(check_go_sections(path, lines, blocks, fenced_lines))
    findings.extend(check_links(path, lines, fenced_lines, root))
    return findings


def read_and_validate(path: Path, root: Path, require_java: bool, require_gpu: bool) -> list[Diagnostic]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        return [diagnostic(path, error.start + 1, 1, "ERROR", "ENC_INVALID_UTF8", str(error))]
    except OSError as error:
        return [diagnostic(path, 1, 1, "ERROR", "INPUT_READ_FAILED", str(error))]
    return validate_content(path.resolve(), text, root, require_java, require_gpu)


def collect_markdown(paths: Sequence[str]) -> list[Path]:
    files: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.update(item.resolve() for item in path.rglob("*.md") if item.is_file())
        elif path.is_file():
            files.add(path.resolve())
        else:
            raise FileNotFoundError(raw)
    return sorted(files, key=lambda item: str(item).lower())


def run_self_test() -> int:
    sha = "a" * 40
    good = f"""# 第 01 课：为什么宁愿等待也不能越过边界

## 先看生产 Java 场景与白板设计
这是 Java Spring 生产事故案例。先建立设计契约、不变量和取舍，再进入源码。

## 当前源码基线与阅读约定
commit: {sha}。中文注释是教学新增。

```go
// 接收一个可变数量的原因，并保持真实 Go 语法。
func explain(reasons ...string) {{
    // 把 slice 展开成参数；这不是省略源码。
    report(reasons...)
}}
```

**大白话总结：** 这段展示输入和结果。

**顺手学 Go：** `...string` 是 variadic 参数。

## 证据与验证
观察变量后再运行只读命令，并做反事实验收。

## GPU 短映射
GPU 复用同一控制逻辑。

## 深读与首遍略过
第二遍再读旁支。
"""
    good_findings = validate_content(Path("good.md"), good, Path.cwd(), True, True)
    good_errors = [item for item in good_findings if item.severity == "ERROR"]
    if good_errors:
        print("self-test good fixture unexpectedly failed", file=sys.stderr)
        for item in good_errors:
            print(item, file=sys.stderr)
        return 1

    bad = """# bad
TODO: finish
```go
...
"""
    bad_codes = {item.code for item in validate_content(Path("bad.md"), bad, Path.cwd(), False, False)}
    expected = {"TODO_UNFINISHED", "GO_PLACEHOLDER", "FENCE_UNCLOSED"}
    if not expected.issubset(bad_codes):
        print(f"self-test bad fixture missed: {sorted(expected - bad_codes)}", file=sys.stderr)
        return 1

    print("SELF-TEST OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Markdown files or directories")
    parser.add_argument("--root", default=".", help="root allowed for relative Markdown links")
    parser.add_argument("--require-java", action="store_true", help="treat a missing Java-specific scenario as an error")
    parser.add_argument("--require-gpu", action="store_true", help="treat a missing GPU mapping as an error")
    parser.add_argument("--strict", action="store_true", help="return failure when warnings exist")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--self-test", action="store_true", help="run built-in regression fixtures")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_test()
    if not args.paths:
        parser.error("provide at least one Markdown file or directory")

    try:
        files = collect_markdown(args.paths)
    except FileNotFoundError as error:
        parser.error(f"input path does not exist: {error}")
    if not files:
        parser.error("no Markdown files found")

    root = Path(args.root).resolve()
    findings: list[Diagnostic] = []
    for path in files:
        findings.extend(read_and_validate(path, root, args.require_java, args.require_gpu))

    findings.sort(key=lambda item: (item.path.lower(), item.line, item.column, item.severity, item.code))
    errors = sum(item.severity == "ERROR" for item in findings)
    warnings = sum(item.severity == "WARNING" for item in findings)

    if args.format == "json":
        print(json.dumps({"files": len(files), "errors": errors, "warnings": warnings, "diagnostics": [asdict(item) for item in findings]}, ensure_ascii=False, indent=2))
    else:
        for item in findings:
            print(f"{item.path}:{item.line}:{item.column}: {item.severity} {item.code}: {item.message}")
        print(f"SUMMARY files={len(files)} errors={errors} warnings={warnings}")

    if errors or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
