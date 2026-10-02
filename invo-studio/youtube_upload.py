"""YouTube official Data API upload. No browser automation or paid AI API."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def request(url, *, method="GET", headers=None, data=None):
    try:
        with urlopen(Request(url, data=data, method=method, headers=headers or {}), timeout=900) as response:
            return response.status, response.headers, response.read()
    except HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"YouTube HTTP {error.code}: {detail}") from error


def token():
    required = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing " + ", ".join(missing))
    encoded = urlencode({"client_id": os.environ["YOUTUBE_CLIENT_ID"],
                         "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
                         "refresh_token": os.environ["YOUTUBE_REFRESH_TOKEN"],
                         "grant_type": "refresh_token"}).encode()
    _, _, body = request("https://oauth2.googleapis.com/token", method="POST",
                         headers={"Content-Type": "application/x-www-form-urlencoded"}, data=encoded)
    return json.loads(body)["access_token"]


def upload(path: Path, *, title: str, description: str, privacy: str):
    if privacy not in {"public", "private"}:
        raise ValueError("privacy must be public or private")
    access = token()
    size = path.stat().st_size
    metadata = json.dumps({"snippet": {"title": title[:100], "description": description, "categoryId": "22"},
                           "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}).encode()
    _, headers, _ = request(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        method="POST", headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json; charset=UTF-8",
                                 "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"}, data=metadata)
    location = headers.get("Location")
    if not location or not location.startswith("https://www.googleapis.com/"):
        raise RuntimeError("YouTube did not return a trusted upload session")
    # Standard library reads the file for the PUT. Rendered Shorts are expected to be short.
    # A failed/uncertain request is never automatically repeated by pipeline.py.
    _, _, body = request(location, method="PUT",
                         headers={"Authorization": f"Bearer {access}", "Content-Type": "video/mp4",
                                  "Content-Length": str(size)}, data=path.read_bytes())
    video_id = json.loads(body).get("id")
    if not video_id:
        raise RuntimeError("Upload returned no video ID")
    return video_id


def verify(video_id: str):
    access = token()
    _, _, body = request("https://www.googleapis.com/youtube/v3/videos?" +
                         urlencode({"part": "status,processingDetails", "id": video_id}),
                         headers={"Authorization": f"Bearer {access}"})
    items = json.loads(body).get("items", [])
    if not items:
        return "unknown"
    item = items[0]
    privacy = item.get("status", {}).get("privacyStatus", "unknown")
    processing = item.get("processingDetails", {}).get("processingStatus", "unknown")
    if processing in {"failed", "terminated"}:
        return "processing_failed"
    if processing != "succeeded":
        return "processing"
    return privacy
