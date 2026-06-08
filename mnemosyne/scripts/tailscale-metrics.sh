#!/bin/bash
sudo tailscale debug metrics > /var/lib/node_exporter/textfile_collector/tailscale.prom.tmp
mv /var/lib/node_exporter/textfile_collector/tailscale.prom.tmp \
   /var/lib/node_exporter/textfile_collector/tailscale.prom
