"""Preferences for a newly allocated task-owned Chrome profile only."""
from __future__ import annotations

import json
from pathlib import Path


def prepare_pdf_download_profile(profile):
    """Use Chrome's own PDF download behavior for an explicit download task.

    This avoids the extension viewer and native Save dialog. Transfer still
    starts from the policy's browser action and is observed through browser
    download events; this helper never retrieves a document.
    """
    directory = Path(profile) / 'Default'
    directory.mkdir(exist_ok=True)
    preferences = directory / 'Preferences'
    # Launchers call this immediately after mkdtemp, before starting Chrome.
    # Refuse to overwrite a profile that could already belong to a user.
    with preferences.open('x', encoding='utf-8') as output:
        json.dump({'plugins': {'always_open_pdf_externally': True}}, output)
