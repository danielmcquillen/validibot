# Create your views here.
# help/views.py
from django.contrib.flatpages.views import flatpage
from django.shortcuts import render


def help_page(request, path="index"):
    """
    Wrap django.contrib.flatpages.views.flatpage so that:
      /help/                 -> FlatPage with url="/help/index/"
      /help/getting-started/ -> FlatPage with url="/help/getting-started/"
      /help/rulesets/fmu/    -> FlatPage with url="/help/rulesets/fmu/"
    """
    if not path.endswith("/"):
        path = path + "/"
    full_url = f"/help/{path}"
    return flatpage(request, url=full_url)
