from django.contrib import admin

from validibot.blog.models import BlogPost


# Register your models here.
@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    list_display = ("title", "summary", "slug", "author", "status", "published_on")
    prepopulated_fields = {"slug": ("title",)}
    search_fields = ("title", "content")
    list_filter = ("status", "published_on", "author")
