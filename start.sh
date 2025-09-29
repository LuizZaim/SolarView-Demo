#!/usr/bin/env bash
gunicorn --timeout 120 --bind 0.0.0.0:5001 dashboard_server:app