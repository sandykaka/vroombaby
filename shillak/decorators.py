import logging
import os
from functools import wraps

import firebase_admin
from firebase_admin import auth as firebase_auth, credentials
from django.contrib.auth.models import User
from django.http import JsonResponse

logger = logging.getLogger(__name__)


def _get_shillak_firebase_app():
    """Get or initialize the Shillak Firebase app."""
    try:
        return firebase_admin.get_app('shillak')
    except ValueError:
        # App doesn't exist yet — initialize it
        key_path = None
        for path in [
            '/Users/sandeshkakade/gitRepos/vroombaby/shillak_service_account_key.json',
            '/home/ubuntu/vroombaby/shillak_service_account_key.json',
        ]:
            if os.path.exists(path):
                key_path = path
                break

        if not key_path:
            raise FileNotFoundError(
                "Shillak Firebase service account key not found. "
                "Download it from Firebase Console → Project Settings → Service Accounts."
            )

        logger.info(f"Initializing Shillak Firebase app with: {key_path}")
        cred = credentials.Certificate(key_path)
        return firebase_admin.initialize_app(cred, name='shillak')


def require_firebase_auth(f):
    """Verify Firebase ID token and get/create Django user for Shillak."""
    @wraps(f)
    def decorated_function(request, *args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return JsonResponse(
                {'error': 'Missing or invalid Authorization header'}, status=401
            )

        id_token = auth_header.split('Bearer ')[1]

        try:
            shillak_app = _get_shillak_firebase_app()
            decoded_token = firebase_auth.verify_id_token(id_token, app=shillak_app)
            firebase_uid = decoded_token['uid']
            phone = decoded_token.get('phone_number')
            email = decoded_token.get('email')

            username = phone if phone else firebase_uid
            user, created = User.objects.get_or_create(
                username=username,
                defaults={'email': email or ''}
            )

            request.user = user
            request.firebase_uid = firebase_uid

            return f(request, *args, **kwargs)

        except Exception as e:
            logger.error(f"Shillak Firebase auth failed: {e}")
            return JsonResponse(
                {'error': 'Invalid authentication token'}, status=401
            )

    return decorated_function
