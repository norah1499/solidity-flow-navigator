"""Pygments wrapper used by the Layer 3 serializer.

Source highlighting runs server-side at template-render time, not at Flow
build time. Layer 2's ``FunctionNode.source_code`` is plain text; Layer 3
adds a parallel ``source_html`` field for the template, leaving the IR
otherwise untouched.

The HTML formatter is configured with ``cssclass="src"`` and ``nowrap=True``:
the template provides the wrapping ``<pre class="src">`` element, and the
formatter emits only the inner ``<span>`` spans. This keeps the wrapping
markup under template control (line wrapping, scroll behavior) while the
Pygments output stays minimal.

Pygments stylesheet generation
------------------------------
``write_pygments_css(static_dir)`` regenerates ``css/pygments.css`` under
the given static directory. It is called once at server startup. The base
palette comes from Pygments' built-in ``default`` style; finer token-color
calibration lives as overrides in ``main.css`` so the visual language can
be tuned without touching Python.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers.solidity import SolidityLexer

# Module-level singletons. The lexer and formatter are stateless after
# construction; reusing them avoids per-call setup cost on a 961-flow repo.
_LEXER = SolidityLexer()
_FORMATTER = HtmlFormatter(cssclass="src", nowrap=True)

# Built-in Pygments style used as the colour base. ``main.css`` overrides
# the small set of token classes that matter (keyword, name, string,
# number, comment); everything else inherits the default palette.
_PYGMENTS_STYLE = "default"

_log = logging.getLogger(__name__)


def highlight_solidity(source: str) -> str:
    """Return the Pygments-highlighted HTML for a Solidity source fragment.

    The output is the bare span sequence (no wrapping div/pre). Empty input
    short-circuits to an empty string so an unimplemented function with
    ``source_code == ""`` doesn't yield a stray ``<span>`` line.
    """
    if not source:
        return ""
    return highlight(source, _LEXER, _FORMATTER)


def write_pygments_css(static_dir: Path) -> Path:
    """Regenerate ``static_dir/css/pygments.css`` and return its path.

    The file is overwritten on every server start. We do not check whether
    it already exists or whether contents would change: the cost is a few
    hundred bytes of disk write at boot, and the alternative (stale CSS
    after a Pygments upgrade) is worse.
    """
    formatter = HtmlFormatter(cssclass="src", style=_PYGMENTS_STYLE)
    css = formatter.get_style_defs(".src")
    target = static_dir / "css" / "pygments.css"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(css, encoding="utf-8")
    _log.debug("wrote pygments stylesheet to %s (%d bytes)", target, len(css))
    return target
