"""Telegram bridge (remote chat with Great Sage).

The laptop stays home and online; the operator messages a Telegram bot from
anywhere with internet. Standard library only (Bot HTTP API via urllib).
The bot token lives in the environment (never files, logs, or events) and
only allowlisted chat ids get replies — strangers are ignored silently.
"""
