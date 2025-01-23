from django.http import HttpResponse
from django.shortcuts import render
from website1 import settings
import urllib3
import json
from urllib3.util import Retry
from urllib.parse import urlencode
from urllib3 import make_headers
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import redirect

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

    # Step 3: Prepare the token data and encode it
    token_data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
    }
    encoded_data = urlencode(token_data)

    # Step 4: Basic authentication header (client_id:client_secret)
    auth = make_headers(auth_basic='{}:{}'.format(client_id, client_secret))

    # Initialize the PoolManager for urllib3
    http = urllib3.PoolManager()

    # Step 5: Send the POST request using urllib3
    response = http.request(
        'POST',
        token_url,
        body=encoded_data,
        headers={**auth, 'Content-Type': 'application/x-www-form-urlencoded'},
        retries=Retry(3, redirect=2)  # Optional: Retry logic in case of failure
    )

    # Step 6: Check the response status
    if response.status != 200:
        return JsonResponse({'error': 'Failed to exchange code for token'}, status=500)

    # Step 7: Extract the access token and refresh token from the JSON response
    try:
        # Decode and parse the JSON response
        token_info = response.data.decode('utf-8')
        token_info = json.loads(token_info)

        access_token = token_info.get('access_token')
        refresh_token = token_info.get('refresh_token')

        if not access_token or not refresh_token:
            return JsonResponse({'error': 'Missing access_token or refresh_token'}, status=400)

    except ValueError:
        # Handle case where JSON is invalid
        return JsonResponse({'error': 'Invalid JSON response from token endpoint'}, status=500)

    # Step 8: Store the tokens in the session
    request.session['access_token'] = access_token
    request.session['refresh_token'] = refresh_token

    # Step 9: Redirect the user back to the Swift app (via deep link or custom URL)
    app_redirect_url = "coffeeChat://oauth-callback?access_token={}&refresh_token={}".format(access_token, refresh_token)

    return redirect(app_redirect_url)

# Create your views here.
def index(request):
    return render(request, 'business/index.html')

def products(request):
    return render(request, 'business/products.html')

def googleeb914ff572b518f7(request):
    return HttpResponse('business/googleeb914ff572b518f7.html')
