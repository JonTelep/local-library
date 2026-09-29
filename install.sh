#!/usr/bin/env bash
# Local Library installer for Ubuntu Server 24.04+.
# Installs Ollama + a small model, kiwix-serve + the latest English Wikipedia, and the chat page.
# Re-run it any time to update Wikipedia, the model, and the app.
#
#   sudo ./install.sh
#   sudo TYPESAFE_API_KEY=... ./install.sh     # turn on Jev re-ranking
#   sudo VARIANT=nopic ./install.sh            # maxi (~119 GB, images) | nopic (~50 GB) | mini (~11 GB, intros only)
set -euo pipefail
[ "$EUID" -eq 0 ] || exec sudo -E "$0" "$@"

VARIANT=${VARIANT:-maxi}
MODEL=${MODEL:-qwen3.5:4b}
PORT=${PORT:-80}
SRC=$(cd "$(dirname "$0")" && pwd)
APP=/opt/local-library
DATA=/var/lib/local-library
ENV=/etc/local-library.env
ZIMS=https://download.kiwix.org/zim/wikipedia

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

step "Packages"
apt-get update -qq
apt-get install -y -qq curl wget python3 pciutils ubuntu-drivers-common zstd >/dev/null

NEED_REBOOT=
if lspci | grep -qi nvidia && ! command -v nvidia-smi >/dev/null; then
  step "NVIDIA driver"
  ubuntu-drivers install
  NEED_REBOOT=1
fi

step "Ollama"
command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/local-library.conf <<'EOF'
[Service]
# Reachable from the LAN, and keep the model loaded so answers never wait on a cold start.
Environment=OLLAMA_HOST=0.0.0.0:11434
Environment=OLLAMA_KEEP_ALIVE=-1
EOF
systemctl daemon-reload
systemctl enable -q ollama
systemctl restart ollama
until curl -fs http://127.0.0.1:11434/ >/dev/null; do sleep 1; done
ollama pull "$MODEL"

step "kiwix-serve"
mkdir -p "$APP/bin" "$DATA"
curl -fsSL https://download.kiwix.org/release/kiwix-tools/kiwix-tools_linux-x86_64.tar.gz |
  tar xz -C "$APP/bin" --strip-components=1
"$APP/bin/kiwix-serve" --version | head -1

step "Wikipedia ($VARIANT)"
ZIM=$(curl -fsSL "$ZIMS/" | grep -oE "wikipedia_en_all_${VARIANT}_[0-9]{4}-[0-9]{2}\.zim" | sort -u | tail -1)
[ -n "$ZIM" ] || { echo "No wikipedia_en_all_${VARIANT} file found at $ZIMS" >&2; exit 1; }
if [ ! -f "$DATA/$ZIM" ]; then
  echo "Downloading $ZIM. This is big; if it gets interrupted, re-run the installer and it resumes."
  wget -c -q --show-progress --tries=0 --retry-connrefused --waitretry=10 -O "$DATA/$ZIM.part" "$ZIMS/$ZIM"
  echo "Verifying checksum…"
  echo "$(curl -fsSL "$ZIMS/$ZIM.sha256" | cut -d' ' -f1)  $DATA/$ZIM.part" | sha256sum -c --quiet
  mv "$DATA/$ZIM.part" "$DATA/$ZIM"
fi
ln -sfn "$ZIM" "$DATA/wikipedia.zim"  # the file name is the book name, so URLs stay /content/wikipedia/...
find "$DATA" -maxdepth 1 -name 'wikipedia_en_all_*.zim' ! -name "$ZIM" -print -delete  # older editions

step "Local Library app"
id -u local-library >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin local-library
install -m 644 "$SRC/app.py" "$SRC/index.html" "$APP/"
[ -f "$ENV" ] || printf 'MODEL=%s\nPORT=%s\nTYPESAFE_API_KEY=\n' "$MODEL" "$PORT" > "$ENV"
sed -i "s|^MODEL=.*|MODEL=$MODEL|" "$ENV"
[ -z "${TYPESAFE_API_KEY:-}" ] || sed -i "s|^TYPESAFE_API_KEY=.*|TYPESAFE_API_KEY=$TYPESAFE_API_KEY|" "$ENV"
chmod 600 "$ENV"

cat > /etc/systemd/system/kiwix.service <<EOF
[Unit]
Description=kiwix-serve (offline Wikipedia, local only; the app proxies it)
After=network.target

[Service]
ExecStart=$APP/bin/kiwix-serve --address=127.0.0.1 --port=8080 $DATA/wikipedia.zim
User=local-library
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/local-library.service <<EOF
[Unit]
Description=Local Library chat page
After=network-online.target kiwix.service ollama.service
Wants=network-online.target

[Service]
EnvironmentFile=$ENV
ExecStart=/usr/bin/python3 $APP/app.py
User=local-library
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable -q kiwix local-library
systemctl restart kiwix local-library

step "Laptop lid"
mkdir -p /etc/systemd/logind.conf.d
printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n' \
  > /etc/systemd/logind.conf.d/local-library.conf
systemctl kill -s HUP systemd-logind
echo "Closing the lid no longer suspends the machine."

if ufw status 2>/dev/null | grep -q "Status: active"; then
  step "Firewall"
  for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
    ufw allow from "$net" to any port "$PORT" proto tcp >/dev/null
    ufw allow from "$net" to any port 11434 proto tcp >/dev/null
  done
  echo "Opened ports $PORT and 11434 to the local network only."
fi

IP=$(hostname -I | awk '{print $1}')
step "Done"
echo "Chat:    http://$IP$([ "$PORT" = 80 ] || echo ":$PORT")/"
echo "Ollama:  http://$IP:11434"
grep -q '^TYPESAFE_API_KEY=.\+' "$ENV" && echo "Jev re-ranking: on" || echo "Jev re-ranking: off (add TYPESAFE_API_KEY to $ENV, then: sudo systemctl restart local-library)"
[ -z "$NEED_REBOOT" ] || echo -e "\n\033[1mNVIDIA driver installed: reboot once (sudo reboot) so the model runs on the GPU.\033[0m"
