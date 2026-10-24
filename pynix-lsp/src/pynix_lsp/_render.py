"""Generic hover rendering for a resolved Nix value, dialect-extensible."""

from __future__ import annotations

from pynix._value_render import format_json
from pynix_lsp._dialect import Dialect
from pynix_lsp._value import EVAL_FAILURES, LazyValue


async def render_value(value: LazyValue, dialects: list[Dialect]) -> str:  # noqa: C901 -- tracked complexity/arg-count debt, see TODO.md
    """Render *value* as hover Markdown, letting *dialects* supply extra sections.

    The first dialect whose ``extra_hover_sections`` returns non-None wins,
    replacing the generic JSON dump (e.g. a NixOS ``mkOption`` declaration's
    description/type/default, rendered by ``ModuleSystemDialect`` instead of
    a useless ``__toString``'d location string).
    """
    nix_type = await value.type_name()
    sections = [f"```\n{nix_type}\n```"]
    extra: list[str] | None = None
    if nix_type == "attrs":
        for dialect in dialects:
            extra = await dialect.extra_hover_sections(value)
            if extra is not None:
                break
    if extra is not None:
        sections.extend(extra)
    elif nix_type != "function":
        try:
            json_value = await value.to_python()
        except EVAL_FAILURES:
            pass
        else:
            sections.append(f"```json\n{format_json(json_value)}\n```")
    try:
        edit_path, edit_line = await value.edit_location()
    except EVAL_FAILURES:
        pass
    else:
        if edit_path:
            sections.append(f"defined at `{edit_path}:{edit_line}`")
    return "\n\n".join(sections)
