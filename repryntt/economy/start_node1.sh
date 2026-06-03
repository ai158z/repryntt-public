#!/bin/bash
# Start Node 1 (seed) in foreground with logs

cd "$(dirname "$0")"

echo "Starting Node 1 (seed) on port 5001..."
python3 qnode2.py --port 5001 --host 0.0.0.0 --foreground
