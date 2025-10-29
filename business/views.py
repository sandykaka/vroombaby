import logging
import secrets
import urllib
from datetime import datetime
from functools import wraps
import base64
import json, os, time
import hashlib
import csv
import requests
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render, redirect
from django.utils.safestring import mark_safe
from django.views.decorators.csrf import csrf_exempt
from firebase_admin import auth as firebase_auth
from django.contrib.auth.models import User

from website1 import settings
from .models import ZoomMeeting, UserProfile, DeliveryAddress, PaymentMethod, AIOrder

import googlemaps
from openai import OpenAI
from django.conf     import settings
from django.http     import JsonResponse, HttpResponseBadRequest
from django.views.decorators.http import require_GET
from .utils.reviews_cache import dish_csv_path, category_csv_path, is_stale, ensure_csv_async, FULL_TARGET, REVIEWS_DIR, QUEUE_DIR
from .utils.yelp_queue import add_place_id_to_queue
from django.core.cache import cache
import pandas as pd
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

def get_weekly_hours(contact_info):
    """Extract weekly hours from contact_info"""
    # Try current_opening_hours first
    current_hours = contact_info.get("current_opening_hours")
    if current_hours and isinstance(current_hours, dict):
        weekday_text = current_hours.get("weekday_text", [])
        if weekday_text:
            return weekday_text
    
    # Fallback to regular opening_hours
    opening_hours = contact_info.get("opening_hours")
    if opening_hours and isinstance(opening_hours, dict):
        weekday_text = opening_hours.get("weekday_text", [])
        if weekday_text:
            return weekday_text
    
    return None

def _clean_website_url(url):
    """Clean website URL by removing query parameters and tracking info"""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        # Keep only scheme, netloc, and path - remove query parameters
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        # Remove trailing slash for consistency
        if clean_url.endswith('/') and len(clean_url) > 1:
            clean_url = clean_url.rstrip('/')
        return clean_url
    except Exception:
        return url  # Return original if parsing fails

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


# initialize clients
gmaps = googlemaps.Client(key=settings.GOOGLE_API_KEY)
client  = OpenAI(api_key=settings.OPENAI_API_KEY)
TABS = {"popular", "indian","american","chinese","mexican","italian"}


def _count_reviews(place_id: str) -> int:
    """Count reviews in var/reviews/<place_id>/reviews.json (best signal for 'full')."""
    reviews_json = Path(REVIEWS_DIR) / place_id / "reviews.json"
    try:
        data = json.loads(reviews_json.read_text(encoding="utf-8"))
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0

@require_GET
def restaurant_contact_info(request):
    """Get cached contact info for a restaurant (phone, website, hours)."""
    place_id = request.GET.get("place_id")
    
    if not place_id:
        return HttpResponseBadRequest("Missing place_id parameter")
    
    # Path to cached contact info
    reviews_dir = Path(settings.BASE_DIR) / "var" / "reviews" / place_id
    contact_file = reviews_dir / "contact_info.json"
    
    contact_info = None
    
    # Check if contact info exists and is fresh (< 90 days / 3 months)
    if contact_file.exists():
        try:
            with open(contact_file, 'r', encoding='utf-8') as f:
                cached_contact = json.load(f)
                cached_time = pd.to_datetime(cached_contact.get('cached_at', '1970-01-01'))
                age_days = (pd.Timestamp.now() - cached_time).days

                if age_days < 90:  # Contact info is fresh (3 months)
                    contact_info = cached_contact
                    logger.info(f"✅ CACHE HIT: contact_info {place_id} (age: {age_days} days)")
                else:
                    logger.info(f"❌ CACHE STALE: contact_info {place_id} (age: {age_days} days), refreshing")
        except Exception as e:
            logger.warning(f"Error reading cached contact info: {e}")
    
    # Fetch contact info if not cached or stale
    if not contact_info:
        try:
            # Make sure directory exists
            reviews_dir.mkdir(parents=True, exist_ok=True)
            
            # Fetch from Google Places API
            gmaps = googlemaps.Client(key=settings.GOOGLE_API_KEY)
            resp = gmaps.place(place_id=place_id, fields=[
                "name", "formatted_phone_number", "website", 
                "opening_hours", "current_opening_hours", "rating", "user_ratings_total"
            ])
            place_data = resp["result"]
            
            # Extract and structure contact info
            contact_info = {
                "name": place_data.get("name"),
                "phone": place_data.get("formatted_phone_number"),
                "website": _clean_website_url(place_data.get("website")),
                "rating": place_data.get("rating"),
                "user_ratings_total": place_data.get("user_ratings_total"),
                "current_opening_hours": place_data.get("current_opening_hours"),
                "opening_hours": place_data.get("opening_hours"),
                "cached_at": pd.Timestamp.now().isoformat(),
                "place_id": place_id
            }
            
            # Save contact info to file
            with open(contact_file, 'w', encoding='utf-8') as f:
                json.dump(contact_info, f, indent=2, ensure_ascii=False)
            
            logger.info(f"Cached fresh contact info for {place_id}")
            
        except Exception as e:
            logger.error(f"Error fetching contact info for {place_id}: {e}")
            return JsonResponse({
                "error": "Failed to fetch contact info",
                "details": str(e)
            }, status=500)
    
    # Check if data has error
    if "error" in contact_info:
        return JsonResponse({
            "error": "Failed to fetch contact info",
            "details": contact_info["error"]
        }, status=500)
    
    # Extract today's hours from opening_hours - always return a string
    today_hours = "Hours not available"
    
    # Try current_opening_hours first (most accurate)
    current_hours = contact_info.get("current_opening_hours")
    if current_hours and isinstance(current_hours, dict):
        weekday_text = current_hours.get("weekday_text", [])
        if weekday_text:
            try:
                import datetime
                today_index = datetime.datetime.now().weekday()  # 0=Monday, 1=Tuesday, ..., 6=Sunday
                # Google's weekday_text format: [Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday]
                # So we can use today_index directly
                if 0 <= today_index < len(weekday_text):
                    today_hours = weekday_text[today_index]
                    # Remove day prefix (e.g. "Monday: 8:00 AM – 8:00 PM" -> "8:00 AM – 8:00 PM")
                    if ": " in today_hours:
                        today_hours = today_hours.split(": ", 1)[1]
            except Exception:
                pass
    
    # Fallback to regular opening_hours if current_opening_hours didn't work
    if today_hours == "Hours not available":
        opening_hours = contact_info.get("opening_hours")
        if opening_hours and isinstance(opening_hours, dict):
            weekday_text = opening_hours.get("weekday_text", [])
            if weekday_text:
                try:
                    import datetime
                    today_index = datetime.datetime.now().weekday()  # 0=Monday, 1=Tuesday, ..., 6=Sunday
                    # Google's weekday_text format: [Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday]
                    # So we can use today_index directly
                    if 0 <= today_index < len(weekday_text):
                        today_hours = weekday_text[today_index]
                        if ": " in today_hours:
                            today_hours = today_hours.split(": ", 1)[1]
                except Exception:
                    pass
    
    # Return structured contact info
    response_data = {
        "name": contact_info.get("name"),
        "phone": contact_info.get("phone"),
        "website": contact_info.get("website"),
        "today_hours": today_hours,
        "weekly_hours": get_weekly_hours(contact_info),
        "rating": contact_info.get("rating"),
        "user_ratings_total": contact_info.get("user_ratings_total"),
        "cached_at": contact_info.get("cached_at"),
        "place_id": place_id
    }
    
    return JsonResponse(response_data)


@require_GET
def restaurant_recommendations(request):
    place_id  = request.GET.get("place_id")
    ethnicity = request.GET.get("ethnicity")
    category = request.GET.get("category", "restaurant")  # Keep for search filtering but don't affect CSV naming
    
    if not place_id or not ethnicity:
        return HttpResponseBadRequest("Missing place_id or ethnicity")

    eth = (ethnicity or "").strip().lower()
    if eth not in TABS:
        return HttpResponseBadRequest("Invalid ethnicity")
        
    # Validate category for search filtering
    if category not in ["restaurant", "coffee", "bar", "brunch", "dessert"]:
        return HttpResponseBadRequest("Invalid category. Must be 'restaurant', 'coffee', 'bar', 'brunch', or 'dessert'")

    # Track this place_id for nightly Yelp processing
    try:
        add_place_id_to_queue(place_id)
    except Exception as e:
        logger.warning(f"Failed to add place_id {place_id} to Yelp queue: {e}")

    bypass = (request.GET.get("nocache") == "1")

    # Always use unified dish_mentions.csv regardless of category
    csv_path: Path = category_csv_path(place_id, category)  # This now always returns dish_mentions.csv
    place_root: Path = csv_path.parent

    # --- Cold-miss guard to avoid infinite spinner & ensure a response every time
    COLD_MAX_WAIT_S = 60
    cold_sentinel = place_root / ".first_cold_miss.ts"

    def _age_seconds(p: Path):
        try:
            return time.time() - p.stat().st_mtime
        except Exception:
            return None

    # If there is no CSV yet ⇒ cold miss
    if not csv_path.exists():
        place_root.mkdir(parents=True, exist_ok=True)

        if not cold_sentinel.exists():
            # First cold miss for this place: touch sentinel, enqueue once, and return partial
            try:
                cold_sentinel.touch(exist_ok=True)
            except Exception:
                pass
            try:
                enq = ensure_csv_async(place_id, fast=False, category=category)
                logger.info("COLD MISS %s — launched FULL (enqueued=%s)", place_id, enq)
            except Exception as e:
                logger.exception("COLD MISS %s — enqueue failed: %s", place_id, e)
            # Return empty result indicating we're collecting data for the first time
            # Frontend should hide dishes section entirely (not show "No dishes" or spinner)
            return JsonResponse({"dishes": [], "partial": True, "isCollecting": True})

        # Subsequent cold misses: decide whether to keep polling or stop
        age = _age_seconds(cold_sentinel)
        if age is not None and age <= COLD_MAX_WAIT_S:
            logger.info("COLD MISS %s (%.0fs) — keep polling", place_id, age)
            try:
                ensure_csv_async(place_id, fast=False, category=category)  # safe: de-duped inside
            except Exception:
                pass
            # Still collecting - hide dishes section
            return JsonResponse({"dishes": [], "partial": True, "isCollecting": True})
        else:
            logger.info("COLD MISS %s exceeded wait window — stop polling", place_id)
            return JsonResponse({"dishes": [], "partial": False})

    # CSV exists now → clear the cold sentinel
    try:
        if cold_sentinel.exists():
            cold_sentinel.unlink()
    except Exception:
        pass

    # Decide if current snapshot is partial (not enough reviews or stale)
    review_count = _count_reviews(place_id)
    csv_is_stale = is_stale(csv_path)
    partial_flag = (review_count < FULL_TARGET) or csv_is_stale

    # If partial → enqueue one FULL backfill (de-duped inside ensure_csv_async)
    if partial_flag:
        try:
            ensure_csv_async(place_id, fast=False, category=category)
        except Exception:
            pass

    # Quick app cache keyed by CSV mtime
    try:
        mtime = int(csv_path.stat().st_mtime)
    except Exception:
        mtime = 0

    cache_key = f"rr:{place_id}:{eth}:{category}:{mtime}"
    if not bypass:
        cached = cache.get(cache_key)
        if cached is not None:
            return JsonResponse(cached)

    # ---------- Read current CSV snapshot ----------
    try:
        df = pd.read_csv(csv_path, dtype={"ethnicity_ui": "string", "dish": "string", "price": "string", "description": "string"})
    except Exception:
        return JsonResponse({"dishes": [], "partial": True})

    # ---------- Image map (shared) ----------
    img_map_path = csv_path.parent / "dish_images.json"
    img_map = {}
    if img_map_path.exists():
        try:
            img_map = json.loads(img_map_path.read_text(encoding="utf-8"))
        except Exception:
            img_map = {}

    # Helper to turn rows into API payload
    def _rows_to_payload(rows: pd.DataFrame):
        out = []
        for _, r in rows.iterrows():
            name = str(r["dish"])  # always use "dish" column name
            people = int(r.get("unique_authors") or 0)
            mentions = int(r.get("mentions") or 0)
            from_rec = bool(r.get("from_recommended", False))
            img_info = img_map.get(name) or {}

            # Build dish object
            dish_obj = {
                "name": name,
                "people": people,
                "mentions": mentions,
                "from_recommended": from_rec,
                "image_url": img_info.get("image_url"),
                "caption": img_info.get("caption"),
            }

            # Add price if available (from menu matching)
            if pd.notna(r.get("price")) and r.get("price"):
                dish_obj["price"] = str(r["price"])

            # Add description if available (from menu matching)
            if pd.notna(r.get("description")) and r.get("description"):
                dish_obj["description"] = str(r["description"])

            out.append(dish_obj)

        # Always use "dishes" key - it's just "recommended items"
        return {"dishes": out, "partial": partial_flag}

    # =========================
    # Popular tab (this is the new bit)
    # =========================
    if eth == "popular":
        # Prefer FULL dataset when we're no longer partial (more accurate).
        if not partial_flag:
            try:
                cols = df.columns
                agg = {"mentions": "sum", "unique_authors": "sum"}
                if "from_recommended" in cols:
                    agg["from_recommended"] = "max"
                if "price" in cols:
                    agg["price"] = "first"  # Take first non-null price
                if "description" in cols:
                    agg["description"] = "first"  # Take first non-null description
                pop = (df.groupby("dish", as_index=False)
                       .agg(agg)
                       .sort_values(["mentions", "unique_authors", "dish"],
                                    ascending=[False, False, True])
                       .head(5))
                payload = _rows_to_payload(pop)
                cache.set(cache_key, payload, timeout=180)
                return JsonResponse(payload)
            except Exception:
                # fall through to top5 fast path
                pass

        # While partial → use dish_mentions_top5.csv for fastest response.
        top5_path = csv_path.parent / "dish_mentions_top5.csv"
        if top5_path.exists():
            try:
                top5 = pd.read_csv(top5_path, dtype={"dish": "string", "price": "string", "description": "string"})
                if not top5.empty:
                    cols = top5.columns
                    agg = {"mentions": "sum", "unique_authors": "sum"}
                    if "from_recommended" in cols:
                        agg["from_recommended"] = "max"
                    if "price" in cols:
                        agg["price"] = "first"  # Take first non-null price
                    if "description" in cols:
                        agg["description"] = "first"  # Take first non-null description
                    pop = (top5.groupby("dish", as_index=False)
                           .agg(agg)
                           .sort_values(["mentions", "unique_authors", "dish"],
                                        ascending=[False, False, True])
                           .head(5))
                    payload = _rows_to_payload(pop)
                    cache.set(cache_key, payload, timeout=120)
                    return JsonResponse(payload)
            except Exception:
                pass

        # Fallback: compute Popular quickly from whatever we have in df
        try:
            cols = df.columns
            agg = {"mentions": "sum", "unique_authors": "sum"}
            if "from_recommended" in cols:
                agg["from_recommended"] = "max"
            if "price" in cols:
                agg["price"] = "first"  # Take first non-null price
            if "description" in cols:
                agg["description"] = "first"  # Take first non-null description
            pop = (df.groupby("dish", as_index=False)
                   .agg(agg)
                   .sort_values(["mentions", "unique_authors", "dish"],
                                ascending=[False, False, True])
                   .head(5))
            payload = _rows_to_payload(pop)
            cache.set(cache_key, payload, timeout=120)
            return JsonResponse(payload)
        except Exception:
            return JsonResponse({"dishes": [], "partial": partial_flag})

    # =========================
    # Per-ethnicity (existing behavior)
    # =========================
    if df.empty:
        payload = {"dishes": [], "partial": True}
        cache.set(cache_key, payload, timeout=120)
        return JsonResponse(payload)

    sub = df[df["ethnicity_ui"].str.lower() == eth]
    if sub.empty:
        payload = {"dishes": [], "partial": partial_flag}
        cache.set(cache_key, payload, timeout=120)
        return JsonResponse(payload)

    sub = sub.sort_values(
        ["mentions", "unique_authors", "dish"],
        ascending=[False, False, True]
    ).head(5)

    payload = _rows_to_payload(sub)
    cache.set(cache_key, payload, timeout=180)
    return JsonResponse(payload)


# ===================================
# AI ORDERING SYSTEM APIs
# ===================================

def require_firebase_auth(view_func):
    """Decorator to require Firebase authentication"""
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        # Get the authorization header
        auth_header = request.META.get('HTTP_AUTHORIZATION')
        if not auth_header or not auth_header.startswith('Bearer '):
            return JsonResponse({'error': 'Missing or invalid authorization header'}, status=401)

        # Extract the token
        token = auth_header.split(' ')[1]

        try:
            # Verify the Firebase token
            decoded_token = firebase_auth.verify_id_token(token)
            firebase_uid = decoded_token['uid']

            # Get or create Django user
            try:
                user = User.objects.get(username=firebase_uid)
            except User.DoesNotExist:
                # Create user if doesn't exist
                user = User.objects.create_user(
                    username=firebase_uid,
                    email=decoded_token.get('email', ''),
                    first_name=decoded_token.get('name', '').split(' ')[0] if decoded_token.get('name') else ''
                )

            # Add user to request
            request.user = user
            return view_func(request, *args, **kwargs)

        except Exception as e:
            logger.error(f"Firebase auth error: {e}")
            return JsonResponse({'error': 'Invalid token'}, status=401)

    return _wrapped_view


