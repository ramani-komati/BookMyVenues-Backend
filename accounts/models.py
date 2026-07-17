from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.validators import RegexValidator
from django.db import models

# Reusable rule: exactly 10 digits (Indian mobile number without +91).
phone_validator = RegexValidator(
    regex=r'^\d{10}$',
    message='Phone number must be exactly 10 digits.',
)


class UserManager(BaseUserManager):
    """
    Tells Django how to create users for our custom model.
    Needed because the default manager expects a "username" field,
    but our login field is "phone".
    """

    def create_user(self, phone, password=None, **extra_fields):
        """Create a normal user (public visitor or vendor)."""
        if not phone:
            raise ValueError('Phone number is required.')

        # Normalize the email (lowercases the domain part) if given.
        email = extra_fields.pop('email', None)
        if email:
            email = self.normalize_email(email)

        user = self.model(phone=phone, email=email, **extra_fields)
        user.set_password(password)  # hashes the password — never stored as plain text
        user.save(using=self._db)
        return user

    def create_superuser(self, phone, password=None, **extra_fields):
        """Create an admin user for the Django admin site (manage.py createsuperuser)."""
        extra_fields.setdefault('role', User.Role.ADMIN)
        extra_fields.setdefault('is_staff', True)       # can log into /admin/
        extra_fields.setdefault('is_superuser', True)   # has every permission
        return self.create_user(phone, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """
    Custom user for Book My Venue.

    - Logs in with PHONE (not username).
    - Has a role: PUBLIC (default), VENDOR, or ADMIN.

    AbstractBaseUser gives us password handling + last_login.
    PermissionsMixin gives us is_superuser, groups, and permissions
    (needed for the Django admin to work).
    """

    class Role(models.TextChoices):
        # First value = stored in DB, second = human-readable label.
        PUBLIC = 'PUBLIC', 'Public'
        VENDOR = 'VENDOR', 'Vendor'
        ADMIN = 'ADMIN', 'Admin'

    # blank/null allowed: customers are auto-created at first OTP login,
    # before we know their name or email.
    name = models.CharField(max_length=150, blank=True, default='')
    phone = models.CharField(
        max_length=10,
        unique=True,
        validators=[phone_validator],
    )
    email = models.EmailField(unique=True, null=True, blank=True)
    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.PUBLIC,
    )

    # Standard Django flags:
    is_active = models.BooleanField(default=True)   # False = account disabled
    is_staff = models.BooleanField(default=False)   # True = may open /admin/
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    # The field used to log in:
    USERNAME_FIELD = 'phone'
    # Extra fields asked for by "manage.py createsuperuser":
    REQUIRED_FIELDS = ['name', 'email']

    def __str__(self):
        return f'{self.name} ({self.phone})'


class PhoneOTP(models.Model):
    """
    One OTP sent to one phone number.

    Lifecycle:
      created -> (user enters code) -> verified -> used
    - "verified" = the code was entered correctly (needed by vendor signup,
      where registration happens in a separate request after verification).
    - "used" = consumed forever; a used OTP can never log anyone in again.

    SECURITY: the code itself is stored HASHED (like a password), so even
    someone reading the database cannot see the OTP.
    """

    class Purpose(models.TextChoices):
        USER = 'USER', 'User'        # customer login
        VENDOR = 'VENDOR', 'Vendor'  # vendor login / signup

    LIFETIME_MINUTES = 5   # code expires 5 minutes after sending
    MAX_ATTEMPTS = 5       # wrong guesses allowed before the OTP is locked
    REGISTER_WINDOW_MINUTES = 30  # how long a verified OTP stays valid for vendor signup

    phone = models.CharField(max_length=10, validators=[phone_validator], db_index=True)
    code_hash = models.CharField(max_length=128)
    purpose = models.CharField(max_length=10, choices=Purpose.choices)
    attempts = models.PositiveSmallIntegerField(default=0)
    verified = models.BooleanField(default=False)
    used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'OTP for {self.phone} ({self.purpose})'
