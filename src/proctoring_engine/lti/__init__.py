"""LTI 1.3 ingestion package.

Converts an LTI third-party-initiated login into a
:class:`proctoring_engine.models.ExamSession` row and issues a
short-lived signed session token for the WebSocket layer (next atomic
layer). See ``docs/02-ingestion-layer-design.md`` §1.
"""