def _serialize_preferences(preferences):
    """Convert all preference values to strings for iOS compatibility"""
    if not preferences:
        return {}
    return {k: str(v) for k, v in preferences.items()}

@csrf_exempt
@require_firebase_auth
def user_profile_api(request):
    """
    GET: Get user profile with addresses and payment methods
    POST: Update user profile (phone, preferences)
    """
    if request.method == 'GET':
        # Get or create user profile
        profile, created = UserProfile.objects.get_or_create(
            user=request.user,
            defaults={'phone': '', 'preferences': {}}
        )

        # Get addresses and payment methods
        addresses = request.user.addresses.all()
        payment_methods = request.user.payment_methods.all()

        return JsonResponse({
            'success': True,
            'profile': {
                'phone': profile.phone,
                'first_name': profile.first_name,
                'last_name': profile.last_name,
                'email': profile.email,
                'preferences': _serialize_preferences(profile.preferences),
                'has_complete_profile': profile.has_complete_profile,
                'created_at': profile.created_at.isoformat(),
                'updated_at': profile.updated_at.isoformat()
            },
            'addresses': [
                {
                    'id': addr.id,
                    'name': addr.name,
                    'street_address': addr.street_address,
                    'city': addr.city,
                    'state': addr.state,
                    'zip_code': addr.zip_code,
                    'is_default': addr.is_default,
                    'created_at': addr.created_at.isoformat()
                } for addr in addresses
            ],
            'payment_methods': [
                {
                    'id': pm.id,
                    'type': pm.type,
                    'last_four': pm.last_four,
                    'is_default': pm.is_default,
                    'created_at': pm.created_at.isoformat()
                } for pm in payment_methods
            ]
        })

    elif request.method == 'POST':
        try:
            data = json.loads(request.body)

            # Get or create user profile
            profile, created = UserProfile.objects.get_or_create(
                user=request.user,
                defaults={'phone': '', 'preferences': {}}
            )

            # Update fields if provided
            if 'phone' in data:
                profile.phone = data['phone']
            if 'first_name' in data:
                profile.first_name = data['first_name']
            if 'last_name' in data:
                profile.last_name = data['last_name']
            if 'email' in data:
                profile.email = data['email']
            if 'preferences' in data:
                profile.preferences = data['preferences']

            profile.save()

            # Get updated addresses and payment methods
            addresses = request.user.addresses.all()
            payment_methods = request.user.payment_methods.all()

            return JsonResponse({
                'success': True,
                'message': 'Profile updated successfully',
                'profile': {
                    'phone': profile.phone,
                    'first_name': profile.first_name,
                    'last_name': profile.last_name,
                    'email': profile.email,
                    'preferences': _serialize_preferences(profile.preferences),
                    'has_complete_profile': profile.has_complete_profile,
                    'created_at': profile.created_at.isoformat(),
                    'updated_at': profile.updated_at.isoformat()
                },
                'addresses': [
                    {
                        'id': addr.id,
                        'name': addr.name,
                        'street_address': addr.street_address,
                        'city': addr.city,
                        'state': addr.state,
                        'zip_code': addr.zip_code,
                        'is_default': addr.is_default,
                        'created_at': addr.created_at.isoformat()
                    } for addr in addresses
                ],
                'payment_methods': [
                    {
                        'id': pm.id,
                        'type': pm.type,
                        'last_four': pm.last_four,
                        'is_default': pm.is_default,
                        'created_at': pm.created_at.isoformat()
                    } for pm in payment_methods
                ]
            })

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
        except Exception as e:
            logger.error(f"Profile update error: {e}")
            return JsonResponse({'success': False, 'error': 'Server error'}, status=500)


@csrf_exempt
@require_firebase_auth
def delivery_addresses_api(request):
    """
    GET: Get all user addresses
    POST: Add new address
    """
    if request.method == 'GET':
        addresses = request.user.addresses.all()
        return JsonResponse({
            'success': True,
            'addresses': [
                {
                    'id': addr.id,
                    'name': addr.name,
                    'street_address': addr.street_address,
                    'city': addr.city,
                    'state': addr.state,
                    'zip_code': addr.zip_code,
                    'is_default': addr.is_default,
                    'created_at': addr.created_at.isoformat()
                } for addr in addresses
            ]
        })

    elif request.method == 'POST':
        try:
            data = json.loads(request.body)

            # Validate required fields
            required_fields = ['name', 'street_address', 'city', 'state', 'zip_code']
            for field in required_fields:
                if field not in data or not data[field].strip():
                    return JsonResponse({
                        'success': False,
                        'error': f'Missing required field: {field}'
                    }, status=400)

            # Check for duplicates
            # 1. Check for same name
            if DeliveryAddress.objects.filter(user=request.user, name__iexact=data['name'].strip()).exists():
                return JsonResponse({
                    'success': False,
                    'error': f'You already have an address named "{data["name"].strip()}". Please use a different name.'
                }, status=400)

            # 2. Check for same address (street + city + state + zip)
            existing_address = DeliveryAddress.objects.filter(
                user=request.user,
                street_address__iexact=data['street_address'].strip(),
                city__iexact=data['city'].strip(),
                state__iexact=data['state'].strip(),
                zip_code=data['zip_code'].strip()
            ).first()

            if existing_address:
                return JsonResponse({
                    'success': False,
                    'error': f'This address already exists as "{existing_address.name}". Please use a different address.'
                }, status=400)

            # Create new address
            address = DeliveryAddress.objects.create(
                user=request.user,
                name=data['name'].strip(),
                street_address=data['street_address'].strip(),
                city=data['city'].strip(),
                state=data['state'].strip(),
                zip_code=data['zip_code'].strip(),
                is_default=data.get('is_default', False)
            )

            return JsonResponse({
                'success': True,
                'message': 'Address added successfully',
                'address': {
                    'id': address.id,
                    'name': address.name,
                    'street_address': address.street_address,
                    'city': address.city,
                    'state': address.state,
                    'zip_code': address.zip_code,
                    'is_default': address.is_default,
                    'created_at': address.created_at.isoformat()
                }
            })

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
        except Exception as e:
            logger.error(f"Address creation error: {e}")
            return JsonResponse({'success': False, 'error': 'Server error'}, status=500)


@csrf_exempt
@require_firebase_auth
def delivery_address_detail_api(request, address_id):
    """
    PUT: Update address
    DELETE: Delete address
    """
    try:
        address = request.user.addresses.get(id=address_id)
    except DeliveryAddress.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Address not found'}, status=404)

    if request.method == 'PUT':
        try:
            data = json.loads(request.body)

            # Update fields if provided
            if 'name' in data:
                address.name = data['name'].strip()
            if 'street_address' in data:
                address.street_address = data['street_address'].strip()
            if 'city' in data:
                address.city = data['city'].strip()
            if 'state' in data:
                address.state = data['state'].strip()
            if 'zip_code' in data:
                address.zip_code = data['zip_code'].strip()
            if 'is_default' in data:
                address.is_default = data['is_default']

            address.save()

            return JsonResponse({
                'success': True,
                'message': 'Address updated successfully',
                'address': {
                    'id': address.id,
                    'name': address.name,
                    'street_address': address.street_address,
                    'city': address.city,
                    'state': address.state,
                    'zip_code': address.zip_code,
                    'is_default': address.is_default
                }
            })

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
        except Exception as e:
            logger.error(f"Address update error: {e}")
            return JsonResponse({'success': False, 'error': 'Server error'}, status=500)

    elif request.method == 'DELETE':
        address.delete()
        return JsonResponse({
            'success': True,
            'message': 'Address deleted successfully'
        })


@csrf_exempt
@require_firebase_auth
def payment_methods_api(request):
    """
    GET: Get all user payment methods
    POST: Add new payment method
    """
    if request.method == 'GET':
        payment_methods = request.user.payment_methods.all()
        return JsonResponse({
            'success': True,
            'payment_methods': [
                {
                    'id': pm.id,
                    'type': pm.type,
                    'last_four': pm.last_four,
                    'is_default': pm.is_default,
                    'created_at': pm.created_at.isoformat()
                } for pm in payment_methods
            ]
        })

    elif request.method == 'POST':
        try:
            data = json.loads(request.body)

            # Validate required fields
            if 'type' not in data:
                return JsonResponse({
                    'success': False,
                    'error': 'Missing required field: type'
                }, status=400)

            # Validate payment type
            valid_types = ['apple_pay', 'google_pay', 'stripe_card']
            if data['type'] not in valid_types:
                return JsonResponse({
                    'success': False,
                    'error': f'Invalid payment type. Must be one of: {valid_types}'
                }, status=400)

            # Create new payment method
            payment_method = PaymentMethod.objects.create(
                user=request.user,
                type=data['type'],
                is_default=data.get('is_default', False),
                stripe_payment_method_id=data.get('stripe_payment_method_id', ''),
                last_four=data.get('last_four', '')
            )

            return JsonResponse({
                'success': True,
                'message': 'Payment method added successfully',
                'payment_method': {
                    'id': payment_method.id,
                    'type': payment_method.type,
                    'last_four': payment_method.last_four,
                    'is_default': payment_method.is_default,
                    'created_at': payment_method.created_at.isoformat()
                }
            })

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
        except Exception as e:
            logger.error(f"Payment method creation error: {e}")
            return JsonResponse({'success': False, 'error': 'Server error'}, status=500)


@csrf_exempt
@require_firebase_auth
def payment_method_detail_api(request, payment_id):
    """
    PUT: Update payment method
    DELETE: Delete payment method
    """
    try:
        payment_method = request.user.payment_methods.get(id=payment_id)
    except PaymentMethod.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Payment method not found'}, status=404)

    if request.method == 'PUT':
        try:
            data = json.loads(request.body)

            # Update is_default if provided
            if 'is_default' in data:
                payment_method.is_default = data['is_default']
                payment_method.save()

            return JsonResponse({
                'success': True,
                'message': 'Payment method updated successfully',
                'payment_method': {
                    'id': payment_method.id,
                    'type': payment_method.type,
                    'last_four': payment_method.last_four,
                    'is_default': payment_method.is_default
                }
            })

        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
        except Exception as e:
            logger.error(f"Payment method update error: {e}")
            return JsonResponse({'success': False, 'error': 'Server error'}, status=500)

    elif request.method == 'DELETE':
        payment_method.delete()
        return JsonResponse({
            'success': True,
            'message': 'Payment method deleted successfully'
        })


@csrf_exempt
@require_firebase_auth
def validate_user_profile_api(request):
    """
    GET: Check if user has complete profile for AI ordering
    """
    if request.method == 'GET':
        # Get or create user profile
        profile, created = UserProfile.objects.get_or_create(
            user=request.user,
            defaults={'phone': '', 'preferences': {}}
        )

        # Check what's missing
        missing_items = []

        if not request.user.addresses.filter(is_default=True).exists():
            missing_items.append('delivery_address')

        if not request.user.payment_methods.filter(is_default=True).exists():
            missing_items.append('payment_method')

        is_complete = len(missing_items) == 0

        logger.info(f"Profile validation - User: {request.user.username}, Complete: {is_complete}, Missing: {missing_items}")

        return JsonResponse({
            'success': True,
            'is_complete': is_complete,
            'missing_items': missing_items,
            'profile': {
                'has_addresses': request.user.addresses.exists(),
                'has_payment_methods': request.user.payment_methods.exists(),
                'default_address_id': getattr(request.user.addresses.filter(is_default=True).first(), 'id', None),
                'default_payment_id': getattr(request.user.payment_methods.filter(is_default=True).first(), 'id', None)
            }
        })


@csrf_exempt
def address_autocomplete_api(request):
    """
    Proxy for Google Places Autocomplete API for address suggestions
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET method allowed'}, status=405)

    input_text = request.GET.get('input', '').strip()
    if not input_text or len(input_text) < 3:
        return JsonResponse({'suggestions': []})

    # Check cache first (90 days / 3 months)
    # Normalize input for consistent caching
    cache_key = f"autocomplete:{input_text.lower()}"
    cached_suggestions = cache.get(cache_key)

    if cached_suggestions is not None:
        logger.info(f"✅ CACHE HIT: autocomplete '{input_text[:20]}...'")
        return JsonResponse({'suggestions': cached_suggestions})

    logger.info(f"❌ CACHE MISS: autocomplete '{input_text[:20]}...' - calling Google Places API ($0.00283)")

    try:
        # Initialize Google Maps client
        gmaps = googlemaps.Client(key=settings.GOOGLE_API_KEY)

        # Get autocomplete predictions
        predictions = gmaps.places_autocomplete(
            input_text=input_text,
            types=['address'],
            components={'country': 'us'}
        )

        # Format response for iOS app
        suggestions = []
        for prediction in predictions:
            suggestion = {
                'place_id': prediction['place_id'],
                'description': prediction['description'],
                'main_text': prediction['structured_formatting']['main_text'],
                'secondary_text': prediction['structured_formatting'].get('secondary_text', '')
            }
            suggestions.append(suggestion)

        # Cache for 90 days (3 months) - addresses don't change often
        cache.set(cache_key, suggestions, timeout=60 * 60 * 24 * 90)
        logger.info(f"💾 Cached autocomplete for '{input_text[:20]}...' (90 days)")

        return JsonResponse({'suggestions': suggestions})

    except Exception as e:
        logger.error(f"Address autocomplete error: {str(e)}")
        return JsonResponse({'error': 'Failed to fetch address suggestions'}, status=500)


@csrf_exempt
def address_details_api(request):
    """
    Proxy for Google Places Details API to get structured address components
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET method allowed'}, status=405)

    place_id = request.GET.get('place_id', '').strip()
    if not place_id:
        return JsonResponse({'error': 'place_id parameter required'}, status=400)

    # Check cache first (90 days / 3 months)
    cache_key = f"address_details:{place_id}"
    cached_details = cache.get(cache_key)

    if cached_details is not None:
        logger.info(f"✅ CACHE HIT: address_details {place_id}")
        return JsonResponse({'details': cached_details})

    logger.info(f"❌ CACHE MISS: address_details {place_id} - calling Google Places API ($0.017)")

    try:
        # Initialize Google Maps client
        gmaps = googlemaps.Client(key=settings.GOOGLE_API_KEY)

        # Get place details
        place_details = gmaps.place(
            place_id=place_id,
            fields=['formatted_address', 'address_component']
        )

        result = place_details.get('result', {})
        address_components = result.get('address_components', [])

        # Parse address components
        details = {
            'street_number': '',
            'street_name': '',
            'city': '',
            'state': '',
            'zip_code': '',
            'formatted_address': result.get('formatted_address', '')
        }

        for component in address_components:
            types = component.get('types', [])
            long_name = component.get('long_name', '')
            short_name = component.get('short_name', '')

            if 'street_number' in types:
                details['street_number'] = long_name
            elif 'route' in types:
                details['street_name'] = long_name
            elif 'locality' in types:
                details['city'] = long_name
            elif 'administrative_area_level_1' in types:
                details['state'] = short_name
            elif 'postal_code' in types:
                details['zip_code'] = long_name

        # Cache for 90 days (3 months)
        cache.set(cache_key, details, timeout=60 * 60 * 24 * 90)
        logger.info(f"💾 Cached address_details for {place_id} (90 days)")

        return JsonResponse({'details': details})

    except Exception as e:
        logger.error(f"Address details error: {str(e)}")
        return JsonResponse({'error': 'Failed to fetch address details'}, status=500)


# ===================================
# MENU STRUCTURE AND ORDERING CAPABILITY APIs
# ===================================

