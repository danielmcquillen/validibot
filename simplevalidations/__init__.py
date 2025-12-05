__version__ = "0.1.0"
__version_info__ = tuple(
    int(num) if num.isdigit() else num
    for num in __version__.replace("-", ".", 1).split(".")
)


# TODO : Review this when django-github-app is integrated, if ever
# Import validators early to ensure they're registered before django-github-app views
# from .validations import github  # noqa: F401
