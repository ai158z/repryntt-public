#!/bin/bash
# repryntt Blockchain Node — Firewall Setup
# Run with: sudo bash scripts/setup_firewall.sh

set -e

echo "=== repryntt Firewall Setup ==="

# Install UFW
apt install -y ufw

# Default policy: deny all inbound, allow all outbound
ufw default deny incoming
ufw default allow outgoing

# Allow SSH (so you don't lock yourself out)
ufw allow 22/tcp comment 'SSH'

# Allow blockchain peer port (the only public-facing port)
ufw allow 5001/tcp comment 'repryntt blockchain'

# Enable the firewall
ufw --force enable

# Show status
echo ""
echo "=== Firewall Active ==="
ufw status verbose
echo ""
echo "Done. Only ports 22 (SSH) and 5001 (blockchain) are open."
