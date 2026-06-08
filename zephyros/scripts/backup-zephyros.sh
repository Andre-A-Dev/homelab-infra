#!/bin/bash
# backup-zephyros.sh — backs up Pi-hole, Unbound and misc configs to Mnemosyne via Tailscale

MNEMOSYNE_IP="100.x.x.x"       # Tailscale IP of Mnemosyne — update after setup
BACKUP_USER="youruser"
REMOTE_DIR="/mnt/codex/backups/zephyros"
DATE=$(date +%F)
BACKUP_DIR="/tmp/zephyros-backup-$DATE"
LOG="/var/log/backup-zephyros.log"
SSH_KEY="/home/youruser/.ssh/backup_key"

echo "=== Backup started: $(date) ===" >> $LOG

mkdir -p "$BACKUP_DIR/pihole" "$BACKUP_DIR/unbound" "$BACKUP_DIR/misc"

# Pi-hole config
echo "Saving: Pi-hole..." >> $LOG
sudo cp /etc/pihole/pihole.toml "$BACKUP_DIR/pihole/" 2>> $LOG
sudo cp /etc/pihole/custom.list "$BACKUP_DIR/pihole/" 2>> $LOG || true

# Unbound config
echo "Saving: Unbound..." >> $LOG
sudo cp /etc/unbound/unbound.conf.d/pi-hole.conf "$BACKUP_DIR/unbound/" 2>> $LOG

# Misc (msmtp, apticron, pihole6-exporter service)
echo "Saving: Misc..." >> $LOG
sudo cp /etc/msmtprc "$BACKUP_DIR/misc/" 2>> $LOG
sudo cp /etc/apticron/apticron.conf "$BACKUP_DIR/misc/" 2>> $LOG
sudo cp /etc/systemd/system/pihole6-exporter.service "$BACKUP_DIR/misc/" 2>> $LOG

# Caddy stack
echo "Saving: Caddy..." >> $LOG
mkdir -p "$BACKUP_DIR/caddy"
cp ~/stacks/caddy/Caddyfile "$BACKUP_DIR/caddy/" 2>> $LOG
cp ~/stacks/caddy/docker-compose.yml "$BACKUP_DIR/caddy/" 2>> $LOG
# Fix permissions for rsync
sudo chown -R youruser:youruser "$BACKUP_DIR"

# Transfer to Mnemosyne via Tailscale
echo "Transferring to Mnemosyne ($MNEMOSYNE_IP)..." >> $LOG
rsync -az --delete \
    -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    "$BACKUP_DIR/" \
    "$BACKUP_USER@$MNEMOSYNE_IP:$REMOTE_DIR/$DATE/" >> $LOG 2>&1

# Keep only last 30 days on Mnemosyne
ssh -i "$SSH_KEY" "$BACKUP_USER@$MNEMOSYNE_IP" \
    "find $REMOTE_DIR -maxdepth 1 -type d -name '20*' -mtime +30 -exec rm -rf {} +" 2>> $LOG

# Clean up local temp dir
rm -rf "$BACKUP_DIR"

echo "=== Backup finished: $(date) ===" >> $LOG
