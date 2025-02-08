from django.http import HttpResponse
from django.shortcuts import render
import base64
import json
import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings

# Create your views here.
def index(request):
    return render(request, 'business/index.html')

def products(request):
    return render(request, 'business/products.html')

def googleeb914ff572b518f7(request):
    return HttpResponse('business/googleeb914ff572b518f7.html')

def support_view(request):
    return render(request, 'business/support.html')

def get_zoom_access_token():
    """
    Use the Server-to-Server OAuth client credentials flow to obtain an access token.
    """
    token_url = f"https://zoom.us/oauth/token?grant_type=account_credentials&account_id={settings.ZOOM_ACCOUNT_ID}"
    auth_str = f"{settings.ZOOM_CLIENT_ID}:{settings.ZOOM_CLIENT_SECRET}"
    auth_b64 = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
    headers = {
        "Authorization": f"Basic {auth_b64}"
    }

    response = requests.post(token_url, headers=headers)
    if response.status_code == 200:
        token_info = response.json()
        access_token = token_info.get("access_token")
        return access_token
    else:
        print("Error obtaining access token:", response.json())
        return None

@csrf_exempt
def create_zoom_meeting(request):
    """
    Create a Zoom meeting via the REST API.
    Expects a POST request with a JSON body containing:
      - topic (optional): meeting topic (default: "Scheduled Meeting")
      - start_time (required): ISO8601 string (e.g., "2025-02-07T11:00:00Z")
      - duration (optional): duration in minutes (default: 60)
    Returns the meeting details as JSON, including the join_url.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Only POST method is allowed."}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception as e:
        return JsonResponse({"error": "Invalid JSON data."}, status=400)

    topic = data.get("topic", "Scheduled Meeting")
    start_time = data.get("start_time")
    duration = data.get("duration", 60)

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
        "type": 2,  # 2 indicates a scheduled meeting.
        "start_time": start_time,
        "duration": duration,
        "timezone": "UTC",  # Adjust this if needed.
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

    return JsonResponse(response.json(), status=201)
