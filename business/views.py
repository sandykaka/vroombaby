import logging
import secrets
import urllib
from datetime import datetime
from functools import wraps
import base64
import json
import requests
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render, redirect
from django.utils.safestring import mark_safe
from django.views.decorators.csrf import csrf_exempt
from firebase_admin import auth as firebase_auth

from website1 import settings
from .models import ZoomMeeting

logger = logging.getLogger(__name__)

def index(request):
    return render(request, 'business/index.html')

def products(request):
    return render(request, 'business/products.html')

def googleeb914ff572b518f7(request):
    return HttpResponse('business/googleeb914ff572b518f7.html')

def support_view(request):
    return render(request, 'business/support.html')

def privacy_view(request):
    return render(request, 'business/privacy.html')

def get_zoom_access_token():
    token_url = f"https://zoom.us/oauth/token?grant_type=account_credentials&account_id={settings.ZOOM_ACCOUNT_ID}"
    auth_str = f"{settings.ZOOM_CLIENT_ID}:{settings.ZOOM_CLIENT_SECRET}"
    auth_b64 = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
    headers = {"Authorization": f"Basic {auth_b64}"}
    response = requests.post(token_url, headers=headers)
    if response.status_code == 200:
        token_info = response.json()
        return token_info.get("access_token")
    else:
        print("Error obtaining access token:", response.json())
        return None

def firebase_login_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        auth_header = request.META.get('HTTP_AUTHORIZATION')
        if not auth_header or not auth_header.startswith('Bearer '):
            return JsonResponse({"error": "Authentication credentials were not provided."}, status=401)
        id_token = auth_header.split(" ")[1]
        try:
            decoded_token = firebase_auth.verify_id_token(id_token)
            # Attach the decoded token (or user info) to the request.
            request.firebase_user = decoded_token
        except Exception as e:
            return JsonResponse({"error": "Invalid token", "details": str(e)}, status=401)
        return view_func(request, *args, **kwargs)
    return _wrapped_view

@csrf_exempt
@firebase_login_required
def create_zoom_meeting(request):
    if request.method != "POST":
        return JsonResponse({"error": "Only POST method is allowed."}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "Invalid JSON data."}, status=400)

    topic = data.get("topic", "Scheduled Meeting")
    start_time = data.get("start_time")
    duration = data.get("duration", 60)
    host_name = data.get("host_name", "Unknown Host")
    # Use the authenticated user's email from firebase_token instead of request.user.email.
    host_email = request.firebase_user.get("email", "No host email")
    linkedin_profile_url = data.get("linkedin_profile_url", "No linkedin url")
    linkedin_profile_picture = data.get("linkedin_profile_picture", "No linkedin picture")

    if not start_time:
        return JsonResponse({"error": "Missing required field: start_time."}, status=400)

    access_token = get_zoom_access_token()
    if not access_token:
        return JsonResponse({"error": "Could not obtain Zoom access token."}, status=500)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    meeting_payload = {
        "topic": topic,
        "type": 2,  # Scheduled meeting
        "start_time": start_time,
        "duration": duration,
        "timezone": "UTC",  # Adjust if necessary.
        "settings": {
            "host_video": True,
            "participant_video": True,
            "join_before_host": False
        }
    }

    zoom_endpoint = "https://api.zoom.us/v2/users/me/meetings"
    response = requests.post(zoom_endpoint, headers=headers, json=meeting_payload)

    if response.status_code != 201:
        return JsonResponse({
            "error": "Failed to create meeting.",
            "details": response.json()
        }, status=response.status_code)

    meeting_details = response.json()
    # Override meeting details with our host and LinkedIn values.
    meeting_details["host_name"] = host_name
    meeting_details["host_email"] = host_email
    meeting_details["linkedin_profile_url"] = linkedin_profile_url
    meeting_details["linkedin_profile_picture"] = linkedin_profile_picture

    try:
        dt = datetime.fromisoformat(meeting_details["start_time"].replace("Z", "+00:00"))
    except Exception as e:
        return JsonResponse({"error": f"Invalid start_time format: {str(e)}"}, status=500)

    ZoomMeeting.objects.create(
        zoom_id=meeting_details.get("id"),
        topic=meeting_details.get("topic", topic),
        join_url=meeting_details.get("join_url"),
        start_time=dt,
        duration=meeting_details.get("duration", duration),
        host_name=meeting_details.get("host_name"),
        host_email=meeting_details.get("host_email"),
        linkedin_profile_url=meeting_details.get("linkedin_profile_url"),
        linkedin_profile_picture=meeting_details.get("linkedin_profile_picture")
    )

    return JsonResponse(meeting_details, status=201)

