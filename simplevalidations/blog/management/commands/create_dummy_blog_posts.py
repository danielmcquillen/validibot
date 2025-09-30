from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils.text import slugify

from simplevalidations.blog.models import BlogPost

LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
    "Curabitur vehicula nibh sed sagittis tincidunt. "
    "Sed non dolor vitae justo aliquet semper."
)


class Command(BaseCommand):
    help = "Create five simple BlogPost instances with sample content."

    def handle(self, *args, **options):
        created_posts = []
        for i in range(1, 6):
            title = f"Sample Post {i}"
            slug = slugify(title)
            post, created = BlogPost.objects.get_or_create(
                slug=slug,
            )
            post.title = title
            post.content = LOREM
            post.status = 1  # Published
            post.save()
            if created:
                created_posts.append(post)

        self.stdout.write(
            self.style.SUCCESS(f"Created {len(created_posts)} BlogPost objects.")
        )
        for p in created_posts:
            self.stdout.write(
                f"- id={getattr(p, 'id', None)} title={getattr(p, 'title', '')}"
            )
