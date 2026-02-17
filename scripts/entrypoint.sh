#!/bin/sh
set -e

echo "Running migrations..."
python -m flask --app app.py db upgrade

echo "Starting app..."
exec "$@"