#!/bin/bash
sudo tailscale debug metrics | sudo tee /var/lib/node_exporter/textfile_collector/tailscale.prom.tmp > /dev/null
mv /var/lib/node_exporter/textfile_collector/tailscale.prom.tmp \
   /var/lib/node_exporter/textfile_collector/tailscale.prom