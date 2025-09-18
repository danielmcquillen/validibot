from django.urls import path

from . import views

app_name = "blog"

urlpatterns = [
    path("", views.BlogPostList.as_view(), name="blog_list"),
    path("<slug:slug>/", views.BlogPostDetail.as_view(), name="blog_post_detail"),
]
