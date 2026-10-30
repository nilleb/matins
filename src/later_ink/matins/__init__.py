"""Matins: what this fork adds on top of later.ink.

later.ink turns a read-it-later queue into an OPDS catalogue. Matins turns
several sources — that queue, an RSS subscription list, and a built-in save-for-
later list — into a dated, composed issue a reader can pull and read offline.

Nothing in this package is imported by upstream modules except the routes and
the tenant factory wired in `later_ink.main`, so upstream releases stay
mergeable.
"""