@csrf_exempt
def get_meetings(request):
    if request.method == "GET":
        meetings = ZoomMeeting.objects.all().order_by("start_time")
        meeting_list = []
        for meeting in meetings:
            try:
                start_time_str = meeting.start_time.isoformat() if meeting.start_time else ""
            except Exception as e:
                logger.debug(f"Error formatting start_time for meeting {meeting.zoom_id}: {e}")
                start_time_str = "Invalid Date"

            meeting_list.append({
                "id": meeting.zoom_id,
                "topic": meeting.topic,
                "join_url": meeting.join_url,
                "start_time": start_time_str,
                "duration": meeting.duration,
                "host_name": meeting.host_name,
                "host_email": meeting.host_email,
                "linkedin_profile_url": meeting.linkedin_profile_url,
                "linkedin_profile_picture": meeting.linkedin_profile_picture,
            })
        return JsonResponse({"meetings": meeting_list}, status=200)
    else:
        return JsonResponse({"error": "Only GET method is allowed."}, status=405)

@csrf_exempt
@firebase_login_required  # Ensures that the request is authenticated via Firebase token.
def delete_meeting(request, meeting_id):
    if request.method != "DELETE":
        return JsonResponse({"error": "Only DELETE method is allowed."}, status=405)

    try:
        # Assuming meeting_id corresponds to the ZoomMeeting.zoom_id field.
        meeting = ZoomMeeting.objects.get(zoom_id=meeting_id)
    except ZoomMeeting.DoesNotExist:
        return JsonResponse({"error": "Meeting not found."}, status=404)

    # Use the Firebase token information instead of request.user.
    current_email = request.firebase_user.get("email", "").lower()
    meeting_host_email = meeting.host_email.lower() if meeting.host_email else ""

    # Debug print statements (remove in production).
    logger.debug("Current user email from token:", current_email)
    logger.debug("Meeting host email:", meeting_host_email)

    if current_email != meeting_host_email:
        return JsonResponse({"error": "You are not authorized to delete this meeting."}, status=403)

    meeting.delete()
    # 204 No Content indicates success with no response body.
    return JsonResponse({}, status=204)

