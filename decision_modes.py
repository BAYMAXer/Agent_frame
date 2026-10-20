"""Select a policy prompt using fresh Jev evidence, with capability checks.

Modes describe the primary targeting method. Browser GUI pages can still expose
real AX controls; the executor keeps validating and routing those references.
New execution modes (such as CODE) must explicitly register their capabilities
here before they can enter a policy menu.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import re

from .scene import browser_context, observation_scene


@dataclass(frozen=True)
class DecisionMode:
    mode: str
    surface: str
    reason: str
    source: str = "fallback"
    fallback_reason: str = ""

    def to_dict(self):
        return asdict(self)


RECOVERY_ACTIONS = frozenset({"observe_desktop", "observe_browser", "wait", "request_finish", "fail"})
MODE_ACTIONS = {
    "cli": frozenset({"cli", "observe_browser", "observe_desktop"}),
    "dom": frozenset({"click", "type", "press", "hotkey", "scroll", "goto", "select",
                      "list_tabs", "get_page_source", "run_browser_js", "double_click", "right_click"}),
    "gui": frozenset({"click", "double_click", "right_click", "type", "press", "hotkey",
                      "scroll", "select", "drag_and_drop", "switch_applications", "run_browser_js"}),
    "observe": frozenset(),
    "dialog": frozenset({"handle_dialog", "list_tabs", "observe_desktop", "observe_browser", "fail"}),
}


def mode_evidence_key(task, obs, step=None):
    """Bind a judgment to the exact screenshot, AX, surface and current goal.

    Capture timestamps and exposed_refs are intentionally excluded: compiling a
    prompt must not invalidate its own judgment. A changed frame, page, AX or
    queue goal does invalidate it, even when the URL stays the same.
    """
    info = getattr(obs, 'info', None) or {}
    payload = {'task': task, 'step': {k: (step or {}).get(k) for k in
               ('id', 'goal', 'expected_evidence', 'phase', 'verification')},
               'context': getattr(obs, 'context', None), 'text': getattr(obs, 'text', ''),
               'info': {k: info.get(k) for k in ('active_surface', 'screenshot_space', 'page_id',
                        'document_id', 'snapshot_id', 'url', 'dialog', 'cli_capability', 'screenshot_stale',
                        'observation_error', 'scene')}}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                       default=str).encode('utf-8'))
    shot = getattr(obs, 'screenshot', None)
    if shot is not None:
        from .model.decision_model import encode_image
        digest.update(encode_image(shot).encode('utf-8'))
    return digest.hexdigest()


def mode_capability(obs, step=None):
    """Only hard surface/capability constraints; no page-density heuristics."""
    info = (getattr(obs, "info", None) or {})
    cli = info.get('cli_capability') or {}
    if cli.get('available') and ('cli' in ((step or {}).get('actions') or []) or (step or {}).get('mode') == 'cli' or info.get('active_surface') == 'terminal'):
        return DecisionMode('cli', 'terminal', 'planned_background_operation', 'capability')
    scene = observation_scene(obs)
    desktop = info.get("active_surface") == "desktop" or info.get("screenshot_space") == "screen_pixels"
    blocks = browser_context(obs)
    browser = not desktop and (scene.get("browser_use") == 1 or bool(blocks)
                              or info.get("active_surface") == "browser"
                              or info.get("screenshot_space") == "css_viewport")
    surface = "desktop" if desktop else "browser" if browser else "unknown"
    if info.get("dialog"):
        return DecisionMode("dialog", surface, "native_dialog", "capability")
    if (getattr(obs, "screenshot", None) is None or info.get("screenshot_stale")
            or (info.get("observation_error") and not desktop)):
        # A fresh desktop capture can carry a separate CDP observation error;
        # that error must not prevent handling a native save/permission window.
        return DecisionMode("observe", surface, "observation_unavailable", "capability")
    if desktop:
        return DecisionMode("gui", surface, "desktop_surface", "capability")
    if browser:
        # Dragging has no AX executor. Choose viewport grounding up front when
        # the current goal explicitly requires this gesture, even on an AX page.
        goal = str((step or {}).get("goal") or "")
        if re.search(r"拖拽|拖动|拖放|拖到|\bdrag\b", goal, re.I):
            return DecisionMode("gui", surface, "visual_gesture_required", "capability")
    if not browser:
        return DecisionMode("gui", surface, "visual_observation", "capability")
    return None


def decision_mode(obs, step=None, *, judgment=None, fallback_reason="jev_not_available"):
    """Use a current Jev label first; statistics are only an explicit fallback.

    Callers must validate the evidence binding before passing judgment. A DOM
    choice still requires current enabled AX controls; the exact selected ref
    is independently validated by the executor after policy inference.
    """
    hard = mode_capability(obs, step)
    if hard is not None:
        return hard
    info = getattr(obs, 'info', None) or {}
    scene = observation_scene(obs)
    blocks = browser_context(obs)
    snapshot = info.get('snapshot_id')
    if snapshot:
        blocks = [b for b in blocks if b.get('snapshot_id') == snapshot]
    actionable = any(n.get('direct') and not (n.get('states') or {}).get('disabled')
                     for b in blocks for n in b.get('nodes', []))
    label = (judgment or {}).get('label')
    if label == 'cli' and info.get('cli_capability', {}).get('available'):
        return DecisionMode('cli', 'terminal', 'jev_background_operation', 'jev')
    if label == 'gui':
        return DecisionMode('gui', 'browser', 'jev_visual_target', 'jev')
    if label == 'dom' and actionable:
        return DecisionMode('dom', 'browser', 'jev_target_has_dom', 'jev')
    if label == 'dom':
        return DecisionMode('gui', 'browser', 'no_actionable_ax', 'capability',
                            'jev_dom_without_current_ax')
    if judgment:
        fallback_reason = 'jev_unknown'
    if scene.get('browser_use') == 1 or blocks or info.get('active_surface') == 'browser' or info.get('screenshot_space') == 'css_viewport':
        if scene.get("mode") == "browser_gui":
            return DecisionMode("gui", 'browser', scene.get("reason") or "visual_page_content",
                                'fallback', fallback_reason)
        # An AX root or only static text is not an actionable DOM surface.
        if actionable:
            return DecisionMode("dom", 'browser', "actionable_ax", 'fallback', fallback_reason)
        return DecisionMode("gui", 'browser', "no_actionable_ax", 'fallback', fallback_reason)
    return DecisionMode("gui", 'unknown', "visual_observation", 'fallback', fallback_reason)
