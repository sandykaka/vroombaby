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
# Smart path selection for different environments and projects
import os

# Try environment variable first
service_account_path = os.environ.get('FIREBASE_SERVICE_ACCOUNT_PATH')

if not service_account_path:
    # Auto-detect based on environment - Ethnopicks only
    if os.path.exists('/Users/sandeshkakade/gitRepos/vroombaby/ethnopicks_service_account_key.json'):
        # Local development with Ethnopicks
        service_account_path = '/Users/sandeshkakade/gitRepos/vroombaby/ethnopicks_service_account_key.json'
    elif os.path.exists('/home/ubuntu/vroombaby/ethnopicks_service_account_key.json'):
        # Ubuntu production with Ethnopicks
        service_account_path = '/home/ubuntu/vroombaby/ethnopicks_service_account_key.json'
    else:
        # Error - no valid service account found
        raise FileNotFoundError("Ethnopicks Firebase service account key not found. Please ensure ethnopicks_service_account_key.json exists.")

print(f"Using Firebase service account: {service_account_path}")

# Initialize Firebase Admin.
cred = credentials.Certificate(service_account_path)
firebase_admin.initialize_app(cred)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'website1.settings')
application = get_wsgi_application()
