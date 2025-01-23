from django.http import HttpResponse
from django.shortcuts import render

import requests
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import redirect

from website1 import settings


def oauth_callback(request):
    # Step 1: Extract the 'code' parameter from the URL
    code = request.GET.get('code')
    if not code:
        return HttpResponseBadRequest("Missing authorization code")

    # Step 2: Prepare data to exchange the authorization code for an access token
    token_url = 'https://zoom.us/oauth/token'  # Replace with the OAuth provider's token URL
    client_id = settings.OAUTH_CLIENT_ID  # OAuth client ID (stored in settings.py)
    client_secret = settings.OAUTH_CLIENT_SECRET  # OAuth client secret (stored in settings.py)
    redirect_uri = settings.OAUTH_REDIRECT_URI  # Your OAuth redirect URI

    # Step 3: Exchange the authorization code for an access token
    token_data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
    }
    auth = (client_id, client_secret)  # Basic Auth (client_id:client_secret)

    response = requests.post(token_url, data=token_data, auth=auth)
    if response.status_code != 200:
        return JsonResponse({'error': 'Failed to exchange code for token'}, status=500)

    # Step 4: Extract the access token and refresh token from the response
    token_info = response.json()
    access_token = token_info.get('access_token')
    refresh_token = token_info.get('refresh_token')

    # Optionally, store the tokens in a session or database (for your app to use later)
    request.session['access_token'] = access_token
    request.session['refresh_token'] = refresh_token

    # Step 5: Redirect the user back to the Swift app (via deep link or custom URL)
    # The redirect URI that the Swift app is listening to (should match with your app configuration)
    app_redirect_url = f"coffeeChat://oauth-callback?access_token={access_token}&refresh_token={refresh_token}"

    return redirect(app_redirect_url)

# Create your views here.
def index(request):
    return render(request, 'vroombaby/index.html')

def googleeb914ff572b518f7(request):
    return HttpResponse('vroombaby/googleeb914ff572b518f7.html')