"""Conservative frontend contract checks before spending a Formatter call."""

import re
from html import unescape
from urllib.parse import unquote

from domain.errors import HarnessError
from domain.schemas import (
    FileBlock,
    HtmlBlock,
    LatexBlock,
    MarkdownBlock,
    Presentation,
    QuestionBlock,
    SvgBlock,
)
from domain.validation import validate_html, validate_latex, validate_svg

MAX_PRESENTATION_BYTES = 8 * 1024**2


def unwrap_document(text: str, languages=frozenset({"markdown", "md", "json"})) -> str:
    """Remove one whole-document wrapper, never a real code example."""
    lines = text.strip().splitlines()
    if len(lines) < 3:
        return text
    start = re.fullmatch(r"\s{0,3}(`{3,}|~{3,})([A-Za-z]*)\s*", lines[0])
    if not start or start[2].lower() not in languages:
        return text
    marker = start[1]
    closing = re.compile(r"\s{0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*")
    if not closing.fullmatch(lines[-1]) or any(closing.fullmatch(line) for line in lines[1:-1]):
        return text
    return "\n".join(lines[1:-1])


def validate_markdown(text: str):
    fence, outside = None, []
    for line in text.splitlines(keepends=True):
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if match:
            marker, rest = match.groups()
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip():
                fence = None
            outside.append(re.sub(r"[^\r\n]", " ", line))
        else:
            outside.append(line if fence is None else re.sub(r"[^\r\n]", " ", line))
    if fence:
        raise ValueError("Unclosed code fence")
    source = re.sub(r"(`+)[^`\n]*\1", lambda m: " " * len(m[0]), "".join(outside))
    # A TeX row break such as \\[4pt] is not a Markdown display delimiter.
    delimiters = re.compile(r"(?<!\\)(?:\\[()[\]]|\${1,2})")
    opening, formulas = None, []
    pairs = {r"\(": r"\)", r"\[": r"\]", "$": "$", "$$": "$$"}
    position, prose, end = 0, [], 0
    for match in delimiters.finditer(source):
        token = match.group()
        if opening is None:
            if token not in pairs:
                raise ValueError("Unexpected math closing delimiter")
            prose.append(source[end : match.start()])
            opening, position = token, match.end()
        elif token == pairs[opening]:
            formulas.append((position, match.start(), source[position : match.start()]))
            end = match.end()
            opening = None
        else:
            raise ValueError("Mixed math delimiters")
    if opening:
        raise ValueError("Unclosed formula")
    repairs = []
    for start, stop, formula in formulas:
        normalized = normalize_latex(formula)
        validate_latex(normalized)
        if normalized != formula:
            repairs.append((start, stop, normalized))
    # Formula inequalities are TeX, not HTML tags; validate their TeX above.
    prose.append(source[end:])
    outside_math = "".join(prose)
    if re.search(r'\{\s*"type"\s*:\s*"(?:markdown|latex|svg|html|question|branch|localfile)"', outside_math):
        raise ValueError("Structured display JSON cannot be embedded in Markdown prose")
    if re.search(r"<(?:/?[A-Za-z][^<>]*|!--.*?)>|!\[", outside_math):
        raise ValueError("Use structured blocks for HTML and images")
    # Check all content for executable schemes, including TeX text arguments.
    decoded = unquote(unescape(source))
    if re.search(r"(?:javascript|vbscript|data|file)\s*:", decoded, re.I):
        raise ValueError("Unsafe Markdown URL")
    for start, stop, normalized in reversed(repairs):
        text = text[:start] + normalized + text[stop:]
    return text


def normalize_latex(source: str) -> str:
    """Translate an annotated equals sign to base/AMS without changing its labels.

    This is typography only. Unknown commands, malformed arguments and optional
    lower labels continue through strict validation; no macros are loaded.
    """
    edits = []
    for match in re.finditer(r"\\xlongequal\b\s*", source):
        if edits and match.start() < edits[-1][1]:
            # Avoid overlapping replacements for unsupported nested annotations.
            # Remaining commands still require Formatter/strict validation.
            continue
        start = match.end()
        if start >= len(source) or source[start] != "{":
            continue
        depth, end = 0, None
        for token in re.finditer(r"(?<!\\)[{}]", source[start:]):
            depth += 1 if token[0] == "{" else -1
            if depth == 0:
                end = start + token.end()
                break
        if end is not None:
            edits.append((match.start(), end, r"\overset" + source[start:end] + "{=}"))
    for start, end, replacement in reversed(edits):
        source = source[:start] + replacement + source[end:]
    return source