@require_GET
def restaurant_menu_structure_api(request):
    """
    Get cached menu structure for a restaurant

    Query parameters:
    - place_id: Google Places ID (required)
    - website_url: Restaurant website URL (optional)
    """
    place_id = request.GET.get("place_id")
    website_url = request.GET.get("website_url")

    if not place_id:
        return HttpResponseBadRequest("Missing place_id parameter")

    try:
        # Get restaurant name from contact info cache or Google Places
        restaurant_name = _get_restaurant_name(place_id)

        from .utils.menu_structure_cache import get_restaurant_menu
        menu_structure = get_restaurant_menu(place_id, restaurant_name, website_url)

        if menu_structure:
            # Use cached platform (already detected during scraping)
            platform = menu_structure.ordering_platform or "custom"

            # Get website from contact_info.json if available
            website_url = None
            try:
                reviews_dir = Path(settings.BASE_DIR) / "var" / "reviews" / place_id
                contact_file = reviews_dir / "contact_info.json"
                if contact_file.exists():
                    with open(contact_file, 'r', encoding='utf-8') as f:
                        contact_info = json.load(f)
                        website_url = contact_info.get('website')
            except Exception:
                pass

            # Convert to API response format
            response_data = {
                "success": menu_structure.success,  # Use actual success flag from menu structure
                "restaurant_id": menu_structure.restaurant_id,
                "restaurant_name": menu_structure.restaurant_name,
                "categories": menu_structure.categories,
                "items": [
                    {
                        "name": item.name,
                        "description": item.description,
                        "category": item.category,
                        "dietary_info": item.dietary_info,
                        "customizations": item.customizations,
                        "image_url": item.image_url,
                        "price": item.price  # Include price in API response
                    }
                    for item in menu_structure.items
                ],
                "supports_online_ordering": menu_structure.supports_online_ordering,
                "ordering_url_pickup": menu_structure.ordering_url_pickup,
                "ordering_url_delivery": menu_structure.ordering_url_delivery,
                "ordering_platform": platform,
                "phone_number": menu_structure.phone_number,
                "cached_at": menu_structure.cached_at.isoformat(),
                "is_fresh": not menu_structure.is_stale(),
                "website": website_url  # Add website URL from contact_info
            }
            return JsonResponse(response_data)
        else:
            # Menu not cached yet - try to provide website or Google Maps fallback URL
            fallback_url = None
            try:
                reviews_dir = Path(settings.BASE_DIR) / "var" / "reviews" / place_id
                contact_file = reviews_dir / "contact_info.json"
                if contact_file.exists():
                    with open(contact_file, 'r', encoding='utf-8') as f:
                        contact_info = json.load(f)
                        # Priority: website > place_url (Google Maps)
                        fallback_url = contact_info.get('website') or contact_info.get('place_url')
            except Exception:
                pass

            response = {
                "success": False,
                "error": "Menu structure not available",
                "message": "Restaurant may not have an online menu or website"
            }

            # Add website or Google Maps URL as fallback for fresh restaurants
            if fallback_url:
                response["google_maps_url"] = fallback_url

            return JsonResponse(response)

    except Exception as e:
        logger.error(f"Error fetching menu structure for {place_id}: {e}")
        return JsonResponse({
            "success": False,
            "error": "Failed to fetch menu structure",
            "details": str(e)
        }, status=500)


@require_GET
def restaurant_ordering_capability_api(request):
    """
    Get ordering capabilities for a restaurant

    Query parameters:
    - place_id: Google Places ID (required)
    - website_url: Restaurant website URL (optional)
    """
    place_id = request.GET.get("place_id")
    website_url = request.GET.get("website_url")

    if not place_id:
        return HttpResponseBadRequest("Missing place_id parameter")

    try:
        # Get restaurant name from contact info cache or Google Places
        restaurant_name = _get_restaurant_name(place_id)

        from .utils.menu_structure_cache import get_restaurant_ordering_capability
        capability = get_restaurant_ordering_capability(place_id, restaurant_name, website_url)

        if capability:
            response_data = {
                "success": True,
                "restaurant_id": capability.restaurant_id,
                "supports_delivery": capability.supports_delivery,
                "supports_pickup": capability.supports_pickup,
                "has_website_ordering": capability.has_website_ordering,
                "delivery_platforms": capability.delivery_platforms,
                "website_url": capability.website_url,
                "phone_number": capability.phone_number,
                "cached_at": capability.cached_at.isoformat(),
                "is_fresh": not capability.is_stale()
            }
            return JsonResponse(response_data)
        else:
            return JsonResponse({
                "success": False,
                "error": "Ordering capability not available"
            })

    except Exception as e:
        logger.error(f"Error fetching ordering capability for {place_id}: {e}")
        return JsonResponse({
            "success": False,
            "error": "Failed to fetch ordering capability",
            "details": str(e)
        }, status=500)


def _get_restaurant_name(place_id: str) -> str:
    """Helper to get restaurant name from cache or Google Places"""
    try:
        # First try to get from contact info cache
        reviews_dir = Path(settings.BASE_DIR) / "var" / "reviews" / place_id
        contact_file = reviews_dir / "contact_info.json"

        if contact_file.exists():
            with open(contact_file, 'r', encoding='utf-8') as f:
                contact_info = json.load(f)
                name = contact_info.get('name')
                if name:
                    return name

        # Fallback to Google Places API
        gmaps = googlemaps.Client(key=settings.GOOGLE_API_KEY)
        resp = gmaps.place(place_id=place_id, fields=["name"])
        return resp["result"].get("name", f"Restaurant_{place_id}")

    except Exception as e:
        logger.error(f"Error getting restaurant name for {place_id}: {e}")
        return f"Restaurant_{place_id}"


# ===================================
# AI ORDERING SYSTEM API ENDPOINTS
# ===================================

@csrf_exempt
@require_firebase_auth
def ai_order_api(request):
    """
    POST: Initiate AI order with selected dishes
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)

    try:
        data = json.loads(request.body)
        logger.info(f"AI Order request data: {data}")

        # Validate required fields
        required_fields = ['restaurant_id', 'restaurant_name', 'restaurant_address', 'selected_dishes']
        for field in required_fields:
            if field not in data:
                logger.error(f"AI Order missing field: {field}")
                return JsonResponse({
                    'success': False,
                    'error': f'Missing required field: {field}'
                }, status=400)

        # Validate selected dishes
        selected_dishes = data.get('selected_dishes', [])
        if not selected_dishes:
            logger.error("AI Order no dishes selected")
            return JsonResponse({
                'success': False,
                'error': 'At least one dish must be selected'
            }, status=400)

        # Get or create user profile
        profile, created = UserProfile.objects.get_or_create(
            user=request.user,
            defaults={'phone': '', 'preferences': {}}
        )

        # Check if user has addresses and payment methods with defaults
        has_addresses = request.user.addresses.exists()
        has_payment_methods = request.user.payment_methods.exists()
        has_default_address = request.user.addresses.filter(is_default=True).exists()
        has_default_payment = request.user.payment_methods.filter(is_default=True).exists()

        logger.info(f"User profile check - Addresses: {has_addresses}, Payment methods: {has_payment_methods}, Default address: {has_default_address}, Default payment: {has_default_payment}")

        if not has_addresses:
            logger.error("AI Order missing addresses")
            return JsonResponse({
                'success': False,
                'error': 'At least one delivery address required'
            }, status=400)

        if not has_payment_methods:
            logger.error("AI Order missing payment methods")
            return JsonResponse({
                'success': False,
                'error': 'At least one payment method required'
            }, status=400)

        if not has_default_address:
            logger.error("AI Order missing default address")
            return JsonResponse({
                'success': False,
                'error': 'Please select a default delivery address'
            }, status=400)

        if not has_default_payment:
            logger.error("AI Order missing default payment method")
            return JsonResponse({
                'success': False,
                'error': 'Please select a default payment method'
            }, status=400)

        # Get the selected defaults
        default_address = request.user.addresses.filter(is_default=True).first()
        default_payment = request.user.payment_methods.filter(is_default=True).first()

        logger.info(f"Using default address: {default_address.name}, payment: {default_payment.type}")

        # Create AI order record using the existing model structure
        ai_order = AIOrder.objects.create(
            user=request.user,
            restaurant_name=data['restaurant_name'],
            restaurant_place_id=data['restaurant_id'],  # Map restaurant_id to restaurant_place_id
            dishes=[dish['name'] for dish in data['selected_dishes']],  # Extract dish names
            delivery_address={
                'name': default_address.name,
                'street_address': default_address.street_address,
                'city': default_address.city,
                'state': default_address.state,
                'zip_code': default_address.zip_code
            } if default_address else {},
            payment_method={
                'type': default_payment.type,
                'last_four': default_payment.last_four
            } if default_payment else {},
            status='processing'  # Start with processing status
        )

        logger.info(f"AI Order created successfully - Order ID: {ai_order.id}, User: {request.user.username}")

        # Start OpenAI assistant processing in background
        try:
            assistant_result = start_ai_assistant_order(ai_order, profile.phone)

            # Update order with assistant info
            if assistant_result.get('success'):
                ai_order.assistant_id = assistant_result.get('assistant_id')
                ai_order.thread_id = assistant_result.get('thread_id')
                ai_order.save()

                return JsonResponse({
                    'success': True,
                    'order_id': str(ai_order.id),
                    'message': 'AI order initiated successfully',
                    'status': 'processing',
                    'assistant_id': assistant_result.get('assistant_id'),
                    'thread_id': assistant_result.get('thread_id')
                })
            else:
                # Assistant creation failed, update order status
                ai_order.status = 'failed'
                ai_order.save()
                return JsonResponse({
                    'success': False,
                    'error': f"Failed to initialize AI assistant: {assistant_result.get('error', 'Unknown error')}"
                }, status=500)

        except Exception as e:
            logger.error(f"AI assistant initialization error: {e}")
            ai_order.status = 'failed'
            ai_order.save()
            return JsonResponse({'success': False, 'error': 'Failed to initialize AI assistant'}, status=500)

    except json.JSONDecodeError as e:
        logger.error(f"AI Order JSON decode error: {e}")
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"AI order creation error: {e}")
        return JsonResponse({'success': False, 'error': 'Server error'}, status=500)


def start_ai_assistant_order(ai_order, user_phone):
    """
    Initialize AI order processing with delivery platforms (DoorDash/Uber Eats)
    """
    try:
        # Create OpenAI thread for decision making and communication
        thread = client.beta.threads.create()

        # Create context for the AI assistant
        dishes_list = "\n".join([f"- {dish}" for dish in ai_order.dishes])
        address = ai_order.delivery_address
        payment = ai_order.payment_method

        # Get restaurant address for platform search
        restaurant_address = get_restaurant_address(ai_order.restaurant_place_id)

        order_prompt = f"""I need to place a food delivery order for {ai_order.restaurant_name}. Here are the details:

RESTAURANT: {ai_order.restaurant_name}
RESTAURANT ADDRESS: {restaurant_address}

SELECTED DISHES:
{dishes_list}

DELIVERY ADDRESS:
{address.get('name', 'N/A')}
{address.get('street_address', 'N/A')}
{address.get('city', 'N/A')}, {address.get('state', 'N/A')} {address.get('zip_code', 'N/A')}

PAYMENT METHOD:
{payment.get('type', 'N/A')} ending in {payment.get('last_four', 'N/A')}

CUSTOMER PHONE:
{user_phone}

Please help me:
1. Find this restaurant on delivery platforms (DoorDash, Uber Eats)
2. Check which dishes are available
3. Choose the best platform based on availability, price, and delivery time
4. Place the order with the selected dishes

Start by searching for the restaurant on delivery platforms."""

        # Add the initial message to the thread
        client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=order_prompt
        )

        # Get delivery platform assistant
        assistant_id = get_or_create_delivery_assistant()

        # Start the assistant run
        run = client.beta.threads.runs.create(
            thread_id=thread.id,
            assistant_id=assistant_id
        )

        # Update order status
        ai_order.status = 'processing'
        ai_order.save()

        logger.info(f"Delivery AI Assistant started for order {ai_order.id}: thread={thread.id}, run={run.id}")

        return {
            'success': True,
            'assistant_id': assistant_id,
            'thread_id': thread.id,
            'run_id': run.id
        }

    except Exception as e:
        logger.error(f"Error starting delivery AI assistant: {e}")
        return {
            'success': False,
            'error': str(e)
        }


def get_restaurant_address(place_id):
    """
    Get restaurant address - use cache to avoid duplicate API calls
    """
    # Try contact_info cache first
    reviews_dir = Path(settings.BASE_DIR) / "var" / "reviews" / place_id
    contact_file = reviews_dir / "contact_info.json"

    if contact_file.exists():
        try:
            with open(contact_file, 'r', encoding='utf-8') as f:
                contact_info = json.load(f)
                # Return formatted_address if available
                if 'formatted_address' in contact_info:
                    logger.info(f"✅ Using cached address for {place_id}")
                    return contact_info['formatted_address']
                # Fallback to name
                if 'name' in contact_info:
                    logger.info(f"✅ Using cached name as address for {place_id}")
                    return contact_info['name']
        except Exception as e:
            logger.warning(f"Error reading contact_info for address: {e}")

    # Cache doesn't exist - call Google
    logger.info(f"❌ CACHE MISS: address for {place_id} - calling Google Places API ($0.017)")

    try:
        gmaps = googlemaps.Client(key=settings.GOOGLE_API_KEY)
        resp = gmaps.place(place_id=place_id, fields=["formatted_address"])
        return resp["result"].get("formatted_address", f"Restaurant_{place_id}")
    except Exception as e:
        logger.error(f"Error fetching restaurant address: {e}")
        return f"Restaurant_{place_id}"


def get_or_create_delivery_assistant():
    """
    Get existing delivery assistant or create a new one
    """
    try:
        # Create delivery platform assistant
        assistant = client.beta.assistants.create(
            name="Food Delivery Assistant",
            instructions="""You are an AI assistant that specializes in placing food orders through delivery platforms like DoorDash and Uber Eats.

Your capabilities include:
- Searching for restaurants on delivery platforms
- Finding matching dishes and menu items
- Comparing prices, availability, and delivery times across platforms
- Selecting the best platform for each order
- Placing orders through platform APIs

When processing an order:
1. Search for the restaurant on DoorDash and Uber Eats
2. Find the requested dishes on each platform's menu
3. Compare total costs including fees and delivery charges
4. Check estimated delivery times
5. Select the best platform (prioritize: dish availability, then cost, then speed)
6. Place the order using the selected platform's API
7. Provide order confirmation details

Be efficient and accurate. If dishes aren't available, suggest similar alternatives. Always confirm the final order details before placing.""",
            model="gpt-4o",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "search_doordash_restaurant",
                        "description": "Search for a restaurant on DoorDash",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "restaurant_name": {
                                    "type": "string",
                                    "description": "Name of the restaurant to search for"
                                },
                                "address": {
                                    "type": "string",
                                    "description": "Restaurant address for better matching"
                                }
                            },
                            "required": ["restaurant_name", "address"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "search_ubereats_restaurant",
                        "description": "Search for a restaurant on Uber Eats",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "restaurant_name": {
                                    "type": "string",
                                    "description": "Name of the restaurant to search for"
                                },
                                "address": {
                                    "type": "string",
                                    "description": "Restaurant address for better matching"
                                }
                            },
                            "required": ["restaurant_name", "address"]
                        }
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "place_delivery_order",
                        "description": "Place an order on the selected delivery platform",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "platform": {
                                    "type": "string",
                                    "enum": ["doordash", "ubereats"],
                                    "description": "Which platform to use"
                                },
                                "store_id": {
                                    "type": "string",
                                    "description": "Restaurant store ID on the platform"
                                },
                                "items": {
                                    "type": "array",
                                    "description": "List of items to order",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "quantity": {"type": "integer"},
                                            "price": {"type": "number"}
                                        }
                                    }
                                },
                                "delivery_address": {
                                    "type": "object",
                                    "description": "Delivery address details"
                                }
                            },
                            "required": ["platform", "store_id", "items", "delivery_address"]
                        }
                    }
                }
            ]
        )

        return assistant.id

    except Exception as e:
        logger.error(f"Error creating delivery assistant: {e}")
        raise e


def get_status_message(status):
    """Get user-friendly message for AI order status"""
    messages = {
        'validating': 'Checking your profile and delivery details...',
        'processing': 'AI is analyzing the best ordering approach...',
        'calling': 'AI agent is contacting the restaurant...',
        'confirmed': 'Your order has been successfully placed!',
        'failed': 'Unable to complete your order. Please try again.'
    }
    return messages.get(status, 'Processing your order...')

def get_estimated_time(status):
    """Get estimated time remaining for each status"""
    times = {
        'validating': '1-2 minutes',
        'processing': '2-3 minutes',
        'calling': '3-5 minutes',
        'confirmed': '',
        'failed': ''
    }
    return times.get(status, '')


