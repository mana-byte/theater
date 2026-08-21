"""Daemon server runtime: socket transport, maintenance, and lifecycle.

Split from server.py for cohesion. The Daemon class in server.py composes
these modules; all public/compatibility surfaces remain on server.py.
"""
