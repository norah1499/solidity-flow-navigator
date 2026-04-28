"""Layer 1 compilation: thin wrapper around crytic-compile.

Per spec §9.1, this layer hard-fails on any compilation issue. There is no
auto-remediation and no partial-analysis fallback. If crytic-compile cannot
produce artifacts, we raise CompilationFailure carrying the underlying error
verbatim and let the CLI surface it.
"""

from pathlib import Path

from crytic_compile import CryticCompile


class CompilationFailure(Exception):
    """Raised when crytic-compile cannot compile the target repository.

    The string form of the exception is the underlying error verbatim, so
    the CLI can print it without paraphrasing (spec §9.1).
    """


def compile_repo(path: Path | str) -> CryticCompile:
    """Compile the Solidity project rooted at ``path``.

    Returns the populated ``CryticCompile`` object. Raises ``CompilationFailure``
    on any failure — including a missing or non-directory path — with the
    underlying error preserved verbatim in the exception message.

    The returned object stays inside Layer 1; only ``RepoFacts`` crosses the
    Layer 1 / Layer 2 boundary.
    """

    repo_path = Path(path).resolve()

    if not repo_path.exists():
        raise CompilationFailure(f"path does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise CompilationFailure(f"path is not a directory: {repo_path}")

    try:
        return CryticCompile(target=str(repo_path))
    except Exception as exc:
        raise CompilationFailure(str(exc)) from exc
