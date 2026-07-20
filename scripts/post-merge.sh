#!/bin/bash
set -e
pnpm install --frozen-lockfile
pnpm --filter db push
# Install Python dependencies for the UC browser checker
pip install -r artifacts/api-server/requirements.txt --quiet
