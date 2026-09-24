FROM lscr.io/linuxserver/obsidian:v1.13.7-ls145@sha256:50c47a0f5223d4f77ced77af501b6b071285da19e4ad6c036fb132326779bbdb

RUN mkdir /share
COPY root /

# The theme-sync daemon keeps the desktop background in the theme's color.
# swaybg paints the nested labwc compositor in Wayland mode; in X mode the
# root window is painted with xsetroot, already present via x11-xserver-utils.
RUN apt-get update && apt-get install -y --no-install-recommends swaybg && rm -rf /var/lib/apt/lists/*

# Durable web hooks: the nginx config and the dashboard are regenerated from
# /defaults/default.conf and /usr/share/selkies/selkies-dashboard at every
# boot, so the edits go into those sources at build time.
RUN sed -i 's|location SUBFOLDER {|location /api/set-theme {\n  proxy_pass http://127.0.0.1:8090;\n}\nlocation /theme-sync.js {\n  alias /opt/ha-theme-sync/theme-sync.js;\n}\nlocation SUBFOLDER {|g' /defaults/default.conf \
 && ( grep -q theme-sync.js /usr/share/selkies/selkies-dashboard/index.html \
      || sed -i 's|</head>|<script src="./theme-sync.js"></script></head>|' /usr/share/selkies/selkies-dashboard/index.html ) \
 && grep -q api/set-theme /defaults/default.conf \
 && grep -q theme-sync.js /usr/share/selkies/selkies-dashboard/index.html

LABEL \
  org.opencontainers.image.title="Home Assistant Obsidian" \
  org.opencontainers.image.description="Runs Obsidian within your Home Assistant setup providing integration of live-features" \
  org.opencontainers.image.source="https://github.com/crazyrokr/ha-obsidian/tree/main/obsidian" \
  org.opencontainers.image.licenses="MIT"
