import os

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.http import HttpResponseRedirect
from requests.auth import HTTPBasicAuth
import requests

# Create your views here.
def index(request):
    return render(request, 'business/index.html')

def products(request):
    return render(request, 'business/products.html')

def googleeb914ff572b518f7(request):
    return HttpResponse('business/googleeb914ff572b518f7.html')

def support_view(request):
    return render(request, 'business/support.html')

import os
import requests
from requests.auth import HTTPBasicAuth
from django.shortcuts import redirect
from django.http import JsonResponse, HttpResponseRedirect

def oauth_callback(request):
    code = request.GET.get('code', None)
    error = request.GET.get('error')

    if error:
        return JsonResponse({"error": error}, status=400)
    if code:
        # Exchange the authorization code for an access token
        access_token = exchange_code_for_access_token(code)

        if access_token:
            # Redirect user to Swift app using custom URL scheme
            redirect_url = f"coffeeChat://oauth-callback?access_token={access_token}"
            return HttpResponseRedirect(redirect_url)
        else:
            return JsonResponse({"error": "Failed to get access token"}, status=400)

    return JsonResponse({"error": "No code returned"}, status=400)

def exchange_code_for_access_token(code):
    token_url = 'https://zoom.us/oauth/token'
    client_id = os.getenv("ZOOM_CLIENT_ID")
    client_secret = os.getenv("ZOOM_CLIENT_SECRET")
    redirect_uri = os.getenv("ZOOM_REDIRECT_URI")

    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri
    }

    response = requests.post(token_url, data=data, auth=HTTPBasicAuth(client_id, client_secret))

    if response.status_code == 200:
        token_data = response.json()
        return token_data.get('access_token')
    else:
        print("Zoom OAuth error:", response.json())
        return None
