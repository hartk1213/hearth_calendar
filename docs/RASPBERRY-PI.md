# Raspberry Pi

Start with **Raspberry Pi OS (64-bit, with desktop)**. In Raspberry Pi Imager, set your Wi-Fi, username and time zone before writing the card.

- **Hearth runs on a server** (Unraid, NAS, another PC): you only need the [Wall screen](#wall-screen) part below.
- **Everything on the Pi** (no server): do [Run Hearth on the Pi](#run-hearth-on-the-pi) first, then the wall screen with `localhost`.

## Run Hearth on the Pi

The easiest way is Docker. Open a terminal on the Pi and run these one at a time:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
git clone https://github.com/hartk1213/hearth.git ~/hearth && cd ~/hearth
cp .env.example .env && nano .env      # set TZ, then Ctrl+O, Enter, Ctrl+X
docker compose up -d
```

Data is stored in `~/hearth/data`, so back that folder up now and then.

## Wall screen

This makes the Pi open Hearth full-screen whenever it turns on.

Open a terminal and paste the block below. Change `SERVER` on the first line to your server's address and port first, e.g. `192.168.1.10:8095`, or use `localhost:8095` if Hearth runs on this Pi.

```bash
SERVER=192.168.1.10:8095
cat > ~/hearth-kiosk.sh <<EOF
#!/bin/sh
# Wait for the server (useful after a power cut, when the Pi boots first)
until curl -sf http://$SERVER/api/health >/dev/null; do sleep 5; done
exec chromium --kiosk --noerrdialogs --disable-infobars --check-for-update-interval=31536000 "http://$SERVER/?kiosk=1"
EOF
chmod +x ~/hearth-kiosk.sh
mkdir -p ~/.config/labwc && echo '~/hearth-kiosk.sh &' >> ~/.config/labwc/autostart
```

Then:

1. **Turn off screen blanking:** Raspberry Pi menu → Preferences → Control Centre → Display → turn off **Screen Blanking**.
2. **Reboot.** Hearth should open full-screen on its own.

Notes:

- `?kiosk=1` tells Hearth it's on the wall screen. It then doesn't open recipe websites there, because a kiosk screen has no back button. On phones those links work normally.
- On older Pi OS images the browser command is `chromium-browser`.
- To make everything bigger, use **Settings → Display → Text size** in Hearth.
- To get out of full-screen, plug in a keyboard and press **Alt+F4**.
- Don't add `--incognito`. Hearth keeps a copy in the browser so the screen keeps working if the server is briefly down.
