from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import CustomUser, UserProfile
from .serializers import LoginSerializer


class TokenRefreshView(APIView):
    """Refresh access token. POST { refresh: "..." } -> { access: "..." }"""
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        refresh_value = request.data.get('refresh')
        if not refresh_value:
            return Response({'detail': 'refresh token is required'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            token = RefreshToken(refresh_value)
            user = token.user
            new_access = token.access_token
            new_access['role'] = user.role
            return Response({'access': str(new_access)}, status=status.HTTP_200_OK)
        except Exception:
            return Response({'detail': 'Invalid or expired refresh token'}, status=status.HTTP_401_UNAUTHORIZED)


class LoginView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        return Response({'access': data['access'], 'refresh': data['refresh']}, status=status.HTTP_200_OK)


class LogoutView(APIView):
    def post(self, request):
        refresh_value = request.data.get('refresh')
        if not refresh_value:
            return Response({'detail': 'refresh token is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            token = RefreshToken(refresh_value)
            token.blacklist()
        except Exception:
            return Response({'detail': 'Invalid refresh token'}, status=status.HTTP_400_BAD_REQUEST)

        return Response({'detail': 'Logged out'}, status=status.HTTP_200_OK)


class MeView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def _profile_payload(self, request, user):
        profile, _ = UserProfile.objects.get_or_create(user=user)
        logo_url = None
        if profile.logo:
            logo_url = request.build_absolute_uri(profile.logo.url)
        return {
            'id': user.id,
            'email': user.email,
            'name': user.name,
            'role': user.role,
            'logo_url': logo_url,
            'plantwise_timeslot_variation_count': profile.plantwise_timeslot_variation_count,
        }

    def get(self, request):
        return Response(self._profile_payload(request, request.user), status=status.HTTP_200_OK)

    def patch(self, request):
        u = request.user
        profile, _ = UserProfile.objects.get_or_create(user=u)

        update_fields = []
        profile_changed = False
        if 'name' in request.data:
            n = str(request.data.get('name') or '').strip()
            if not n:
                return Response({'detail': 'Name cannot be empty.'}, status=status.HTTP_400_BAD_REQUEST)
            u.name = n[:255]
            update_fields.append('name')
        if 'email' in request.data:
            em = str(request.data.get('email') or '').strip().lower()
            if not em:
                return Response({'detail': 'Email cannot be empty.'}, status=status.HTTP_400_BAD_REQUEST)
            if CustomUser.objects.filter(email=em).exclude(pk=u.pk).exists():
                return Response({'detail': 'That email is already in use.'}, status=status.HTTP_400_BAD_REQUEST)
            u.email = em
            update_fields.append('email')
        if 'logo' in request.FILES:
            profile.logo = request.FILES['logo']
            profile.save(update_fields=['logo'])

        if 'plantwise_timeslot_variation_count' in request.data:
            raw = request.data.get('plantwise_timeslot_variation_count')
            try:
                n = int(raw)
            except (TypeError, ValueError):
                return Response(
                    {'detail': 'plantwise_timeslot_variation_count must be an integer between 0 and 96.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if n < 0 or n > 96:
                return Response(
                    {'detail': 'plantwise_timeslot_variation_count must be between 0 and 96.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            profile.plantwise_timeslot_variation_count = n
            profile.save(update_fields=['plantwise_timeslot_variation_count'])
            profile_changed = True

        if update_fields:
            u.save(update_fields=update_fields)

        if not update_fields and 'logo' not in request.FILES and not profile_changed:
            return Response(
                {'detail': 'No updatable fields provided (name, email, logo, plantwise_timeslot_variation_count).'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(self._profile_payload(request, u), status=status.HTTP_200_OK)


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        old_password = request.data.get('old_password') or request.data.get('current_password')
        new_password = request.data.get('new_password')
        if not old_password or not new_password:
            return Response(
                {'detail': 'current_password and new_password are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(new_password) < 8:
            return Response({'detail': 'New password must be at least 8 characters.'}, status=status.HTTP_400_BAD_REQUEST)
        if not request.user.check_password(old_password):
            return Response({'detail': 'Current password is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)
        request.user.set_password(new_password)
        request.user.save(update_fields=['password'])
        return Response({'detail': 'Password updated successfully.'}, status=status.HTTP_200_OK)

