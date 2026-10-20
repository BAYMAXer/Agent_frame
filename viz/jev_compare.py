"""Compatibility router; implementation lives in the portable comparison package.

Standalone: python server.py
Embedded: copy comparison/ alongside viz/, then include this router and serve
viz/static/jev-compare.html, jev-compare.css and jev-compare.js at /static/.
"""
if __package__ and '.' in __package__:
    from ..comparison.service import router
else:
    from comparison.service import router
