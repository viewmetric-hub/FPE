from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone


class CustomUserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Users must have an email address')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('role', CustomUser.Role.PLATFORM_ADMIN)
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email=email, password=password, **extra_fields)


class CustomUser(AbstractBaseUser, PermissionsMixin):
    class Role(models.TextChoices):
        PLATFORM_ADMIN = 'PLATFORM_ADMIN', 'Platform Admin'
        CONSUMER_MANAGER = 'CONSUMER_MANAGER', 'Consumer Manager'
        PLANT_USER = 'PLANT_USER', 'Plant User'
        GENERATOR = 'GENERATOR', 'Generator'

    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=32, choices=Role.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['name', 'role']

    # `password` field is provided by AbstractBaseUser.

    def __str__(self) -> str:
        return f'{self.email} ({self.role})'


class UserProfile(models.Model):
    """
    Extended profile for CustomUser. Name and email live on CustomUser; logo is stored here.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='userprofile',
    )
    logo = models.ImageField(upload_to='logos/', null=True, blank=True)
    # After the real-time lock line on today’s plantwise AI table, this many extra
    # quarter-hour slots keep the locked (green) styling before yellow starts.
    plantwise_timeslot_variation_count = models.PositiveSmallIntegerField(default=7)

    def __str__(self) -> str:
        return f'Profile({self.user_id})'

