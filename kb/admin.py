from django.contrib import admin

from .models import KnowledgeBase

admin.site.site_header = "NIKI · Knowledge e grafo"
admin.site.site_title = "Knowledge e grafo"


@admin.register(KnowledgeBase)
class KnowledgeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_default", "riservata")
    filter_horizontal = ("utenti",)
    readonly_fields = ("slug", "name", "is_default", "topics_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
