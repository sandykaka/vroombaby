import os

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.http import HttpResponseRedirect
from requests.auth import HTTPBasicAuth
import requests
import logging

import json
logger = logging.getLogger(__name__)

# Create your views here.
def index(request):
    logger.debug("Logging configuration successfully loaded.")
    return render(request, 'business/index.html')

def products(request):
    logger.debug("In product")
    logger.info("This is an info message")
    logger.error("This is an error message")
    return render(request, 'business/products.html')

def googleeb914ff572b518f7(request):
    return HttpResponse('business/googleeb914ff572b518f7.html')

def support_view(request):
    return render(request, 'business/support.html')
import os
import requests
from requests.auth import HTTPBasicAuth
from django.shortcuts import redirect
from django.http import JsonResponse,HttpResponseRedirect
import logging
from django.views.decorators.csrf import csrf_exempt
from django.urls import reverse
logger = logging.getLogger(__name__)
@csrf_exempt
def oauth_callback(request):
    """
    Handles the OAuth callback from Zoom. If the user is on an iOS device with your app
    installed and universal links are configured, iOS will open your app directly using thi
s HTTPS URL.
    If the view is reached, simply display a confirmation message.
    """
    code = request.GET.get('code', None)
    error = request.GET.get('error')

    if error:
        return JsonResponse({"error": error}, status=400)

    if code:
        # Option 1: You can display a confirmation page.
        html = f"""
        <html>
            <head><title>Authentication Complete</title></head>
            <body>
                <p>Authentication is complete. Please return to the app.</p>
            </body>
        </html>
        """
        return HttpResponse(html)
    return JsonResponse({"error": "No code received"}, status=400)


def apple_site(request):
    # Define the JSON data as a Python dict
    data = {
        "applinks": {
            "apps": [],
            "details": [
                {
                    "appID": "4WKM8GU86V.bleedblue.coffeeChat",
                    "paths": [ "/oauth/callback*" ]
                }
            ]
        }
    }
    # Convert the dict to a JSON string
    json_data = json.dumps(data)
    # Return an HTTP response with the appropriate content type
    return HttpResponse(json_data, content_type="application/json")
