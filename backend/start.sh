#!/bin/sh
# Local dev: all config comes from backend/.env
cd "$(dirname "$0")" && exec .venv/bin/uvicorn main:app --port 8000 --reload --env-file .env