@csrf_exempt
@firebase_login_required
def update_meeting(request, meeting_id):
    if request.method not in ["PUT", "PATCH"]:
        return JsonResponse({"error": "Only PUT or PATCH method is allowed."}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception as e:
        return JsonResponse({"error": "Invalid JSON data.", "details": str(e)}, status=400)

    try:
        # Assuming meeting_id corresponds to ZoomMeeting.zoom_id
        meeting = ZoomMeeting.objects.get(zoom_id=meeting_id)
    except ZoomMeeting.DoesNotExist:
        return JsonResponse({"error": "Meeting not found."}, status=404)

    # Verify that the current user is authorized to update this meeting.
    current_email = request.firebase_user.get("email", "").lower()
    if meeting.host_email.lower() != current_email:
        return JsonResponse({"error": "You are not authorized to update this meeting."}, status=403)

    # Update fields if they are provided in the request.
    if "topic" in data:
        meeting.topic = data["topic"]
    if "start_time" in data:
        try:
            # Replace "Z" with "+00:00" so that datetime.fromisoformat can parse the string.
            meeting.start_time = datetime.fromisoformat(data["start_time"].replace("Z", "+00:00"))
        except Exception as e:
            return JsonResponse({"error": f"Invalid start_time format: {str(e)}"}, status=400)
    if "duration" in data:
        meeting.duration = data["duration"]
    if "host_name" in data:
        meeting.host_name = data["host_name"]
    if "linkedin_profile_url" in data:
        meeting.linkedin_profile_url = data["linkedin_profile_url"]
    if "linkedin_profile_picture" in data:
        meeting.linkedin_profile_picture = data["linkedin_profile_picture"]

    meeting.save()

    updated_meeting = {
        "id": meeting.zoom_id,
        "topic": meeting.topic,
        "join_url": meeting.join_url,
        "start_time": meeting.start_time.isoformat(),
        "duration": meeting.duration,
        "host_name": meeting.host_name,
        "host_email": meeting.host_email,
        "linkedin_profile_url": meeting.linkedin_profile_url,
        "linkedin_profile_picture": meeting.linkedin_profile_picture,
    }

    return JsonResponse(updated_meeting, status=200)

# ===================== LinkedIn OAuth Endpoints =====================

def linkedin_login(request):
    # Generate a secure random state token for CSRF protection.
    logger.debug("LinkedIn login method")
    state = secrets.token_urlsafe(16)
    request.session["linkedin_oauth_state"] = state

    params = {
        "response_type": "code",
        "client_id": settings.LINKEDIN_CLIENT_ID,
        "redirect_uri": settings.LINKEDIN_REDIRECT_URI,  # e.g., "https://coffeewithexpert.com/linkedin-callback"
        # Request the OpenID Connect scopes. You can add additional scopes if needed.
        "scope": "openid profile email",
        "state": state,
    }
    auth_url = f"https://www.linkedin.com/oauth/v2/authorization?{urllib.parse.urlencode(params)}"
    logger.debug("LinkedIn login url: %s", auth_url)
    return redirect(auth_url)

def custom_redirect(url):
    # Create an HTML page that forces the redirect via meta refresh and JavaScript.
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="0;url={url}">
  <script type="text/javascript">
    window.location.href = "{url}";
  </script>
  <title>Redirecting…</title>
</head>
<body>
  <p>If you are not redirected automatically, <a href="{url}">click here</a>.</p>
</body>
</html>"""
    # Mark the string as safe so it isn’t processed by the Django template engine.
    return HttpResponse(mark_safe(html), content_type="text/html")

@csrf_exempt
# @firebase_login_required
def linkedin_callback(request):
    # Extract the authorization code and state from the query parameters
    logger.debug("In linkedin callback debug")
    logger.info("In linkedin callback info")
    code = request.GET.get("code")
    state = request.GET.get("state")
    if not code:
        logger.error("Missing authorization code in callback")
        return JsonResponse({"error": "Missing authorization code"}, status=400)

    # Exchange the authorization code for an access token.
    token_url = "https://www.linkedin.com/oauth/v2/accessToken"
    token_params = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.LINKEDIN_REDIRECT_URI,
        "client_id": settings.LINKEDIN_CLIENT_ID,
        "client_secret": settings.LINKEDIN_CLIENT_SECRET,
    }
    token_response = requests.post(token_url, data=token_params)
    if token_response.status_code != 200:
        logger.error("Failed to obtain access token: %s", token_response.text)
        return JsonResponse({
            "error": "Failed to obtain access token",
            "details": token_response.json()
        }, status=token_response.status_code)

    token_data = token_response.json()
    access_token = token_data.get("access_token")
    if not access_token:
        logger.error("Access token not found in token response: %s", token_data)
        return JsonResponse({"error": "Access token not found"}, status=400)

    # Fetch the user's profile using the access token.
    # We use a projection to request the id, localizedFirstName, and localizedLastName.
    profile_url = "https://api.linkedin.com/v2/me?projection=(id,localizedFirstName,localizedLastName)"
    headers = {"Authorization": f"Bearer {access_token}"}
    profile_response = requests.get(profile_url, headers=headers)
    if profile_response.status_code != 200:
        logger.error("Failed to fetch LinkedIn profile: %s", profile_response.text)
        return JsonResponse({
            "error": "Failed to fetch LinkedIn profile",
            "details": profile_response.json()
        }, status=profile_response.status_code)

    profile_data = profile_response.json()
    linkedin_id = profile_data.get("id", "")
    first_name = profile_data.get("localizedFirstName", "")
    last_name = profile_data.get("localizedLastName", "")
    full_name = f"{first_name} {last_name}".strip()

    # (Optional) If you need the user's email, LinkedIn requires a separate API call.
    # See LinkedIn's documentation for details.

    # Build a custom URL to redirect back to your iOS app.
    ios_redirect_scheme = "coffeewithexpert://linkedin_callback"
    query_params = {
        "full_name": full_name,
        "linkedin_id": linkedin_id,
        # Add additional details as needed.
    }
    redirect_url = ios_redirect_scheme + "?" + urllib.parse.urlencode(query_params)
    logger.info("Redirecting to: %s", redirect_url)

    return HttpResponseRedirect(redirect_url)

def apple_app_site_association(request):
    # Define the AASA content (adjust values as needed)
    data = {
        "webcredentials": {
            "apps": ["4WKM8GU86V.bleedblue.CoffeeWithExpert"]
        },
        "applinks": {
            "apps": [],
            "details": [
                {
                    "appID": "4WKM8GU86V.bleedblue.CoffeeWithExpert",
                    "paths": ["*"]
                }
            ]
        }
    }
    # Convert the data to JSON
    json_data = json.dumps(data)
    # Return the response with the correct content type
    return HttpResponse(json_data, content_type="application/json")

@csrf_exempt
@firebase_login_required
def get_user_linkedin_details(request):
    """
    Returns LinkedIn details (host_name, linkedin_profile_url, linkedin_profile_picture)
    for the currently authenticated user, based on the most recent meeting record.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Only GET method is allowed."}, status=405)

    user_email = request.firebase_user.get("email")
    if not user_email:
        return JsonResponse({"error": "User email not found."}, status=400)

    # Query for meetings associated with this user's email, ordered by start_time descending.
    meetings = ZoomMeeting.objects.filter(host_email=user_email).order_by("-start_time")
    if meetings.exists():
        meeting = meetings.first()
        data = {
            "topic": meeting.topic or "",
            "host_name": meeting.host_name or "",
            "linkedin_profile_url": meeting.linkedin_profile_url or "",
            "linkedin_profile_picture": meeting.linkedin_profile_picture or "",
        }
        logger.debug("Returning LinkedIn details: %s", data)
        return JsonResponse(data, status=200)
    else:
        # No meeting record exists; return empty strings.
        data = {
            "topic": "",
            "host_name": "",
            "linkedin_profile_url": "",
            "linkedin_profile_picture": "",
        }
        logger.debug("No LinkedIn details found for user %s", user_email)
        return JsonResponse(data, status=200)