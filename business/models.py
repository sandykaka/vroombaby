from django.db import models

class ZoomMeeting(models.Model):
    zoom_id = models.BigIntegerField()  # Meeting ID returned by Zoom
    topic = models.CharField(max_length=255)
    join_url = models.URLField()
    start_time = models.DateTimeField()
    duration = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.topic} at {self.start_time}"