@csrf_exempt
@require_firebase_auth
def ai_order_status_api(request, order_id):
    """
    GET: Get AI order status
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Only GET method allowed'}, status=405)

    try:
        ai_order = request.user.aiorder_set.get(id=order_id)

        return JsonResponse({
            'status': ai_order.status,
            'message': get_status_message(ai_order.status),
            'estimated_time': get_estimated_time(ai_order.status),
            'platform_order_id': ai_order.platform_order_id,
            'total_amount': float(ai_order.total_amount) if ai_order.total_amount else None
        })

    except AIOrder.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Order not found'}, status=404)
    except Exception as e:
        logger.error(f"AI order status error: {e}")
        return JsonResponse({'success': False, 'error': 'Server error'}, status=500)


# ===================================
# AI CONVERSATION API
# ===================================

def _extract_dishes_from_ai_response(ai_response_text):
    """Extract dish names and descriptions from AI response (looks for bullet lists with descriptions)"""
    import re

    dishes = []

    # Look for bullet point patterns with optional description in parentheses
    # Examples: "- Butter Chicken (creamy tomato sauce)" or "- Naan" or "• Tikka Masala - spicy grilled chicken"
    bullet_pattern = r'^[\s]*[-•*]\s+(.+?)(?:\s*[-–—]\s*(.+?))?(?:\s*\((.+?)\))?$'

    for line in ai_response_text.split('\n'):
        match = re.match(bullet_pattern, line.strip())
        if match:
            dish_name = match.group(1).strip()
            # Description can be in group 2 (after dash) or group 3 (in parentheses)
            description = match.group(2) or match.group(3)
            description = description.strip() if description else None

            # Clean up dish name
            dish_name = re.sub(r'[:\d]+\s*$', '', dish_name).strip()
            if dish_name and len(dish_name) > 2:  # At least 3 characters
                dishes.append({
                    "name": dish_name,
                    "mentions": 0,
                    "people": 0,
                    "description": description
                })

    return dishes


def _log_ai_conversation(restaurant_id, user_question, ai_response, cache_hit, cost, response_time_ms, conversation_type="restaurant", recommended_dishes=None, recommended_restaurants=None):
    """Log AI conversation for analytics and optimization

    Args:
        restaurant_id: Restaurant place_id (or "discovery" for home AI)
        user_question: User's question
        ai_response: AI's response
        cache_hit: Whether response was cached
        cost: Cost in USD
        response_time_ms: Response time in milliseconds
        conversation_type: "restaurant" or "home" (for analytics)
        recommended_dishes: List of recommended dishes (optional)
        recommended_restaurants: List of recommended restaurants (optional)
    """
    try:
        log_dir = Path(settings.BASE_DIR) / "var" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "ai_conversations.csv"

        # Create header if file doesn't exist
        file_exists = log_file.exists()

        with open(log_file, 'a', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)

            if not file_exists:
                writer.writerow([
                    'timestamp',
                    'conversation_type',  # NEW: restaurant or home
                    'restaurant_id',
                    'user_question',
                    'ai_response_preview',
                    'dishes_count',  # NEW
                    'restaurants_count',  # NEW
                    'dishes_list',  # NEW
                    'restaurants_list',  # NEW
                    'cache_hit',
                    'cost_usd',
                    'response_time_ms'
                ])

            # Clean response for CSV (remove newlines to prevent row splitting)
            # Save full response for complete analytics
            response_text = ai_response if ai_response else ''
            response_text = response_text.replace('\n', ' ').replace('\r', ' ')

            # Count and format structured data
            dishes_count = len(recommended_dishes) if recommended_dishes else 0
            restaurants_count = len(recommended_restaurants) if recommended_restaurants else 0

            # Format dish names (first 5)
            dishes_list = ""
            if recommended_dishes:
                dish_names = [d.get('dish_name', d.get('name', 'Unknown')) for d in recommended_dishes[:5]]
                dishes_list = "; ".join(dish_names)

            # Format restaurant names (first 5)
            restaurants_list = ""
            if recommended_restaurants:
                rest_names = [r.get('name', 'Unknown') for r in recommended_restaurants[:5]]
                restaurants_list = "; ".join(rest_names)

            writer.writerow([
                pd.Timestamp.now().isoformat(),
                conversation_type,  # restaurant or home
                restaurant_id,
                user_question,
                response_text,  # Full response, not just preview
                dishes_count,
                restaurants_count,
                dishes_list,
                restaurants_list,
                cache_hit,
                f"{cost:.6f}",
                response_time_ms
            ])

        logger.info(f"📝 Logged {conversation_type} AI: {restaurant_id} - cache_hit={cache_hit}, cost=${cost:.6f}, dishes={dishes_count}, restaurants={restaurants_count}")

    except Exception as e:
        logger.warning(f"Failed to log AI conversation: {e}")


@csrf_exempt
def ai_conversation_api(request):
    """
    POST: Send message to AI and get response
    Optional: Supports both anonymous and authenticated users
    """
    start_time = time.time()  # Track response time

    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)

    try:
        data = json.loads(request.body)

        # Validate required fields
        if 'message' not in data or 'restaurant_id' not in data:
            return JsonResponse({
                'success': False,
                'error': 'Missing required fields: message, restaurant_id'
            }, status=400)

        user_message = data['message'].strip()
        restaurant_id = data['restaurant_id']
        conversation_history = data.get('conversation_history', [])  # Array of {role, content}

        if not user_message:
            return JsonResponse({
                'success': False,
                'error': 'Message cannot be empty'
            }, status=400)

        # Check if we have dish data - include in cache key for auto-invalidation
        csv_path = category_csv_path(restaurant_id, "restaurant")
        dish_data_version = int(csv_path.stat().st_mtime) if csv_path.exists() else 0

        # Check cache first - cache based on message + restaurant_id + dish data version
        # This ensures cache invalidates when dish data updates
        # Normalize message for better cache hits (lowercase, trim)
        # Hash the key to avoid memcached special character warnings
        cache_key_raw = f"ai_chat:{restaurant_id}:{dish_data_version}:{user_message.lower().strip()}"
        cache_key = hashlib.md5(cache_key_raw.encode()).hexdigest()
        cached_response = cache.get(cache_key)

        # Fallback to file-based cache if in-memory cache misses
        if not cached_response:
            cached_response = _load_ai_cache_from_file(restaurant_id, user_message, dish_data_version)

        if cached_response:
            response_time_ms = int((time.time() - start_time) * 1000)
            logger.info(f"✅ CACHE HIT: AI response for '{user_message[:30]}...' at {restaurant_id}")

            # Log the cache hit
            _log_ai_conversation(
                restaurant_id=restaurant_id,
                user_question=user_message,
                ai_response=cached_response.get('response', ''),
                cache_hit=True,
                cost=0.0,  # FREE (cache hit)
                response_time_ms=response_time_ms
            )

            # Also warm up in-memory cache
            cache.set(cache_key, cached_response, timeout=60 * 60 * 24 * 90)
            return JsonResponse(cached_response)

        # Get restaurant info from cache
        restaurant_info = _get_restaurant_info_for_ai(restaurant_id)

        # Get user profile with saved addresses (if authenticated)
        user_id = data.get('user_id')  # Firebase UID from request
        user_profile = _get_user_food_profile(user_id) if user_id else None

        # Get recommended dishes from our cache (returns dict with 'text' and 'structured')
        recommended_dishes_data = _get_recommended_dishes_for_ai(restaurant_id)

        # Check if this is a common question we can answer without AI (FREE)
        # BUT ONLY if we have dish data - otherwise let AI handle it or user will see empty list
        has_dish_data = len(recommended_dishes_data['structured']) > 0

        common_answer = None
        if has_dish_data:
            common_answer = _handle_common_questions(user_message, restaurant_info, recommended_dishes_data['text'])

        if common_answer:
            response_time_ms = int((time.time() - start_time) * 1000)
            response_data = {
                'success': True,
                'response': common_answer,
                'restaurant_info': restaurant_info,
                'recommended_dishes': recommended_dishes_data['structured'],
                'quick_actions': _extract_restaurant_quick_actions(common_answer, user_profile, user_message)
            }

            # Log the pattern match (FREE)
            _log_ai_conversation(
                restaurant_id=restaurant_id,
                user_question=user_message,
                ai_response=common_answer,
                cache_hit=False,  # Not cached, but pattern matched
                cost=0.0,  # FREE (pattern matching)
                response_time_ms=response_time_ms
            )

            # Cache common answers for 3 months (90 days)
            cache.set(cache_key, response_data, timeout=60 * 60 * 24 * 90)
            logger.info(f"💡 Answered common question without AI (FREE): '{user_message[:30]}'")
            return JsonResponse(response_data)

        # Build user context for personalized greeting
        user_context = ""
        saved_addresses = []
        if user_profile:
            # Check if user has ordered from THIS restaurant before
            last_order_by_restaurant = user_profile.get('last_order_by_restaurant', {})
            restaurant_last_order = last_order_by_restaurant.get(restaurant_id)

            if restaurant_last_order:
                # User has ordered from this restaurant before - show reorder prompt
                dishes_str = ", ".join(restaurant_last_order.get('dishes', [])[:2])  # First 2 dishes
                user_context = f"""
USER PROFILE:
- 🔁 RETURNING CUSTOMER at this restaurant!
- Last order from {restaurant_info['name']}: {dishes_str}
- START WITH: "Welcome back! Last time you ordered {dishes_str}. Order again?"
- USE BUTTONS: ['🔁 Yes, order again', '🔍 Try something new']
- NEVER write button text in your response - buttons render automatically
"""
            else:
                # User hasn't ordered from THIS restaurant before, but check saved address
                saved_addresses = user_profile.get('saved_addresses', [])
                if saved_addresses:
                    default_addr = saved_addresses[0]
                    user_context = f"""
USER PROFILE:
- ✅ RETURNING USER (first time at this restaurant) with saved delivery address: {default_addr.get('street')}, {default_addr.get('city')}, {default_addr.get('state')} {default_addr.get('zip')}
- When user first messages you, greet them warmly and confirm their saved address
- Example: "You usually order delivery to {default_addr.get('street')}. Would you like to deliver there again?"
- NEVER write button text in your response - buttons render automatically
"""
                else:
                    user_context = """
USER PROFILE:
- ⚠️ NEW USER or no saved address
- Ask: "Would you like delivery or pickup?"
- NEVER write button text like ["Delivery", "Pickup"] - buttons render automatically
"""
        else:
            user_context = """
USER PROFILE:
- Anonymous user
- Ask: "Would you like delivery or pickup?"
"""

        # Build AI context with stronger bullet-point enforcement when no dish data
        if has_dish_data:
            dish_context = f"""RECOMMENDED DISHES (from our ethnicity-based analysis):
{recommended_dishes_data['text']}"""
        else:
            dish_context = """IMPORTANT: We're still analyzing reviews for this restaurant. When asked about popular dishes, you MUST suggest 3-5 typical dishes for this cuisine type and format them as a bullet list with SHORT descriptions (one dish per line starting with "-"). This is critical for our ordering system."""

        system_prompt = f"""You are a helpful food ordering assistant for {restaurant_info['name']}.

RESTAURANT INFO:
- Name: {restaurant_info['name']}
- Address: {restaurant_info.get('address', 'Not available')}
- Phone: {restaurant_info.get('phone', 'Not available')}

{user_context}

{dish_context}

Your role:
1. Help users understand what dishes are popular and recommended
2. Answer questions about the menu
3. Help them decide what to order
4. **SMART ORDERING**: When user says "Order top dishes for X people", automatically:
   - Select 3-5 popular dishes from the recommended list above (or typical dishes for this cuisine if no data)
   - Suggest appropriate quantities based on party size (e.g., 2 people = 2-3 dishes, 4 people = 4-5 dishes)
   - Present as a ready-to-order selection
   - Say: "I've selected these dishes for X people: [list with quantities]. Ready to order?"
5. **CRITICAL FORMATTING RULE**: When recommending ANY dishes, you MUST format them as a bullet list with descriptions like this:
   - Butter Chicken - creamy tomato curry with tender chicken
   - Chicken Tikka Masala - grilled chicken in spiced tomato sauce
   - Garlic Naan - soft flatbread with garlic and butter

   Format options (choose one):
   • "- Dish Name - short description" (preferred)
   • "- Dish Name (short description)"

   Keep descriptions under 8 words. Do NOT use numbered lists or plain text.
   This exact format is required for our ordering system to extract the dishes.
6. Be conversational and friendly, but ALWAYS include the bullet list with descriptions when suggesting dishes

Current conversation context:
{json.dumps(conversation_history, indent=2) if conversation_history else 'First message'}

User message: {user_message}

Respond naturally and helpfully. For smart ordering requests, make the selection immediately.
For regular orders:
- If returning user with saved address → Confirm their saved address first
- If new user or no saved address → Ask "Delivery or pickup?" then collect details

REMEMBER: If suggesting dishes, use bullet format with descriptions (- Dish Name - description) on separate lines."""

        # Only call OpenAI if we couldn't answer with pattern matching
        logger.info(f"❌ CACHE MISS: AI response for '{user_message[:30]}...' - calling OpenAI (~$0.0002)")

        # Call OpenAI
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # Cost-effective for conversation
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.7,
            max_tokens=500
        )

        ai_response = response.choices[0].message.content
        response_time_ms = int((time.time() - start_time) * 1000)

        # If we don't have review-based dish data yet, extract dishes from AI response
        dishes_to_show = recommended_dishes_data['structured']
        if not has_dish_data:
            # Extract dishes from AI's bullet-point response
            ai_suggested_dishes = _extract_dishes_from_ai_response(ai_response)
            if ai_suggested_dishes:
                dishes_to_show = ai_suggested_dishes
                logger.info(f"📝 Extracted {len(ai_suggested_dishes)} dishes from AI response (review data not ready yet)")

        # Build response object with structured dish data
        response_data = {
            'success': True,
            'response': ai_response,
            'restaurant_info': restaurant_info,
            'recommended_dishes': dishes_to_show,  # Either review-based or AI-suggested
            'quick_actions': _extract_restaurant_quick_actions(ai_response, user_profile, user_message)
        }

        # Log the OpenAI API call (COST MONEY)
        _log_ai_conversation(
            restaurant_id=restaurant_id,
            user_question=user_message,
            ai_response=ai_response,
            cache_hit=False,
            cost=0.0002,  # gpt-4o-mini cost per message
            response_time_ms=response_time_ms
        )

        # Cache the response for 3 months (90 days - same as contact info)
        # Use 90 days because dish recommendations are stable
        cache.set(cache_key, response_data, timeout=60 * 60 * 24 * 90)

        # Also save to file for persistence across server restarts
        _save_ai_cache_to_file(restaurant_id, user_message, dish_data_version, response_data)

        logger.info(f"💾 Cached AI response for '{user_message[:30]}...' (90 days, in-memory + file)")

        return JsonResponse(response_data)

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"AI conversation error: {e}")
        return JsonResponse({'success': False, 'error': 'Server error'}, status=500)


def _get_restaurant_info_for_ai(place_id):
    """Get restaurant info from contact_info cache for AI context"""
    try:
        reviews_dir = Path(settings.BASE_DIR) / "var" / "reviews" / place_id
        contact_file = reviews_dir / "contact_info.json"

        if contact_file.exists():
            with open(contact_file, 'r', encoding='utf-8') as f:
                contact_info = json.load(f)
                return {
                    'name': contact_info.get('name', 'Restaurant'),
                    'phone': contact_info.get('phone'),
                    'address': contact_info.get('formatted_address'),
                    'website': contact_info.get('website')
                }
    except Exception as e:
        logger.warning(f"Error reading contact info for AI: {e}")

    return {'name': 'Restaurant', 'phone': None, 'address': None, 'website': None}


def _get_recommended_dishes_for_ai(place_id):
    """Get recommended dishes from dish_mentions.csv for AI context and UI display"""
    try:
        csv_path = category_csv_path(place_id, "restaurant")

        if csv_path.exists():
            df = pd.read_csv(csv_path, dtype={"ethnicity_ui": "string", "dish": "string", "price": "string", "description": "string"})

            # Get top 5 popular dishes across all ethnicities
            top_dishes = (df.groupby("dish", as_index=False)
                          .agg({
                              "mentions": "sum",
                              "unique_authors": "sum",
                              "price": "first",  # Get first price (they're all same for matched dishes)
                              "description": "first"  # Get first description
                          })
                          .sort_values(["mentions", "unique_authors"], ascending=[False, False])
                          .head(5))

            # Text format for AI context (clean format with just name, NO price in text)
            dishes_text_parts = []
            for _, row in top_dishes.iterrows():
                # Don't include price in text - it's shown in the UI buttons below
                dishes_text_parts.append(f"- {row['dish']}")
            dishes_text = "\n".join(dishes_text_parts)

            # Structured format for UI display (include price and description)
            dishes_structured = []
            for _, row in top_dishes.iterrows():
                dish_obj = {
                    "name": row['dish'],
                    "mentions": int(row['mentions']),
                    "people": int(row['unique_authors'])
                }
                # Add price if available
                if pd.notna(row.get('price')) and row.get('price'):
                    dish_obj["price"] = row['price']
                # Add description if available
                if pd.notna(row.get('description')) and row.get('description'):
                    dish_obj["description"] = row['description']
                dishes_structured.append(dish_obj)

            return {
                "text": dishes_text if dishes_text else "No dish recommendations available yet",
                "structured": dishes_structured
            }
    except Exception as e:
        logger.warning(f"Error reading dishes for AI: {e}")

    return {
        "text": "No dish recommendations available yet",
        "structured": []
    }


def _handle_common_questions(message, restaurant_info, recommended_dishes):
    """Handle common questions without calling OpenAI API (FREE)"""
    msg_lower = message.lower().strip()

    # Common question patterns
    popular_keywords = ["what's good", "what's popular", "what do you recommend",
                       "best dishes", "most popular", "top dishes", "recommended"]

    info_keywords = ["hours", "when open", "what time", "phone", "address", "location"]

    # Check if asking about popular/recommended dishes
    if any(keyword in msg_lower for keyword in popular_keywords):
        return f"Here are the most popular dishes at {restaurant_info['name']} based on customer reviews:\n\n{recommended_dishes}\n\nWould you like to know more about any of these dishes?"

    # Check if asking about restaurant info (hours, phone, address)
    if any(keyword in msg_lower for keyword in info_keywords):
        info_parts = []
        if restaurant_info.get('phone'):
            info_parts.append(f"📞 Phone: {restaurant_info['phone']}")
        if restaurant_info.get('address'):
            info_parts.append(f"📍 Address: {restaurant_info['address']}")

        if info_parts:
            return f"Here's the information for {restaurant_info['name']}:\n\n" + "\n".join(info_parts) + "\n\nHow can I help you order?"

    # Not a common question - need to use AI
    return None


