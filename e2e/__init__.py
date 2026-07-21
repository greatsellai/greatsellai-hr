"""Local-only end-to-end test support.

This package is never imported by the production ASGI entry point.  The
Playwright launcher starts ``e2e.playwright_app`` explicitly with an isolated SQLite
database and upload directory.
"""