def validate_presentation(presentation: Presentation, artifact_ids, question=None, *, append_question=False):
    if len(presentation.model_dump_json().encode()) > MAX_PRESENTATION_BYTES:
        raise ValueError("Presentation is too large")
    questions = []
    for block in presentation.leaves():
        if isinstance(block, MarkdownBlock):
            block.text = unwrap_document(block.text, {"markdown", "md"})
            block.text = validate_markdown(block.text)
        elif isinstance(block, HtmlBlock):
            validate_html(block.source)
        elif isinstance(block, SvgBlock):
            validate_svg(block.source)
        elif isinstance(block, LatexBlock):
            block.source = normalize_latex(block.source)
            validate_latex(block.source)
        elif isinstance(block, FileBlock) and block.artifact_id not in artifact_ids:
            raise ValueError("File does not belong to this session")
        elif isinstance(block, QuestionBlock):
            questions.append(block.model_dump())
    if question and not questions and append_question:
        if len(presentation.blocks) >= 49:
            raise ValueError("No room for server question and output log")
        presentation.blocks.append(QuestionBlock.model_validate(question))
        questions = [question]
    if questions != ([question] if question else []):
        raise ValueError("Changed or invented a question")
    if len(presentation.blocks) > 49:
        raise ValueError("Reserve one block for the output log")
    return presentation


def parse_expert(text, artifact_ids, question=None):
    source = unwrap_document(text).strip()
    if re.match(r'^(?:\{\s*"|\[\s*\{)', source) or source == "[]":
        result = Presentation.model_validate_json(source)
    else:
        result = Presentation(blocks=[MarkdownBlock(type="markdown", text=source)])
    return validate_presentation(result, artifact_ids, question, append_question=True)


def literal_fallback(text, question=None):
    # Render failed formatting as a literal code block, preserving the complete
    # answer without executing links/HTML or attempting malformed TeX.
    # Pick the shorter safe alphabet, avoiding expansion for long fence runs.
    fences = []
    for character in (chr(96), "~"):
        longest = max((len(m[0]) for m in re.finditer(re.escape(character) + "+", text)), default=0)
        fences.append(character * max(3, longest + 1))
    fence = min(fences, key=len)
    blocks = [{"type": "markdown", "text": f"{fence}text\n{text}\n{fence}"}]
    if question:
        blocks.append(question)
    return Presentation.model_validate({"blocks": blocks})


def recover_presentation(text, artifact_ids, question=None):
    """Keep validated rich leaves in place when a remote Formatter fails.

    Question and file ownership failures reject the entire document. Only
    display syntax can be downgraded locally, with each invalid leaf quoted.
    """
    try:
        result = Presentation.model_validate_json(unwrap_document(text).strip())
        questions = []
        for block in result.leaves():
            if isinstance(block, FileBlock) and block.artifact_id not in artifact_ids:
                raise ValueError("File does not belong to this session")
            if isinstance(block, QuestionBlock):
                questions.append(block.model_dump())
        if questions not in ([], [question] if question else []):
            raise ValueError("Changed or invented a question")
        # Convert only failed display leaves; retain branch/tab and file order.
        for block_index, block in enumerate(result.blocks):
            groups = [tab.blocks for tab in block.tabs] if block.type == "branch" else [[block]]
            for group in groups:
                for index, leaf in enumerate(group):
                    if isinstance(leaf, QuestionBlock):
                        continue
                    try:
                        validate_presentation(Presentation(blocks=[leaf]), artifact_ids)
                    except (ValueError, HarnessError):
                        group[index] = literal_fallback(
                            getattr(leaf, "text", getattr(leaf, "source", leaf.model_dump_json()))
                        ).blocks[0]
                if block.type != "branch":
                    result.blocks[block_index] = group[0]
        return validate_presentation(result, artifact_ids, question, append_question=True)
    except (ValueError, HarnessError):
        return literal_fallback(text, question)