def _load_ai_cache_from_file(place_id, message, dish_data_version):
    """Load AI conversation cache from file"""
    try:
        reviews_dir = Path(settings.BASE_DIR) / "var" / "reviews" / place_id
        cache_file = reviews_dir / "ai_cache.json"

        if not cache_file.exists():
            return None

        with open(cache_file, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)

        # Normalize message for lookup
        msg_key = message.lower().strip()

        # Check if we have cached response for this message and dish version
        cache_entry = cache_data.get(msg_key)
        if cache_entry and cache_entry.get('dish_data_version') == dish_data_version:
            # Check if not expired (90 days)
            cached_time = pd.to_datetime(cache_entry.get('cached_at', '1970-01-01'))
            age_days = (pd.Timestamp.now() - cached_time).days

            if age_days < 90:
                logger.info(f"✅ FILE CACHE HIT: '{message[:30]}' (age: {age_days} days)")
                return cache_entry.get('response_data')

        return None

    except Exception as e:
        logger.warning(f"Error reading AI cache file: {e}")
        return None


def _save_ai_cache_to_file(place_id, message, dish_data_version, response_data):
    """Save AI conversation cache to file for persistence"""
    try:
        reviews_dir = Path(settings.BASE_DIR) / "var" / "reviews" / place_id
        reviews_dir.mkdir(parents=True, exist_ok=True)

        cache_file = reviews_dir / "ai_cache.json"

        # Load existing cache or create new
        if cache_file.exists():
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
        else:
            cache_data = {}

        # Normalize message for storage
        msg_key = message.lower().strip()

        # Save with metadata
        cache_data[msg_key] = {
            'response_data': response_data,
            'dish_data_version': dish_data_version,
            'cached_at': pd.Timestamp.now().isoformat()
        }

        # Write back to file
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, indent=2, ensure_ascii=False)

        logger.info(f"💾 Saved AI cache to file: {cache_file}")

    except Exception as e:
        logger.warning(f"Error saving AI cache to file: {e}")


@csrf_exempt
@require_firebase_auth
def discover_dish_customizations_api(request):
    """
    POST: Discover customization options for selected dishes

    Request body:
    {
        "restaurant_id": "ChIJAb33GUS0j4ARjGKbpYi5jR4",
        "dishes": ["BBQ Ranch Bacon Burger*", "Cuban Sandwich"]
    }

    Response:
    {
        "success": true,
        "customizations": {
            "BBQ Ranch Bacon Burger*": {
                "dish_name": "BBQ Ranch Bacon Burger*",
                "sizes": [...],
                "add_ons": [...],
                "modifications": [...],
                "special_instructions_allowed": true
            }
        }
    }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body)

        # Validate required fields
        restaurant_id = data.get('restaurant_id')
        order_type = data.get('order_type', 'delivery')  # 'pickup' or 'delivery'
        dishes = data.get('dishes', [])

        # Get user's default delivery address from profile (for Uber Eats address prompt)
        # IMPORTANT: Fetch for BOTH delivery AND pickup (many sites require address even for pickup)
        delivery_address = None
        if hasattr(request, 'user') and request.user.is_authenticated:
            try:
                # Get user's default delivery address
                default_address = DeliveryAddress.objects.filter(
                    user=request.user,
                    is_default=True
                ).first()

                if default_address:
                    # Format as "street, city, state zip"
                    delivery_address = f"{default_address.street_address}, {default_address.city}, {default_address.state} {default_address.zip_code}"
                    logger.info(f"Using user's default address: {delivery_address}")
                else:
                    logger.warning(f"No default address found for user - automation may fail if site requires address")
            except Exception as e:
                logger.warning(f"Could not fetch user delivery address: {e}")

        if not restaurant_id:
            return JsonResponse({
                'success': False,
                'error': 'Missing required field: restaurant_id'
            }, status=400)

        if not dishes or not isinstance(dishes, list):
            return JsonResponse({
                'success': False,
                'error': 'Missing or invalid field: dishes (must be a non-empty array)'
            }, status=400)

        logger.info(f"Discovering customizations for {len(dishes)} dishes at {restaurant_id}")
        logger.info(f"Order type: {order_type}")

        # Get ordering URL from menu_structure.json based on order_type
        from .utils.menu_structure_cache import get_restaurant_menu
        restaurant_name = _get_restaurant_name(restaurant_id)
        menu_structure = get_restaurant_menu(restaurant_id, restaurant_name, None)

        if not menu_structure:
            return JsonResponse({
                'success': False,
                'error': 'Menu structure not available for this restaurant'
            }, status=400)

        # Select correct URL based on order type
        if order_type == 'pickup':
            ordering_url = menu_structure.ordering_url_pickup or menu_structure.ordering_url_delivery
            logger.info(f"🏪 Using pickup URL: {ordering_url}")
        else:
            ordering_url = menu_structure.ordering_url_delivery or menu_structure.ordering_url_pickup
            logger.info(f"🚚 Using delivery URL: {ordering_url}")

        if not ordering_url:
            return JsonResponse({
                'success': False,
                'error': 'Online ordering not available for this restaurant'
            }, status=400)

        # Import and run discovery
        import asyncio
        from .utils.dish_customization import discover_dish_customizations

        # Run async function (pass user's real delivery address for Uber Eats prompt)
        # Use user's address for BOTH delivery AND pickup (many sites ask for address even for pickup)
        restaurant_location = None
        if not delivery_address:
            # Only use restaurant fallback if user has NO address saved
            import re
            restaurant_name = menu_structure.restaurant_name or ""
            city_match = re.search(r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*),?\s+([A-Z]{2})', str(restaurant_name))
            restaurant_location = f"{city_match.group(1)}, {city_match.group(2)}" if city_match else "San Jose, CA"
            logger.info(f"No user address, using restaurant location fallback: {restaurant_location}")
        else:
            logger.info(f"Using user's delivery address: {delivery_address}")

        customizations = asyncio.run(
            discover_dish_customizations(restaurant_id, ordering_url, dishes, delivery_address, restaurant_location)
        )

        return JsonResponse({
            'success': True,
            'customizations': customizations,
            'restaurant_id': restaurant_id,
            'ordering_url': ordering_url
        })

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Error discovering dish customizations: {e}", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': f'Server error: {str(e)}'
        }, status=500)


@csrf_exempt
@require_firebase_auth
def automate_order_api(request):
    """
    POST: Automate order placement using discovered customizations and user selections

    Request body:
    {
        "restaurant_id": "ChIJAb33GUS0j4ARjGKbpYi5jR4",
        "ordering_url": "https://orders.lazydogrestaurants.com/order/cupertino",
        "dish_selections": [
            {
                "dish_name": "BBQ Ranch Bacon Burger*",
                "group_selections": [
                    {
                        "label": "How would you like it cooked?",
                        "type": "single_choice",
                        "selected_options": ["Med Rare"]
                    },
                    {
                        "label": "What side would you like?",
                        "type": "single_choice",
                        "selected_options": ["Cajun Fries"]
                    }
                ],
                "special_instructions": "Extra crispy please",
                "quantity": 1
            }
        ],
        "user_info": {
            "first_name": "John",
            "last_name": "Doe",
            "email": "john@example.com",
            "phone": "(555) 123-4567",
            "address": "123 Main St",
            "city": "Cupertino",
            "state": "CA",
            "zip": "95014"
        }
    }

    Response:
    {
        "success": true,
        "order_id": "auto_order_123456",
        "dishes_added": 1,
        "message": "Order automated successfully"
    }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body)

        # Validate required fields
        restaurant_id = data.get('restaurant_id')
        ordering_url = data.get('ordering_url')
        dish_selections = data.get('dish_selections', [])
        user_info = data.get('user_info', {})

        # Convert camelCase keys from Swift to snake_case for Python
        def convert_to_snake_case(obj):
            """Recursively convert camelCase keys to snake_case"""
            if isinstance(obj, dict):
                new_dict = {}
                for key, value in obj.items():
                    # Convert camelCase to snake_case
                    snake_key = ''.join(['_' + c.lower() if c.isupper() else c for c in key]).lstrip('_')
                    new_dict[snake_key] = convert_to_snake_case(value)
                return new_dict
            elif isinstance(obj, list):
                return [convert_to_snake_case(item) for item in obj]
            else:
                return obj

        # Convert dish_selections keys
        dish_selections = convert_to_snake_case(dish_selections)
        user_info = convert_to_snake_case(user_info)

        if not restaurant_id:
            return JsonResponse({
                'success': False,
                'dishes_added': 0,
                'message': 'Missing required field: restaurant_id'
            }, status=400)

        if not ordering_url:
            return JsonResponse({
                'success': False,
                'dishes_added': 0,
                'message': 'Missing required field: ordering_url'
            }, status=400)

        # DoorDash: Return URL for WebView immediately (before validating dish_selections)
        if 'doordash.com' in ordering_url.lower():
            from datetime import datetime
            order_id = f"auto_order_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            logger.info(f"🔍 DoorDash detected - returning URL for WebView (order_id: {order_id})")
            return JsonResponse({
                'success': True,
                'order_id': order_id,
                'dishes_added': 0,  # No dishes added - handled in WebView
                'use_webview': True,
                'checkout_url': ordering_url,
                'session_cookies': [],  # No cookies - WebView handles session
                'local_storage': {},  # No storage - WebView handles state
                'message': 'DoorDash requires WebView - please complete order in app'
            })

        # Validate dish_selections for non-DoorDash platforms
        if not dish_selections or not isinstance(dish_selections, list):
            return JsonResponse({
                'success': False,
                'dishes_added': 0,
                'message': 'Missing or invalid field: dish_selections (must be a non-empty array)'
            }, status=400)

        logger.info(f"Automating order for {len(dish_selections)} dishes at {restaurant_id}")
        logger.info(f"Ordering URL: {ordering_url}")

        # Get restaurant name for DoorDash search
        restaurant_name = _get_restaurant_name(restaurant_id)
        logger.info(f"Restaurant name: {restaurant_name}")

        # Import and run automation
        import asyncio
        from .utils.order_automation import automate_restaurant_order

        # Run async automation
        result = asyncio.run(
            automate_restaurant_order(
                restaurant_id=restaurant_id,
                restaurant_name=restaurant_name,
                ordering_url=ordering_url,
                dish_selections=dish_selections,
                user_info=user_info
            )
        )

        if result.get('success'):
            return JsonResponse({
                'success': True,
                'order_id': result.get('order_id'),
                'dishes_added': result.get('dishes_added', len(dish_selections)),
                'message': result.get('message', 'Navigated to checkout'),
                'checkout_url': result.get('checkout_url'),
                'session_cookies': result.get('session_cookies', []),
                'local_storage': result.get('local_storage', {})
            })
        else:
            # Pass through ALL fields for fallback scenarios (e.g., restaurant closed)
            # iOS needs order_id, dishes_added, checkout_url, session_cookies, local_storage, and fallback_to_manual
            return JsonResponse({
                'success': False,
                'order_id': result.get('order_id', ''),
                'dishes_added': result.get('dishes_added', 0),
                'message': result.get('message', result.get('error', 'Automation failed')),
                'checkout_url': result.get('checkout_url'),
                'session_cookies': result.get('session_cookies', []),
                'local_storage': result.get('local_storage', {}),
                'fallback_to_manual': result.get('fallback_to_manual', False),
                'error': result.get('error'),  # Keep for backward compatibility
                'details': result.get('details')
            }, status=400)

    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'dishes_added': 0,
            'message': 'Invalid JSON'
        }, status=400)
    except Exception as e:
        logger.error(f"Error automating order: {e}", exc_info=True)
        return JsonResponse({
            'success': False,
            'dishes_added': 0,
            'message': f'Server error: {str(e)}'
        }, status=500)


# ============================================================================
# HOME AI CHAT - Discovery & Restaurant Recommendation
# ============================================================================

@csrf_exempt
def ai_home_chat_api(request):
    """
    POST: Home screen AI chat for restaurant discovery and recommendation
    Different from restaurant AI - helps users DISCOVER and SELECT restaurants

    Request body:
    {
        "message": "I want pizza",
        "conversation_history": [...],  # Optional
        "location": {"lat": 37.123, "lng": -122.456},  # Optional
        "user_id": "firebase_uid"  # Optional (for personalization)
    }

    Response:
    {
        "success": true,
        "response": "AI response text",
        "recommended_restaurants": [...],  # List of Restaurant objects
        "quick_actions": ["Order Pizza", "Try Tacos", ...],  # Button suggestions
        "conversation_state": "showing_options"  # Current state in flow
    }
    """
    start_time = time.time()

    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method allowed'}, status=405)

    try:
        data = json.loads(request.body)

        # Validate required fields
        if 'message' not in data:
            return JsonResponse({
                'success': False,
                'error': 'Missing required field: message'
            }, status=400)

        user_message = data['message'].strip()
        conversation_history = data.get('conversation_history', [])
        location = data.get('location')  # {"lat": float, "lng": float}
        user_id = data.get('user_id')  # Firebase UID

        if not user_message:
            return JsonResponse({
                'success': False,
                'error': 'Message cannot be empty'
            }, status=400)

        # Get user food profile (order history, preferences, saved addresses)
        user_profile = _get_user_food_profile(user_id) if user_id else None

        # Get nearby restaurants if location provided
        nearby_restaurants = []
        if location:
            nearby_restaurants = _get_nearby_restaurants(
                lat=location.get('lat'),
                lng=location.get('lng'),
                radius=5  # 5 miles
            )

        # Build context for AI (includes saved address info)
        context = _build_home_ai_context(
            user_profile=user_profile,
            nearby_restaurants=nearby_restaurants,
            location=location,
            conversation_history=conversation_history
        )

        # Build enhanced system prompt with cached restaurant data
        nearby_restaurants_with_data = _enrich_restaurants_with_cache_data(nearby_restaurants)

        system_prompt = f"""You are a helpful food discovery assistant for Crave.

Your role:
1. Help users discover restaurants based on their preferences
2. Recommend dishes using REAL data from nearby restaurants (cached reviews + AI knowledge)
3. Guide users efficiently through ordering with DISH-FIRST approach

USER CONTEXT:
{context}

NEARBY RESTAURANTS (prioritize ✅ cached restaurants - they have REAL review data):
{_format_nearby_restaurants_for_ai(nearby_restaurants_with_data)}

CRITICAL RULES:
1. **DISH-FIRST ORDERING FLOW (NEW!)** - Speed is everything!
   - We show REAL dish buttons from nearby restaurants (not generic dishes)
   - Dishes come from cached review data OR AI recommendations
   - Each button has format: "🍕 Tony's Special ($12)" - shows dish + price + restaurant

2. **EFFICIENT conversation flow**:

   A. **FIRST MESSAGE (after cuisine selected)**:
      - Returning user WITH saved address → "Great choice! 🍕 Deliver to [saved address]?"
        * NEVER write button text in your response - buttons render automatically below your message
        * DO NOT show dishes/restaurants yet - wait for order type confirmation

      - New user OR no saved address → "Would you like delivery or pickup?"
        * NEVER write button text like ["Delivery", "Pickup"] - buttons render automatically
        * DO NOT show dishes/restaurants yet

   B. **AFTER address/delivery confirmed**:
      - System automatically shows 3-4 REAL dish buttons from nearby good restaurants
      - Just say: "Here are the most popular [cuisine] dishes near you!"
      - NEVER write button text - buttons appear automatically
      - DO NOT mention specific restaurant names yet

   C. **AFTER user clicks dish button (e.g., "🍕 Tony's Special ($12)")**:
      - User message will contain the full button text
      - Extract dish name and recommend that specific restaurant
      - Say: "Great choice! Order [dish name] from [restaurant name]? (4.5⭐, 10 min away)"
      - Show [✓ Order now] button automatically

   D. **Important**:
      - NEVER mention specific restaurants until user clicks a dish button
      - This ensures fast ordering: Cuisine → Address → Dish → Restaurant → Done!

3. **CRITICAL: Button Text Formatting**:
   - NEVER include button arrays like ["Option 1", "Option 2"] in your text response
   - NEVER write "Buttons:" or list buttons in brackets
   - Just ask the question naturally - buttons are added automatically by the system
   - Example GOOD: "Would you like delivery or pickup?"
   - Example BAD: "Would you like delivery or pickup? [Delivery] [Pickup]"

4. **Be natural and concise**:
   - Match user's vibe (casual → casual, specific → specific)
   - Under 100 words per response
   - Use emojis sparingly (only for cuisines: 🍕🌮🍛 and order type: 🚗🏃)

Current conversation:
{json.dumps(conversation_history, indent=2) if conversation_history else 'First message'}

User message: {user_message}

Respond naturally and guide them toward fast ordering with the DISH-FIRST flow!"""

        # Call OpenAI
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.7,
            max_tokens=300
        )

        ai_response = response.choices[0].message.content
        response_time_ms = int((time.time() - start_time) * 1000)

        # Extract quick actions from AI response or generate based on context
        # This now returns EITHER dish objects OR simple button strings
        quick_actions_or_dishes = _extract_quick_actions(
            ai_response,
            user_profile,
            user_message,
            location=location,
            conversation_history=conversation_history
        )

        # Separate dish objects from simple buttons
        recommended_dishes = []
        quick_actions = []

        if isinstance(quick_actions_or_dishes, list) and len(quick_actions_or_dishes) > 0:
            first_item = quick_actions_or_dishes[0]
            if isinstance(first_item, dict) and 'dish_name' in first_item:
                # These are structured dish objects
                recommended_dishes = quick_actions_or_dishes
            else:
                # These are simple button strings
                quick_actions = quick_actions_or_dishes

        # Filter recommended restaurants based on conversation
        # IMPORTANT: If we're showing dishes, DON'T show restaurants separately
        # (dishes already contain restaurant info)
        if recommended_dishes:
            # Extract unique restaurants from dishes for logging only
            seen_restaurant_ids = set()
            recommended_restaurants = []
            for dish in recommended_dishes:
                restaurant_id = dish.get('restaurant_id')
                if restaurant_id and restaurant_id not in seen_restaurant_ids:
                    seen_restaurant_ids.add(restaurant_id)
                    # Create minimal restaurant object for logging
                    recommended_restaurants.append({
                        'place_id': restaurant_id,
                        'name': dish.get('restaurant_name', 'Unknown'),
                        'rating': dish.get('rating'),
                        'distance_miles': dish.get('distance_miles')
                    })
        else:
            # No dishes - show restaurant recommendations
            recommended_restaurants = _filter_restaurants_for_response(
                nearby_restaurants=nearby_restaurants,
                user_message=user_message,
                ai_response=ai_response,
                user_profile=user_profile
            )

        # Log home AI conversation for analytics (after extracting structured data)
        _log_ai_conversation(
            restaurant_id="discovery",  # Home AI is discovery mode
            user_question=user_message,
            ai_response=ai_response,
            cache_hit=False,  # Home AI doesn't use cache (yet)
            cost=0.0002,  # gpt-4o-mini cost per message
            response_time_ms=response_time_ms,
            conversation_type="home",
            recommended_dishes=recommended_dishes,
            recommended_restaurants=recommended_restaurants
        )

        # Determine conversation state
        conversation_state = _determine_conversation_state(user_message, ai_response, user_profile)

        # Extract and save user preferences from this conversation (if user_id provided)
        # This builds long-term memory of dietary restrictions, cuisine preferences, etc.
        if user_id:
            try:
                extracted_prefs = _extract_preferences_from_conversation(user_message, ai_response, user_profile)
                if extracted_prefs:
                    _save_conversation_preferences(user_id, extracted_prefs)
                    logger.info(f"📝 Extracted {len(extracted_prefs)} preferences from conversation")
            except Exception as e:
                logger.warning(f"Failed to extract preferences: {e}")

        response_data = {
            'success': True,
            'response': ai_response,
            'recommended_restaurants': recommended_restaurants,
            'recommended_dishes': recommended_dishes,  # NEW: Structured dish objects
            'quick_actions': quick_actions,  # Simple button strings (Delivery/Pickup, etc.)
            'conversation_state': conversation_state
        }

        # Log what we're sending to frontend
        logger.info(f"📤 SENDING TO FRONTEND:")
        logger.info(f"   Response: {ai_response[:100]}...")
        logger.info(f"   Quick actions: {quick_actions}")
        logger.info(f"   Restaurants count: {len(recommended_restaurants)}")
        logger.info(f"   Dishes count: {len(recommended_dishes)}")
        if recommended_dishes:
            logger.info(f"   📊 DISH DATA BEING SENT:")
            for i, dish in enumerate(recommended_dishes[:3]):
                logger.info(f"      Dish {i+1}: {dish}")

        return JsonResponse(response_data)

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Home AI chat error: {e}", exc_info=True)
        return JsonResponse({'success': False, 'error': 'Server error'}, status=500)


