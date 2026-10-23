#!/bin/sh
# One-shot install of the now-playing daemon + np.rizzfm.com nginx site.
# Run from this directory:  sudo sh install.sh
set -eu
cd "$(dirname "$0")"

id nowplaying >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin nowplaying
install -d -o nowplaying -g nowplaying /var/www/nowplaying
install -d /opt/nowplaying
install -m 755 now_playing.py /opt/nowplaying/now_playing.py
install -m 644 now-playing.service /etc/systemd/system/now-playing.service
systemctl daemon-reload
systemctl enable now-playing
systemctl restart now-playing

install -m 644 nginx-np.conf /etc/nginx/sites-available/np
ln -sf /etc/nginx/sites-available/np /etc/nginx/sites-enabled/np
nginx -t
systemctl reload nginx

sleep 5
echo "--- daemon log"
journalctl -u now-playing -n 5 --no-pager
echo "--- local check"
curl -sk --resolve np.rizzfm.com:443:127.0.0.1 https://np.rizzfm.com/capital.json | head -5
