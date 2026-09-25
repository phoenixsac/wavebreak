"""Thin, stdlib-only HTTP clients for the Wavebreak backend services.

Sub-modules (hawkBit, observability, lab) wrap plain REST APIs with small,
typed Python functions. They are designed to be imported by a build-day
agent and exposed as tools, so each call is a simple, side-effect-free
request/response with explicit error types.
"""
