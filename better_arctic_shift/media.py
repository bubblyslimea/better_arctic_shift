"""keep one display candidate for each original attachment"""

import html
import re
from urllib.parse import urlparse

from .model import digest


def candidates(record):
    choices = {}

    def add(url, asset=None):
        if not isinstance(url, str):
            return
        url = html.unescape(url)
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            return
        ext = parsed.path.rsplit('.', 1)[-1].lower()
        kind = 'video' if ext in {'mp4', 'webm', 'mov'} else 'image'
        giphy = re.search(r'/media/(?:[^/]+/)*([^/]+)/[^/]+$', parsed.path) if parsed.hostname.endswith('.giphy.com') else None
        key = 'giphy|' + giphy[1] if giphy else asset or (parsed.path.rsplit('/', 1)[-1].rsplit('.', 1)[0] if parsed.hostname in {'i.redd.it', 'preview.redd.it'} else parsed.hostname + parsed.path)
        if key not in choices:
            choices[key] = {'id': digest(key), 'url': url, 'label': 'saved attachment link', 'kind': kind,
                            'role': 'original', 'status': 'pending', 'path': None, 'entity_ids': []}

    metadata = record.get('media_metadata') or {}
    for key, media in metadata.items():
        original = media.get('s') or {}
        add(original.get('gif') or original.get('u') or original.get('mp4'), key)
    video = (record.get('secure_media') or record.get('media') or {}).get('reddit_video') or {}
    add(video.get('fallback_url'))
    url = record.get('url_overridden_by_dest') or record.get('url') or ''
    if re.search(r'\.(?:png|jpe?g|gif|webp|avif|mp4|webm)(?:[?#]|$)', url, re.I):
        add(url)
    if not choices:
        for preview in (record.get('preview') or {}).get('images', []):
            variants = preview.get('variants') or {}
            add((variants.get('gif') or variants.get('mp4') or preview).get('source', {}).get('url'))
    text = record.get('body') or record.get('selftext') or ''
    for gid in re.findall(r'!\[[^\]]*\]\(giphy\|([A-Za-z0-9_-]+)\)', text):
        add(f'https://media.giphy.com/media/{gid}/giphy.gif', 'giphy|' + gid)
    for found in re.findall(r'https?://[^\s<>"\)]+', text):
        if re.search(r'\.(?:png|jpe?g|gif|webp|avif|mp4|webm)(?:[?#]|$)', found, re.I):
            add(found)
    return list(choices.values())
