"""
WSGI config for website1 project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/2.1/howto/deployment/wsgi/
"""

import os
import firebase_admin
from firebase_admin import credentials
from django.core.wsgi import get_wsgi_application

# Set the path to your service account key JSON file.
# It’s best to load this path from an environment variable.
service_account_path = os.environ.get('FIREBASE_SERVICE_ACCOUNT_PATH')

# Initialize Firebase Admin.
cred = credentials.Certificate(service_account_path)
firebase_admin.initialize_app(cred)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'your_project.settings')
application = get_wsgi_application()
