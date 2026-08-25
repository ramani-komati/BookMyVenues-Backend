from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """
    Admin page for our custom User.
    We extend Django's built-in UserAdmin but swap "username" for "phone".
    """

    # Columns shown in the user list:
    list_display = ('phone', 'name', 'email', 'role', 'is_active', 'date_joined')
    list_filter = ('role', 'is_active', 'is_staff')
    search_fields = ('phone', 'name', 'email')
    ordering = ('-date_joined',)

    def get_readonly_fields(self, request, obj=None):
        """Only a superuser may change the privilege fields.

        `role`, `is_staff` and `is_superuser` are the escalation path: editing
        them here grants super-admin access WITHOUT the OTP step the panel
        enforces, and Django's admin logs it to its own LogEntry rather than
        our AuditEntry trail. Staff who are not superusers get a read-only
        view of them."""
        readonly = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            readonly += ['role', 'is_staff', 'is_superuser',
                         'groups', 'user_permissions']
        return readonly

    # Field layout when EDITING an existing user:
    fieldsets = (
        (None, {'fields': ('phone', 'password')}),
        ('Personal info', {'fields': ('name', 'email', 'role')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Dates', {'fields': ('last_login',)}),
    )

    # Field layout when ADDING a new user from the admin:
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('phone', 'name', 'email', 'role', 'password1', 'password2'),
        }),
    )