def _get_user_food_profile(user_id):
    """Get user's food preferences from order history

    Args:
        user_id: Firebase UID (stored as Django User.username)
    """
    if not user_id:
        return None

    try:
        # Get Django User by Firebase UID (stored as username)
        user = User.objects.filter(username=user_id).first()
        if not user:
            logger.warning(f"No Django User found for Firebase UID: {user_id}")
            return None

        # Get user's order history from AIOrder model
        recent_orders = AIOrder.objects.filter(user=user).order_by('-created_at')[:20]

        # Get saved addresses
        saved_addresses = _get_user_saved_addresses(user_id)

        if not recent_orders.exists():
            # No order history, but check if they have saved address (completed onboarding)
            return {
                'is_first_time': len(saved_addresses) == 0,  # First-time = no saved address
                'favorite_cuisines': {},
                'favorite_restaurants': {},
                'favorite_dishes': {},
                'total_orders': 0,
                'saved_addresses': saved_addresses
            }

        # Analyze order patterns
        cuisine_counts = {}
        restaurant_counts = {}
        dish_counts = {}

        # Track last order details for personalization
        last_order = None
        last_order_by_restaurant = {}  # {place_id: {restaurant_name, dishes, created_at}}

        for order in recent_orders:
            restaurant_id = order.restaurant_place_id
            restaurant_name = order.restaurant_name
            restaurant_counts[restaurant_id] = restaurant_counts.get(restaurant_id, 0) + 1

            # Track dishes from this order
            for dish_name in order.dishes:
                dish_counts[dish_name] = dish_counts.get(dish_name, 0) + 1

            # Capture LAST order overall (most recent)
            if last_order is None:
                last_order = {
                    'restaurant_name': restaurant_name,
                    'restaurant_id': restaurant_id,
                    'dishes': order.dishes,
                    'created_at': order.created_at.isoformat() if order.created_at else None
                }

            # Capture last order PER RESTAURANT
            if restaurant_id not in last_order_by_restaurant:
                last_order_by_restaurant[restaurant_id] = {
                    'restaurant_name': restaurant_name,
                    'dishes': order.dishes,
                    'created_at': order.created_at.isoformat() if order.created_at else None
                }

        # Get saved addresses
        saved_addresses = _get_user_saved_addresses(user_id)

        return {
            'is_first_time': False,
            'favorite_cuisines': cuisine_counts,
            'favorite_restaurants': restaurant_counts,
            'favorite_dishes': dish_counts,
            'total_orders': recent_orders.count(),
            'saved_addresses': saved_addresses,
            'last_order': last_order,  # NEW: Most recent order
            'last_order_by_restaurant': last_order_by_restaurant  # NEW: Last order per restaurant
        }

    except Exception as e:
        logger.error(f"Error getting user food profile: {e}")
        return None


def _get_user_saved_addresses(user_id):
    """Get user's saved delivery addresses

    Args:
        user_id: Firebase UID (stored as Django User.username)
    """
    try:
        # Get Django User by Firebase UID (stored as username)
        user = User.objects.filter(username=user_id).first()
        if not user:
            logger.warning(f"❌ No Django User found for Firebase UID: {user_id}")
            return []

        logger.info(f"✅ Found Django User: {user.username} (id={user.id})")

        # Get UserProfile for this user
        user_profile = UserProfile.objects.filter(user=user).first()
        if not user_profile:
            logger.warning(f"⚠️ No UserProfile found for user: {user.username}")
        else:
            logger.info(f"✅ Found UserProfile for user: {user.username}")

        # Get addresses linked to this user
        addresses = DeliveryAddress.objects.filter(user=user).order_by('-is_default', '-id')
        logger.info(f"📍 Found {addresses.count()} addresses for user {user.username}")

        if addresses.exists():
            for addr in addresses:
                logger.info(f"   - {addr.name}: {addr.street_address}, {addr.city}, {addr.state} (default={addr.is_default})")

        return [{
            'street': addr.street_address,
            'city': addr.city,
            'state': addr.state,
            'zip': addr.zip_code,
            'is_default': addr.is_default
        } for addr in addresses]

    except Exception as e:
        logger.error(f"❌ Error getting saved addresses: {e}", exc_info=True)
        return []


def _get_nearby_restaurants(lat, lng, radius=5):
    """Get nearby restaurants using Google Maps API"""
    try:
        gmaps = googlemaps.Client(key=settings.GOOGLE_API_KEY)

        # Search for restaurants
        places_result = gmaps.places_nearby(
            location=(lat, lng),
            radius=radius * 1609.34,  # Convert miles to meters
            type='restaurant'
        )

        restaurants = []
        for place in places_result.get('results', [])[:10]:  # Limit to top 10
            restaurants.append({
                'place_id': place.get('place_id'),
                'name': place.get('name'),
                'address': place.get('vicinity'),
                'rating': place.get('rating'),
                'price_level': place.get('price_level'),
                'cuisine': place.get('types', [])
            })

        return restaurants

    except Exception as e:
        logger.error(f"Error getting nearby restaurants: {e}")
        return []


def _build_home_ai_context(user_profile, nearby_restaurants, location, conversation_history=None):
    """Build context string for AI prompt"""
    context_parts = []

    # User profile context
    if user_profile:
        if user_profile.get('is_first_time'):
            context_parts.append("- First-time user (no order history)")
        else:
            context_parts.append(f"- Returning user ({user_profile['total_orders']} previous orders)")

            # Add last order info for personalization
            last_order = user_profile.get('last_order')
            if last_order:
                dishes_str = ", ".join(last_order.get('dishes', [])[:2])  # First 2 dishes
                context_parts.append(f"- 🔁 LAST ORDER: {dishes_str} from {last_order.get('restaurant_name')}")
                context_parts.append(f"  → START WITH: 'Welcome back! Last time you ordered {dishes_str} from {last_order.get('restaurant_name')}. Order again?'")
                context_parts.append(f"  → USE BUTTON: ['🔁 Yes, order again', '🔍 Try something new']")
            elif user_profile.get('favorite_restaurants'):
                top_restaurant = max(user_profile['favorite_restaurants'],
                                   key=user_profile['favorite_restaurants'].get)
                context_parts.append(f"- Favorite restaurant: {top_restaurant}")

        # Add saved addresses if available (CRITICAL for first message flow)
        saved_addresses = user_profile.get('saved_addresses', [])
        logger.info(f"🏠 Building context - saved_addresses count: {len(saved_addresses)}")
        if saved_addresses:
            default_addr = saved_addresses[0]  # Assume first is default
            logger.info(f"🏠 Default address: {default_addr.get('street')}, {default_addr.get('city')}, {default_addr.get('state')}")
            context_parts.append(f"- ✅ HAS SAVED ADDRESS: {default_addr.get('street')}, {default_addr.get('city')}, {default_addr.get('state')}")
            # Only ask about address if NOT showing reorder prompt
            if not user_profile.get('last_order'):
                context_parts.append(f"  → IMMEDIATELY ASK: 'Great choice! Deliver to {default_addr.get('street')}?' with buttons")
        else:
            logger.info(f"⚠️ No saved addresses in user_profile")
            context_parts.append(f"- ⚠️ NO SAVED ADDRESS: Ask delivery/pickup first, then get address")
    else:
        logger.info(f"❌ No user_profile provided to _build_home_ai_context")
        context_parts.append("- Anonymous user (no profile)")

    # Check conversation state - has user already answered delivery/pickup?
    order_type_confirmed = False
    if conversation_history and len(conversation_history) > 0:
        # Check if user has already answered delivery/pickup
        for msg in conversation_history:
            if msg.get('role') == 'user':
                content_lower = msg.get('content', '').lower()

                # User selected pickup - no address needed
                if any(word in content_lower for word in ['pickup', 'pick up']):
                    order_type_confirmed = True
                    context_parts.append("- ✅ ORDER TYPE CONFIRMED: User selected pickup (no address needed)")
                    break

                # User said "yes deliver" or similar - confirming saved address
                if saved_addresses and any(phrase in content_lower for phrase in ['yes deliver', 'yes,', 'deliver there']):
                    order_type_confirmed = True
                    context_parts.append("- ✅ ORDER TYPE CONFIRMED: User confirmed delivery to saved address")
                    break

                # User said "delivery" but has NO saved address - NOT confirmed yet!
                # They still need to provide an address
                if any(word in content_lower for word in ['delivery', 'deliver']) and not saved_addresses:
                    # Don't mark as confirmed - we still need address
                    logger.info("⚠️ User said 'delivery' but has no saved address - need to collect address first")
                    break

    if not order_type_confirmed and conversation_history and len(conversation_history) > 0:
        context_parts.append("- ⚠️ ORDER TYPE NOT CONFIRMED: Still waiting for delivery/pickup answer or address")

    # Location context
    if location:
        context_parts.append(f"- Current location: {location.get('lat'):.4f}, {location.get('lng'):.4f}")
        if nearby_restaurants:
            context_parts.append(f"- {len(nearby_restaurants)} restaurants nearby")
    else:
        context_parts.append("- No location provided yet - need to ask for address")

    context_string = "\n".join(context_parts)
    logger.info(f"📝 Built home AI context:\n{context_string}")
    return context_string


def _extract_quick_actions(ai_response, user_profile, user_message, location=None, conversation_history=None):
    """Extract or generate quick action buttons based on conversation

    Args:
        ai_response: AI's response text
        user_profile: User profile with order history
        user_message: User's current message
        location: Optional dict with lat/lng for nearby search
        conversation_history: Optional list of previous messages
    """
    ai_lower = ai_response.lower()
    user_lower = user_message.lower()

    # Check if AI is asking to reorder last order
    if 'order again' in ai_lower and '?' in ai_response:
        return ["🔁 Yes, order again", "🔍 Try something new"]

    # Check if AI is asking for address confirmation (saved address flow)
    if 'deliver to' in ai_lower and '?' in ai_response:
        # Extract address from AI response if possible
        if user_profile and user_profile.get('saved_addresses'):
            # New order: Yes → Pickup → Change address
            return ["✓ Yes, deliver there", "🏃 Pickup instead", "📍 Change address"]
        else:
            # Fallback to generic delivery/pickup
            return ["🚗 Delivery", "🏃 Pickup"]

    # Check if AI is asking for delivery/pickup (multiple patterns)
    delivery_pickup_patterns = [
        'delivery or pickup', 'pickup or delivery',
        'would you prefer', 'would you like delivery',
        'delivery pickup', 'pickup delivery'
    ]
    if any(phrase in ai_lower for phrase in delivery_pickup_patterns):
        return ["🚗 Delivery", "🏃 Pickup"]

    # Check if AI is asking about group size
    group_size_patterns = [
        'just you', 'how many', 'group size', 'ordering for',
        'many people', 'people are you', 'party size'
    ]
    if any(phrase in ai_lower for phrase in group_size_patterns):
        return ["👤 Just me", "👥 2-3 people", "👨‍👩‍👧‍👦 Group (4+)"]

    # ===================================================================
    # DISH-FIRST FLOW: Show real dish buttons from nearby restaurants
    # ===================================================================

    # Address is confirmed ONLY if:
    # 1. User has saved addresses AND confirmed them ("yes", "deliver there")
    # 2. User selected "pickup" (no address needed)
    has_saved_address = user_profile and user_profile.get('saved_addresses')
    confirmed_saved_address = has_saved_address and any(phrase in user_lower for phrase in [
        'yes', 'deliver there', 'yes deliver'
    ])
    selected_pickup = any(phrase in user_lower for phrase in ['pick up', 'pickup'])

    address_confirmed = confirmed_saved_address or selected_pickup

    if address_confirmed and location and conversation_history:
        # Parse conversation history to find which cuisine they selected
        cuisine_keywords = {
            'pizza': 'pizza',
            'taco': 'mexican',
            'indian': 'indian',
            'burger': 'burger',
            'chinese': 'chinese',
            'mexican': 'mexican',
            'thai': 'thai',
            'sushi': 'japanese'
        }

        selected_cuisine = None
        for msg in reversed(conversation_history):  # Check recent messages
            if msg.get('role') == 'user':
                msg_lower = msg.get('content', '').lower()
                for keyword, cuisine_type in cuisine_keywords.items():
                    if keyword in msg_lower:
                        selected_cuisine = cuisine_type
                        break
                if selected_cuisine:
                    break

        # If we found a cuisine selection, show real dish buttons
        if selected_cuisine:
            logger.info(f"🍽️ DISH-FIRST: User confirmed address for {selected_cuisine}")

            # Infer user ethnicity from profile for personalization
            user_ethnicity = _infer_user_ethnicity(user_profile) if user_profile else None

            # Get dishes with GEOHASH CACHING (much faster + cost-effective!)
            # This checks AI cache first, then falls back to Google + AI if needed
            all_dishes = _get_ai_dishes_with_geohash_cache(
                cuisine=selected_cuisine,
                user_lat=location['lat'],
                user_lng=location['lng'],
                user_ethnicity=user_ethnicity
            )

            # Return structured dish objects (not just button strings)
            if all_dishes:
                cuisine_emoji_map = {
                    'pizza': '🍕',
                    'mexican': '🌮',
                    'indian': '🍛',
                    'burger': '🍔',
                    'chinese': '🥡',
                    'thai': '🍜',
                    'japanese': '🍣'
                }
                emoji = cuisine_emoji_map.get(selected_cuisine, '🍽️')

                dish_objects = []
                for dish in all_dishes[:4]:  # Max 4 dishes
                    restaurant_id = dish.get('restaurant_id', '')

                    # Check if menu is cached for this restaurant
                    menu_cached = False
                    if restaurant_id:
                        menu_path = os.path.join(settings.BASE_DIR, 'var', 'reviews', restaurant_id, 'menu_structure.json')
                        menu_cached = os.path.exists(menu_path)

                    dish_obj = {
                        'dish_name': dish['name'],
                        'restaurant_name': dish.get('restaurant_name', 'Restaurant'),
                        'restaurant_id': restaurant_id,
                        'place_id': restaurant_id,  # Same as restaurant_id (Google place_id)
                        'price': dish.get('price'),  # Can be None
                        'emoji': emoji,
                        'menu_cached': menu_cached,
                        'rating': dish.get('restaurant_rating'),
                        'distance_miles': dish.get('distance_miles')
                    }
                    dish_objects.append(dish_obj)

                logger.info(f"✅ Returning {len(dish_objects)} structured dish objects for {selected_cuisine} (geohash cached)")
                return dish_objects

    # Check if AI mentioned specific restaurants (show "Order" buttons)
    if any(word in ai_lower for word in ["here's", "here are", "i recommend", "try", "check out"]):
        # User has confirmed order type, show restaurant actions
        return ["🔍 See menu", "💬 Ask about dishes"]

    # Common cuisine quick actions for first-time users
    if user_profile and user_profile.get('is_first_time'):
        return ["🍕 Pizza", "🌮 Tacos", "🍛 Indian", "🍜 Asian", "🍔 Burgers"]

    # Check if AI mentioned specific cuisines
    cuisines_mentioned = []
    cuisine_keywords = {
        'pizza': '🍕 Pizza',
        'taco': '🌮 Tacos',
        'indian': '🍛 Indian',
        'chinese': '🥡 Chinese',
        'thai': '🍜 Thai',
        'mexican': '🌯 Mexican',
        'burger': '🍔 Burgers',
        'sushi': '🍣 Sushi'
    }

    for keyword, action in cuisine_keywords.items():
        if keyword in ai_lower or keyword in user_lower:
            cuisines_mentioned.append(action)

    if cuisines_mentioned:
        return cuisines_mentioned[:4]

    # Default actions based on user profile
    if user_profile and user_profile.get('favorite_cuisines'):
        return ["🔁 Reorder favorite", "🎲 Surprise me", "🔍 Browse nearby"]

    return ["🍕 Pizza", "🌮 Tacos", "🍛 Indian", "💬 Chat with me"]


