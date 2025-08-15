from django.db import models

class ZoomMeeting(models.Model):
    zoom_id = models.BigIntegerField()  # Meeting ID returned by Zoom
    topic = models.CharField(max_length=255)
    join_url = models.URLField()
    start_time = models.DateTimeField()
    duration = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    host_name = models.CharField(max_length=255, null=True, blank=True)
    host_email = models.EmailField(null=True, blank=True)
    linkedin_profile_url = models.URLField(null=True, blank=True)
    linkedin_profile_picture = models.URLField(null=True, blank=True)

    def __str__(self):
        return f"{self.topic} at {self.start_time}"


class Review(models.Model):
    place_id = models.CharField(max_length=80, db_index=True)
    review_id = models.CharField(max_length=80, unique=True)
    author_name = models.CharField(max_length=200)
    rating = models.FloatField()
    time_text = models.CharField(max_length=100)  # e.g. “2 weeks ago”
    text = models.TextField()

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.author_name} ({self.rating})"
