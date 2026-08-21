"""FastAPI web layer for the Chess Review Coach interface.

All routes import the process-wide engine pool and review session from ``server.core``.
The Web server is the primary entry point; the optional MCP server reuses the same modules.
"""
