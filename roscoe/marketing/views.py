# Create your views here.

from django.views.generic import TemplateView

from roscoe.core.mixins import BreadcrumbMixin


class HomePageView(TemplateView):
    template_name = "marketing/home.html"
    http_method_names = ["get"]


class AboutPageView(BreadcrumbMixin, TemplateView):
    template_name = "marketing/about.html"
    http_method_names = ["get"]
    breadcrumbs = [
        {"name": "About", "url": ""},
    ]
