#!/bin/sh
# `claude` stand-in for the CI container: runs the scripted fake under the image's python.
exec /usr/local/bin/python -I /opt/fake-claude/fake_claude.py "$@"
