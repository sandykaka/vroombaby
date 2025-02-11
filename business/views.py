from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
import base64
import json
import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from .models import ZoomMeeting

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

@csrf_exempt
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
    linkedin_profile_url = data.get("linkedin_profile_url", "No linkedin url")

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
    # Inject the host_name into the meeting details.
    meeting_details["host_name"] = host_name
    meeting_details["linkedin_profile_url"] = linkedin_profile_url

    # Save the meeting details to the database.
    # Parse the start_time string into a Python datetime if needed.
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(meeting_details["start_time"].replace("Z", "+00:00"))
    except Exception:
        dt = None

    ZoomMeeting.objects.create(
        zoom_id=meeting_details.get("id"),
        topic=meeting_details.get("topic", topic),
        join_url=meeting_details.get("join_url"),
        start_time=dt,
        duration=meeting_details.get("duration", duration),
        host_name=meeting_details.get("host_name"),
        linkedin_profile_url=meeting_details.get("linkedin_profile_url")
    )

    return JsonResponse(meeting_details, status=201)

# Optionally, create an endpoint to fetch all meetings.
@csrf_exempt
def get_meetings(request):
    if request.method == "GET":
        meetings = ZoomMeeting.objects.all().order_by("start_time")
        meeting_list = []
        for meeting in meetings:
            meeting_list.append({
                "id": meeting.zoom_id,
                "topic": meeting.topic,
                "join_url": meeting.join_url,
                "start_time": meeting.start_time.isoformat(),
                "duration": meeting.duration,
                "host_name": meeting.host_name,
                "linkedin_profile_url": meeting.linkedin_profile_url
            })
        return JsonResponse({"meetings": meeting_list}, status=200)
    else:
        return JsonResponse({"error": "Only GET method is allowed."}, status=405)

@csrf_exempt
@login_required  # Ensure the user is authenticated.
def delete_meeting(request, meeting_id):
    if request.method != "DELETE":
        return JsonResponse({"error": "Only DELETE method is allowed."}, status=405)

    try:
        # Assuming meeting_id in the URL corresponds to the ZoomMeeting.zoom_id field.
        meeting = ZoomMeeting.objects.get(zoom_id=meeting_id)
    except ZoomMeeting.DoesNotExist:
        return JsonResponse({"error": "Meeting not found."}, status=404)

    # Check that the current user is the meeting host.
    # This example assumes meeting.host_name stores the host's email.
    if meeting.host_name.lower() != request.user.email.lower():
        return JsonResponse({"error": "You are not authorized to delete this meeting."}, status=403)

    meeting.delete()
    # 204 No Content indicates success with no response body.
    return JsonResponse({}, status=204)