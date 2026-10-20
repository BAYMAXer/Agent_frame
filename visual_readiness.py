"""Conservative PDF render heuristics; readiness is never task completion."""
from urllib.parse import urlsplit
from .browser_feedback import observation_url


def is_pdf_observation(obs):
    if obs is None or (obs.info or {}).get('active_surface') == 'desktop':
        return False
    info = obs.info or {}
    blocks = [b for b in obs.context if b.get('kind') == 'browser_ax']
    try:
        path = urlsplit(observation_url(obs)).path.lower()
    except ValueError:
        path = ''
    return any(b.get('document_kind') in {'pdf', 'pdf_viewer'} or 'application/pdf' in str(b.get('content_type', ''))
               for b in [info, *blocks]) or '/pdf/' in path or path.endswith('.pdf')


def content_thumbnail(screenshot):
    """Ignore browser/PDF toolbars; uniform black, white and gray are not ready."""
    try:
        from PIL import Image, ImageStat
        from io import BytesIO
        import base64
        if isinstance(screenshot, str) and screenshot.startswith('data:'):
            screenshot = Image.open(BytesIO(base64.b64decode(screenshot.split(',', 1)[1])))
        elif isinstance(screenshot, bytes):
            screenshot = Image.open(BytesIO(screenshot))
        elif isinstance(screenshot, str):
            screenshot = Image.open(screenshot)
        width, height = screenshot.size
        if width < 20 or height < 20:
            return None
        crop = screenshot.convert('L').crop((int(width * .15), int(height * .2), int(width * .85), int(height * .85))).resize((96, 96))
        return crop if ImageStat.Stat(crop).stddev[0] >= 3 else None
    except Exception:
        return None


def stable_content(previous, current):
    if previous is None or current is None:
        return False
    from PIL import ImageChops, ImageStat
    return ImageStat.Stat(ImageChops.difference(previous, current)).mean[0] <= 1.5


def likely_loading_page(obs):
    """Select only sparse, visibly unfinished browser pages for fast polling."""
    if (obs is None or obs.screenshot is None or (obs.info or {}).get('active_surface') == 'desktop'
            or is_pdf_observation(obs) or observation_url(obs) in {'', 'about:blank'}):
        return False
    nodes = [n for block in obs.context if block.get('kind') == 'browser_ax'
             for n in block.get('nodes', []) if isinstance(n, dict)]
    names = [str(n.get('name') or '').strip().lower() for n in nodes if n.get('name')]
    if any(term in name for name in names for term in ('rate exceeded', 'not a bot', 'access denied', 'captcha')):
        return False
    if len(names) > 5:
        return False
    if any('loading' in name or '加载中' in name or 'please wait' in name for name in names):
        return True
    if content_thumbnail(obs.screenshot) is not None:
        return False
    try:
        from PIL import ImageStat
        return ImageStat.Stat(obs.screenshot.convert('L')).mean[0] < 55
    except (AttributeError, ValueError):
        return False