def _extract_restaurant_quick_actions(ai_response, user_profile, user_message):
    """Extract or generate quick action buttons for restaurant AI chat"""
    ai_lower = ai_response.lower()
    user_lower = user_message.lower()

    # Check if AI is asking to reorder from this restaurant
    if 'order again' in ai_lower and '?' in ai_response:
        return ["🔁 Yes, order again", "🔍 Try something new"]

    # Check if AI is asking for address confirmation (saved address flow)
    if 'deliver to' in ai_lower and '?' in ai_response:
        if user_profile and user_profile.get('saved_addresses'):
            return ["✓ Yes, deliver there", "📍 Change address", "🏃 Pickup instead"]
        else:
            return ["🚗 Delivery", "🏃 Pickup"]

    # Check if AI is asking for delivery/pickup
    delivery_pickup_patterns = [
        'delivery or pickup', 'pickup or delivery',
        'would you prefer', 'would you like delivery',
        'delivery pickup', 'pickup delivery'
    ]
    if any(phrase in ai_lower for phrase in delivery_pickup_patterns):
        return ["🚗 Delivery", "🏃 Pickup"]

    # Check if AI is asking about group size
    group_size_patterns = [
        'just you', 'how many', 'group size', 'ordering for',
        'many people', 'people are you', 'party size'
    ]
    if any(phrase in ai_lower for phrase in group_size_patterns):
        return ["👤 Just me", "👥 2-3 people", "👨‍👩‍👧‍👦 Group (4+)"]

    # Check if AI mentioned specific dishes (ready to order)
    if any(word in ai_lower for word in ["here are", "i recommend", "popular dishes", "top dishes"]):
        return ["🛒 Order these", "💬 Tell me more", "🔄 Show different dishes"]

    # Default actions
    return ["🛒 Ready to order", "💬 Ask about dishes", "📞 Contact info"]


def _extract_preferences_from_conversation(user_message, ai_response, user_profile):
    """
    Extract user preferences from conversation for long-term storage
    Returns dict of preferences to merge with existing profile

    Extracts:
    - Favorite cuisines (mentioned repeatedly)
    - Dietary restrictions (vegetarian, vegan, gluten-free)
    - Order preferences (delivery vs pickup, group size)
    - Budget preferences (cheap eats vs fine dining)
    """
    new_prefs = {}

    # Combine messages for analysis
    text = f"{user_message} {ai_response}".lower()

    # Extract dietary restrictions
    dietary_keywords = {
        'vegetarian': ['vegetarian', 'veggie', 'no meat'],
        'vegan': ['vegan', 'plant-based'],
        'gluten_free': ['gluten free', 'gluten-free', 'celiac'],
        'dairy_free': ['dairy free', 'dairy-free', 'lactose'],
        'halal': ['halal'],
        'kosher': ['kosher']
    }

    for diet_type, keywords in dietary_keywords.items():
        if any(keyword in text for keyword in keywords):
            new_prefs[f'dietary_{diet_type}'] = True

    # Extract cuisine preferences (track frequency)
    cuisine_mentions = {}
    cuisines = ['pizza', 'indian', 'chinese', 'mexican', 'thai', 'italian', 'japanese', 'korean', 'vietnamese']

    for cuisine in cuisines:
        if cuisine in text:
            # Get existing count from profile
            existing_prefs = user_profile.get('preferences', {}) if user_profile else {}
            current_count = existing_prefs.get(f'cuisine_{cuisine}_count', 0)
            cuisine_mentions[f'cuisine_{cuisine}_count'] = current_count + 1

    new_prefs.update(cuisine_mentions)

    # Extract order type preference
    if 'delivery' in text and 'pickup' not in text:
        new_prefs['preferred_order_type'] = 'delivery'
    elif 'pickup' in text and 'delivery' not in text:
        new_prefs['preferred_order_type'] = 'pickup'

    # Extract budget preferences
    if any(word in text for word in ['cheap', 'budget', 'affordable', 'inexpensive']):
        new_prefs['budget_preference'] = 'budget'
    elif any(word in text for word in ['fine dining', 'upscale', 'fancy', 'expensive']):
        new_prefs['budget_preference'] = 'upscale'

    # Extract group size patterns
    if any(phrase in text for phrase in ['just me', 'by myself', 'solo']):
        new_prefs['typical_group_size'] = 1
    elif any(phrase in text for phrase in ['two people', 'for 2', 'couple']):
        new_prefs['typical_group_size'] = 2
    elif any(phrase in text for phrase in ['family', 'group', 'four people', 'for 4']):
        new_prefs['typical_group_size'] = 4

    return new_prefs


def _save_conversation_preferences(user_id, new_preferences):
    """
    Save extracted preferences to UserProfile
    Merges with existing preferences (doesn't overwrite everything)
    """
    try:
        # Get user
        user = User.objects.get(username=user_id)

        # Get or create profile
        profile, created = UserProfile.objects.get_or_create(
            user=user,
            defaults={'phone': '', 'preferences': {}}
        )

        # Merge preferences (new ones override old ones)
        existing_prefs = profile.preferences or {}
        existing_prefs.update(new_preferences)

        profile.preferences = existing_prefs
        profile.save()

        logger.info(f"💾 Saved preferences for {user_id}: {new_preferences}")
        return True

    except Exception as e:
        logger.error(f"Error saving conversation preferences: {e}")
        return False


def _infer_user_ethnicity(user_profile):
    """
    Infer user's ethnicity from order history

    Returns: ethnicity string ('indian', 'mexican', 'chinese', etc.) or None

    Guardrails:
    - First-time users: returns None (no data)
    - Need 3+ orders of same cuisine to infer
    """
    if not user_profile or user_profile.get('is_first_time'):
        return None

    # Count cuisine preferences from order history
    # Format: {'cuisine_pizza_count': 5, 'cuisine_indian_count': 8}
    cuisine_counts = {}
    for key, value in user_profile.items():
        if key.startswith('cuisine_') and key.endswith('_count'):
            cuisine_name = key.replace('cuisine_', '').replace('_count', '')
            cuisine_counts[cuisine_name] = value

    if not cuisine_counts:
        return None

    # Find dominant cuisine (3+ orders threshold)
    dominant_cuisine = max(cuisine_counts.items(), key=lambda x: x[1])
    if dominant_cuisine[1] >= 3:
        # Map cuisine to ethnicity
        ethnicity_mapping = {
            'indian': 'indian',
            'chinese': 'chinese',
            'mexican': 'mexican',
            'thai': 'thai',
            'japanese': 'japanese',
            'korean': 'korean',
            'vietnamese': 'vietnamese',
            'italian': 'italian',
            'greek': 'greek'
        }
        return ethnicity_mapping.get(dominant_cuisine[0])

    return None


def _get_ethnicity_insights_from_cache(place_id, user_ethnicity):
    """
    Get ethnicity-specific dish recommendations from cached review data
    Uses existing CSV-based cache with ethnicity_ui column

    Returns: dict with popular dishes for that ethnicity, or None if no cache

    Guardrails:
    - No cache? Returns None (graceful fallback)
    - No ethnicity insights? Returns None
    """
    if not user_ethnicity:
        return None

    try:
        # Use existing cache loader
        cached_data = _get_cached_restaurant_data(place_id)
        if not cached_data:
            return None  # No cache - can't scrape on-the-fly

        # Filter dishes by user's ethnicity
        ethnic_dishes = [
            dish for dish in cached_data.get('top_dishes', [])
            if dish.get('ethnicity', '').lower() == user_ethnicity.lower()
        ]

        if ethnic_dishes:
            return {
                'popular_dishes': ethnic_dishes[:3],  # Top 3 dishes for this ethnicity
                'has_insights': True
            }

        return None

    except Exception as e:
        logger.warning(f"Error reading ethnicity cache for {place_id}: {e}")
        return None


def _filter_restaurants_for_response(nearby_restaurants, user_message, ai_response, user_profile=None):
    """Filter and rank restaurants based on conversation context + user ethnicity"""
    if not nearby_restaurants:
        return []

    # Check if AI is asking for delivery/pickup (don't show restaurants yet)
    ai_lower = ai_response.lower()
    if any(phrase in ai_lower for phrase in ['delivery or pickup', 'pickup or delivery', 'would you prefer']):
        return []  # Wait for user to answer

    # Check if AI is asking for address confirmation (don't show restaurants yet)
    if 'deliver to' in ai_lower and '?' in ai_response:
        return []  # Wait for user to confirm address

    # Check if AI is asking to reorder (don't show restaurants yet)
    if 'order again' in ai_lower and '?' in ai_response:
        return []  # Wait for user to choose reorder or try new

    # Check if AI is asking about group size (don't show restaurants yet)
    group_size_patterns = [
        'just you', 'how many', 'group size', 'ordering for',
        'many people', 'people are you', 'party size'
    ]
    if any(phrase in ai_lower for phrase in group_size_patterns):
        return []  # Wait for user to answer group size

    # Infer user's ethnicity from order history
    user_ethnicity = _infer_user_ethnicity(user_profile) if user_profile else None

    # Filter by cuisine if mentioned in user message
    user_lower = user_message.lower()
    cuisine_keywords = {
        'pizza': ['pizza', 'pizzeria'],
        'mexican': ['mexican', 'taco', 'burrito'],
        'indian': ['indian', 'curry', 'tandoori'],
        'chinese': ['chinese', 'asian'],
        'italian': ['italian', 'pasta'],
        'burger': ['burger', 'burgers']
    }

    # Find which cuisine user wants
    filtered = []
    requested_cuisine = None

    for cuisine, keywords in cuisine_keywords.items():
        if any(kw in user_lower for kw in keywords):
            requested_cuisine = cuisine
            # Filter restaurants by name/types containing cuisine keywords
            for restaurant in nearby_restaurants:
                name_lower = restaurant.get('name', '').lower()
                types = [t.lower() for t in restaurant.get('cuisine', [])]
                place_id = restaurant.get('place_id')

                if any(kw in name_lower for kw in keywords) or any(kw in ' '.join(types) for kw in keywords):
                    # Enhance with ethnicity insights if available
                    if user_ethnicity and place_id:
                        ethnicity_insights = _get_ethnicity_insights_from_cache(place_id, user_ethnicity)
                        if ethnicity_insights:
                            restaurant['ethnicity_insights'] = ethnicity_insights
                            restaurant['has_ethnicity_match'] = True
                        else:
                            restaurant['has_ethnicity_match'] = False

                    filtered.append(restaurant)

            if filtered:
                # Sort: Restaurants with ethnicity insights first
                filtered.sort(key=lambda r: r.get('has_ethnicity_match', False), reverse=True)
                return filtered[:3]  # Return top 3 matching restaurants
            break

    # Guardrail: If no cuisine match or no filtered results, return top 3 nearby
    return nearby_restaurants[:3]


def _determine_conversation_state(user_message, ai_response, user_profile):
    """Determine current state in conversation flow"""
    # Simple state detection based on keywords
    user_lower = user_message.lower()
    ai_lower = ai_response.lower()

    if any(word in user_lower for word in ['hi', 'hello', 'hey']):
        return 'greeting'

    if any(word in user_lower for word in ['pizza', 'taco', 'indian', 'chinese']):
        return 'cuisine_selected'

    if 'restaurant' in ai_lower or 'recommend' in ai_lower:
        return 'showing_options'

    if 'deliver' in ai_lower or 'location' in ai_lower:
        return 'collecting_location'

    return 'chatting'


def _enrich_restaurants_with_cache_data(nearby_restaurants):
    """
    Enrich nearby restaurants with cached review data (dishes + ethnicity analysis)
    Returns list of restaurants with 'cached_data' field added
    """
    enriched = []

    for restaurant in nearby_restaurants:
        place_id = restaurant.get('place_id')

        # Try to load cached dish data
        cached_data = _get_cached_restaurant_data(place_id)

        enriched.append({
            **restaurant,
            'cached_data': cached_data  # None if no cache, dict if cached
        })

    return enriched


def _get_cached_restaurant_data(place_id, cuisine_type=None):
    """
    Get cached dish mentions and ethnicity analysis for a restaurant
    Returns None if no cache, otherwise dict with dishes and ethnicity insights

    Args:
        place_id: Google Places ID
        cuisine_type: Optional - filter dishes by cuisine type (e.g., 'burger', 'pizza')
    """
    try:
        csv_path = category_csv_path(place_id, "restaurant")

        if not csv_path.exists():
            return None

        # Read cached dish data
        df = pd.read_csv(csv_path, dtype={
            "ethnicity_ui": "string",
            "dish": "string",
            "price": "string",
            "description": "string"
        })

        if df.empty:
            return None

        # Filter by cuisine type if provided (simple substring match)
        filtered_df = df
        if cuisine_type:
            # Look for dishes that contain the cuisine type in the name
            cuisine_matches = df[df['dish'].str.contains(cuisine_type, case=False, na=False)]

            # Use cuisine matches if found, otherwise fall back to all dishes
            if not cuisine_matches.empty:
                filtered_df = cuisine_matches
                logger.info(f"🎯 Found {len(cuisine_matches)} dishes matching '{cuisine_type}' in cache")
            else:
                logger.info(f"⚠️ No dishes with '{cuisine_type}' in name, using top dishes instead")

        # Get top dishes (limit to 5 for brevity)
        top_dishes = []
        for _, row in filtered_df.head(5).iterrows():
            dish_info = {
                'name': row['dish'],
                'mentions': row.get('mention_count', 0),
                'ethnicity': row.get('ethnicity_ui', 'Popular')
            }
            if pd.notna(row.get('price')):
                dish_info['price'] = row['price']
            if pd.notna(row.get('description')):
                dish_info['description'] = row['description']

            top_dishes.append(dish_info)

        # Get ethnicity insights (which ethnic groups love this restaurant)
        ethnicity_insights = []
        if 'ethnicity_ui' in df.columns:
            ethnicity_counts = df['ethnicity_ui'].value_counts().head(3)
            for ethnicity, count in ethnicity_counts.items():
                if ethnicity != 'Popular':
                    ethnicity_insights.append({
                        'ethnicity': ethnicity,
                        'count': int(count)
                    })

        return {
            'has_cache': True,
            'top_dishes': top_dishes,
            'ethnicity_insights': ethnicity_insights,
            'total_dish_mentions': len(df)
        }

    except Exception as e:
        logger.warning(f"Error loading cached data for {place_id}: {e}")
        return None


def _get_geohash(lat, lng, precision=5):
    """
    Simple geohash encoder for geographic grid caching

    Args:
        lat: Latitude
        lng: Longitude
        precision: Number of characters (5 = ~5km cell)

    Returns:
        Geohash string (e.g., "9q9j5")
    """
    base32 = '0123456789bcdefghjkmnpqrstuvwxyz'
    lat_range = [-90.0, 90.0]
    lng_range = [-180.0, 180.0]

    geohash = []
    bits = 0
    bit = 0
    ch = 0

    while len(geohash) < precision:
        if bits % 2 == 0:  # even bit: longitude
            mid = (lng_range[0] + lng_range[1]) / 2
            if lng > mid:
                ch |= (1 << (4 - bit))
                lng_range[0] = mid
            else:
                lng_range[1] = mid
        else:  # odd bit: latitude
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat > mid:
                ch |= (1 << (4 - bit))
                lat_range[0] = mid
            else:
                lat_range[1] = mid

        bits += 1
        bit += 1

        if bit == 5:
            geohash.append(base32[ch])
            bit = 0
            ch = 0

    return ''.join(geohash)


