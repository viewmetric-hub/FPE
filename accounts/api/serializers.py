from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import CustomUser


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')

        try:
            user = CustomUser.objects.get(email=email)
        except CustomUser.DoesNotExist as exc:
            raise serializers.ValidationError('Invalid credentials') from exc

        if not user.is_active:
            raise serializers.ValidationError('User is inactive')

        # Custom authentication against the email/password fields.
        if not user.check_password(password):
            raise serializers.ValidationError('Invalid credentials')

        refresh = RefreshToken.for_user(user)
        # Add role claim so the frontend can route UI without extra API calls.
        access = refresh.access_token
        access['role'] = user.role
        return {
            'user': user,
            'access': str(access),
            'refresh': str(refresh),
        }

