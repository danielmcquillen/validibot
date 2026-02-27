from importlib.metadata import PackageNotFoundError
from importlib.metadata import version

try:
    __version__ = version("validibot")
except PackageNotFoundError:
    # When running without the package formally installed (e.g. Docker
    # with --no-install-project), fall back to the version in pyproject.toml.
    from pathlib import Path

    try:
        import tomllib

        _pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        with _pyproject.open("rb") as _f:
            __version__ = tomllib.load(_f)["project"]["version"]
    except Exception:
        __version__ = "0.0.0"

__version_info__ = tuple(
    int(num) if num.isdigit() else num
    for num in __version__.replace("-", ".", 1).split(".")
)