def _calculate_distance(lat1, lng1, lat2, lng2):
    """
    Calculate distance between two points using Haversine formula

    Args:
        lat1, lng1: First point coordinates
        lat2, lng2: Second point coordinates

    Returns:
        Distance in miles
    """
    from math import radians, sin, cos, sqrt, atan2

    # Earth radius in miles
    R = 3959.0

    # Convert to radians
    lat1_rad = radians(lat1)
    lng1_rad = radians(lng1)
    lat2_rad = radians(lat2)
    lng2_rad = radians(lng2)

    # Haversine formula
    dlat = lat2_rad - lat1_rad
    dlng = lng2_rad - lng1_rad

    a = sin(dlat / 2)**2 + cos(lat1_rad) * cos(lat2_rad) * sin(dlng / 2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))

    distance = R * c
    return distance


def _load_ai_dish_cache(cache_key):
    """
    Load cached AI-generated dishes from geohash-based file

    Args:
        cache_key: Format "{cuisine}_{geohash}" (e.g., "pizza_9q9j5")

    Returns:
        Cached data dict or None if not exists/expired
    """
    try:
        cache_dir = Path(settings.BASE_DIR) / "var" / "ai_dish_cache"
        cache_file = cache_dir / f"{cache_key}.json"

        if not cache_file.exists():
            return None

        with open(cache_file, 'r', encoding='utf-8') as f:
            cached_data = json.load(f)

        # Check if expired (90 days)
        cached_time = pd.to_datetime(cached_data.get('cached_at', '1970-01-01'))
        age_days = (pd.Timestamp.now() - cached_time).days

        if age_days >= 90:
            logger.info(f"❌ AI cache EXPIRED for {cache_key} (age: {age_days} days)")
            return None

        logger.info(f"✅ AI cache HIT for {cache_key} (age: {age_days} days, {len(cached_data.get('dishes', []))} dishes)")
        return cached_data

    except Exception as e:
        logger.warning(f"Error loading AI dish cache for {cache_key}: {e}")
        return None


def _save_ai_dish_cache(cache_key, data):
    """
    Save AI-generated dishes to geohash-based cache file

    Args:
        cache_key: Format "{cuisine}_{geohash}"
        data: Dict with cuisine, geohash, center_location, dishes
    """
    try:
        cache_dir = Path(settings.BASE_DIR) / "var" / "ai_dish_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        cache_file = cache_dir / f"{cache_key}.json"

        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.info(f"💾 Saved AI cache for {cache_key} ({len(data.get('dishes', []))} dishes)")

    except Exception as e:
        logger.warning(f"Error saving AI dish cache for {cache_key}: {e}")


def _get_ai_dishes_with_geohash_cache(cuisine, user_lat, user_lng, user_ethnicity=None):
    """
    Get AI-recommended dishes with geohash-based caching (90 day TTL)

    This function provides smart geographic caching:
    - Users in same ~5km area share AI recommendations
    - First user pays for AI call, next 1000+ users get FREE instant results
    - Filters results by 5-mile delivery radius from user's exact location
    - Falls back to fresh AI call if cache miss or insufficient dishes

    Args:
        cuisine: Cuisine type (pizza, indian, etc.)
        user_lat: User's latitude
        user_lng: User's longitude
        user_ethnicity: Optional ethnicity for personalization

    Returns:
        List of dish dicts with restaurant context
    """
    # 1. Get geohash for user's location (precision 5 = ~5km cell)
    user_geohash = _get_geohash(user_lat, user_lng, precision=5)

    # 2. Check AI cache for this geohash + cuisine
    cache_key = f"{cuisine}_{user_geohash}"
    cached_data = _load_ai_dish_cache(cache_key)

    if cached_data:
        # 3. Filter cached dishes within 5 miles of USER's exact location
        nearby_dishes = []
        for dish in cached_data.get('dishes', []):
            distance = _calculate_distance(
                user_lat, user_lng,
                dish.get('restaurant_lat', 0),
                dish.get('restaurant_lng', 0)
            )

            if distance <= 5:  # 5 mile delivery radius
                dish['distance_miles'] = round(distance, 1)
                nearby_dishes.append(dish)

        # Need at least 3 dishes for good UX
        if len(nearby_dishes) >= 3:
            logger.info(f"✅ Returning {len(nearby_dishes)} cached AI dishes for {cuisine} in {user_geohash}")
            return nearby_dishes[:4]  # Max 4 for buttons
        else:
            logger.info(f"⚠️ Cache has only {len(nearby_dishes)} dishes within 5 miles, need fresh data")

    # 4. No cache or insufficient dishes → Call Google + AI
    logger.info(f"❌ AI cache MISS for {cache_key}, calling Google + AI")

    # Find good restaurants nearby
    good_restaurants = _find_good_restaurants_by_cuisine(
        cuisine=cuisine,
        location={'lat': user_lat, 'lng': user_lng},
        limit=5  # Get more restaurants for better caching
    )

    if not good_restaurants:
        logger.warning(f"No good {cuisine} restaurants found near ({user_lat}, {user_lng})")
        return []

    # 5. Get dishes from each restaurant (HYBRID: Cache first, AI fallback)
    all_dishes = []
    for restaurant in good_restaurants:
        # NEW: Try to get cached dishes first (real menu data!)
        cached_dishes = _get_dishes_for_restaurant(
            place_id=restaurant['place_id'],
            restaurant_name=restaurant['name'],
            cuisine_type=cuisine,
            user_ethnicity=user_ethnicity,
            limit=1,  # 1 dish per restaurant
            skip_scraping=True  # DON'T queue scraping during discovery phase
        )

        # Check if we got real cached dishes (not AI fallback)
        # _get_dishes_for_restaurant marks cached dishes with 'from_cache': True
        dishes = cached_dishes  # Use whatever it returned (cache or AI)

        # Add restaurant context and location to all dishes
        for dish in dishes:
            # These fields might already exist from cache, but ensure they're set
            dish['restaurant_name'] = restaurant['name']
            dish['restaurant_id'] = restaurant['place_id']
            dish['restaurant_rating'] = restaurant['rating']
            dish['restaurant_lat'] = restaurant.get('lat', user_lat)
            dish['restaurant_lng'] = restaurant.get('lng', user_lng)

            # Calculate distance from user
            if 'lat' in restaurant and 'lng' in restaurant:
                distance = _calculate_distance(
                    user_lat, user_lng,
                    restaurant['lat'], restaurant['lng']
                )
                dish['distance_miles'] = round(distance, 1)

            all_dishes.append(dish)

            # Log what we're using for visibility
            if dish.get('from_cache'):
                logger.info(f"   ✅ Using CACHED dish '{dish['name']}' from {restaurant['name']}")
            elif dish.get('from_ai'):
                logger.info(f"   🤖 Using AI-generated dish '{dish['name']}' from {restaurant['name']}")
            else:
                logger.info(f"   ❓ Using dish '{dish['name']}' from {restaurant['name']} (unknown source)")

        # ❌ REMOVED: No longer enqueue scraping upfront for all 5 restaurants
        # Scraping is now triggered on-demand when user actually selects a dish (via fetchMenuStructure)
        # This avoids wasting 4 scraping jobs (4 × 60s = 4 min wait) for restaurants user won't order from

    # 6. Save to geohash cache (3 month expiry)
    cache_data = {
        'cuisine': cuisine,
        'geohash': user_geohash,
        'center_location': {
            'lat': user_lat,
            'lng': user_lng
        },
        'cached_at': pd.Timestamp.now().isoformat(),
        'expires_at': (pd.Timestamp.now() + pd.Timedelta(days=90)).isoformat(),
        'dishes': all_dishes
    }
    _save_ai_dish_cache(cache_key, cache_data)

    logger.info(f"✅ Generated {len(all_dishes)} AI dishes for {cuisine} in {user_geohash}")
    return all_dishes[:4]  # Max 4 for buttons


def _ask_ai_for_popular_dishes(restaurant_name, cuisine_type, ethnicity=None, limit=3):
    """
    Ask AI for popular dishes at a restaurant when we don't have cached data
    This is a fast fallback (~200ms) instead of waiting for scraping

    Args:
        restaurant_name: Name of the restaurant
        cuisine_type: Type of cuisine (pizza, indian, chinese, etc.)
        ethnicity: Optional - filter by ethnicity (indian, chinese, etc.)
        limit: Number of dishes to return (default 3)

    Returns:
        List of dish dicts: [{'name': 'Margherita Pizza', 'price': None, 'ethnicity': 'Popular'}]
    """
    try:
        ethnicity_filter = f" popular among {ethnicity} people" if ethnicity else ""

        prompt = f"""List the {limit} most popular {cuisine_type} dishes at {restaurant_name}{ethnicity_filter}.

Return ONLY a JSON array with this exact format:
[
  {{"name": "Dish Name", "description": "Brief 3-5 word description"}},
  ...
]

Rules:
- Use actual dish names if you know them (e.g., "Tony's Special Pizza" for Tony's Pizza)
- Include cuisine-specific variations (e.g., Tikka Pizza for Indian, Bulgogi Pizza for Korean)
- Keep descriptions very short (3-5 words max)
- Return ONLY the JSON array, no other text"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a food recommendation expert. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,  # Lower temperature for consistent results
            max_tokens=200
        )

        ai_response = response.choices[0].message.content.strip()

        # Parse JSON response
        dishes_json = json.loads(ai_response)

        # Format to match our cache structure
        dishes = []
        for dish in dishes_json[:limit]:
            dishes.append({
                'name': dish['name'],
                'description': dish.get('description'),
                'price': None,  # AI doesn't know price
                'ethnicity': ethnicity if ethnicity else 'Popular',
                'from_ai': True  # Flag to know this is AI-generated
            })

        logger.info(f"🤖 AI generated {len(dishes)} dishes for {restaurant_name}: {[d['name'] for d in dishes]}")
        return dishes

    except Exception as e:
        logger.error(f"Error asking AI for dishes at {restaurant_name}: {e}")
        # Fallback to generic dishes
        generic_dishes = {
            'pizza': ['Margherita Pizza', 'Pepperoni Pizza', 'Veggie Pizza'],
            'indian': ['Butter Chicken', 'Tikka Masala', 'Biryani'],
            'chinese': ['Fried Rice', 'Chow Mein', 'Kung Pao Chicken'],
            'mexican': ['Tacos', 'Burrito', 'Enchiladas'],
            'burger': ['Cheeseburger', 'Bacon Burger', 'Veggie Burger']
        }

        fallback = generic_dishes.get(cuisine_type.lower(), ['Special Dish', 'House Special', 'Chef\'s Choice'])
        return [{'name': name, 'description': None, 'price': None, 'ethnicity': 'Popular', 'from_ai': True} for name in fallback[:limit]]


def _get_dishes_for_restaurant(place_id, restaurant_name, cuisine_type, user_ethnicity=None, limit=3, skip_scraping=False):
    """
    Get popular dishes for a restaurant - tries cache first, falls back to AI

    Args:
        place_id: Google Places ID
        restaurant_name: Restaurant name
        cuisine_type: Type of cuisine (pizza, indian, etc.)
        user_ethnicity: Optional - user's ethnicity for filtering
        limit: Number of dishes to return
        skip_scraping: If True, don't queue scraping jobs (for discovery phase)

    Returns:
        List of dish dicts with 'name', 'price', 'ethnicity', 'from_cache' or 'from_ai'
    """
    # Try cache first - now with cuisine filtering
    cached_data = _get_cached_restaurant_data(place_id, cuisine_type=cuisine_type)

    if cached_data and cached_data.get('top_dishes'):
        logger.info(f"✅ Using cached dishes for {restaurant_name}")
        dishes = cached_data['top_dishes'][:limit]

        # Filter by ethnicity if requested and available
        if user_ethnicity:
            ethnicity_dishes = [d for d in dishes if d.get('ethnicity', '').lower() == user_ethnicity.lower()]
            if ethnicity_dishes:
                dishes = ethnicity_dishes[:limit]

        # Mark as from cache
        for dish in dishes:
            dish['from_cache'] = True

        return dishes

    # No cache - use AI fallback
    logger.info(f"❌ No cache for {restaurant_name}, asking AI for popular {cuisine_type} dishes")
    ai_dishes = _ask_ai_for_popular_dishes(restaurant_name, cuisine_type, user_ethnicity, limit)

    # Background: Enqueue scraping for next time (unless skip_scraping=True for discovery)
    if not skip_scraping:
        try:
            ensure_csv_async(place_id, fast=False, category="restaurant")
            logger.info(f"📥 Enqueued scraping for {restaurant_name} (place_id: {place_id})")
        except Exception as e:
            logger.warning(f"Failed to enqueue scraping for {place_id}: {e}")
    else:
        logger.info(f"⏭️ Skipping scraping queue for {restaurant_name} (discovery phase)")

    return ai_dishes


def _find_good_restaurants_by_cuisine(cuisine, location, limit=3):
    """
    Find top-rated, currently open restaurants for a cuisine type near user's location

    Args:
        cuisine: Type of cuisine (pizza, indian, chinese, etc.)
        location: Dict with 'lat' and 'lng' keys
        limit: Number of restaurants to return (default 3)

    Returns:
        List of restaurant dicts with 'place_id', 'name', 'rating', 'open_now'
    """
    if not location:
        logger.warning("No location provided for restaurant search")
        return []

    try:
        gmaps = googlemaps.Client(key=settings.GOOGLE_API_KEY)

        # Build search query
        search_query = f"{cuisine} restaurant"

        # Search nearby places
        places_result = gmaps.places_nearby(
            location=(location.get('lat'), location.get('lng')),
            radius=5 * 1609.34,  # 5 miles in meters
            keyword=search_query,
            type='restaurant',
            open_now=True  # Only open restaurants
        )

        restaurants = []
        for place in places_result.get('results', []):
            # Filter by rating (≥4.0)
            rating = place.get('rating', 0)
            if rating < 4.0:
                continue

            # Extract location from geometry
            geometry = place.get('geometry', {})
            location_data = geometry.get('location', {})

            restaurants.append({
                'place_id': place.get('place_id'),
                'name': place.get('name'),
                'rating': rating,
                'open_now': place.get('opening_hours', {}).get('open_now', False),
                'vicinity': place.get('vicinity', ''),
                'lat': location_data.get('lat'),  # Add latitude
                'lng': location_data.get('lng')   # Add longitude
            })

        # Check which restaurants have cached dishes (prioritize these!)
        for r in restaurants:
            cached_data = _get_cached_restaurant_data(r['place_id'])
            r['has_cache'] = cached_data is not None and bool(cached_data.get('top_dishes'))

        # Sort: Cached restaurants FIRST, then by rating
        # This ensures we show restaurants with real menu data over AI-generated dishes
        restaurants.sort(key=lambda r: (
            not r.get('has_cache', False),  # False (has cache) sorts before True (no cache)
            -r['rating']                     # Higher rating first within each group
        ))

        cached_count = sum(1 for r in restaurants if r.get('has_cache'))
        logger.info(f"🔍 Found {len(restaurants)} good {cuisine} restaurants near user ({cached_count} with cache, showing top {limit})")
        return restaurants[:limit]

    except Exception as e:
        logger.error(f"Error finding {cuisine} restaurants: {e}")
        return []


def _format_nearby_restaurants_for_ai(restaurants_with_data):
    """
    Format enriched restaurants into readable text for AI prompt
    Highlights which ones have cached data vs need AI brain
    """
    if not restaurants_with_data:
        return "No nearby restaurants found. Ask user for their location or expand search radius."

    lines = []

    for i, r in enumerate(restaurants_with_data[:10], 1):  # Limit to 10
        name = r.get('name', 'Unknown')
        rating = r.get('rating', 0)
        cached = r.get('cached_data')

        # Header with cache status
        if cached and cached.get('has_cache'):
            status = "✅ ANALYZED"
            lines.append(f"{i}. **{name}** ({rating}★) - {status}")

            # Add top dishes
            dishes = cached.get('top_dishes', [])
            if dishes:
                dish_list = ", ".join([
                    f"{d['name']}" + (f" (${d['price']})" if d.get('price') else "")
                    for d in dishes[:3]
                ])
                lines.append(f"   Popular: {dish_list}")

            # Add ethnicity insights
            insights = cached.get('ethnicity_insights', [])
            if insights:
                insight_text = ", ".join([f"{e['ethnicity']}s" for e in insights[:2]])
                lines.append(f"   Loved by: {insight_text}")
        else:
            status = "⚠️ No review data"
            lines.append(f"{i}. **{name}** ({rating}★) - {status} (use general knowledge)")

        lines.append("")  # Blank line between restaurants

    return "\n".join(lines)


