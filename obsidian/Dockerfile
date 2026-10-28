FROM lscr.io/linuxserver/obsidian:v1.14.5-ls150@sha256:da0e7e082b5922fe9f9065254e38e7b9875211f85089834f5e822f7037dbfabb

RUN mkdir /share
COPY root /

# The PATH overlays and the S6 service must be executable in the image.
RUN chmod 0755 /usr/local/bin/xdg-open /usr/local/bin/xdg-email /etc/services.d/open-url-api/run

# The theme-sync daemon keeps the desktop background in the theme's color.
# swaybg paints the nested labwc compositor in Wayland mode; in X mode the
# root window is painted with xsetroot, already present via x11-xserver-utils.
RUN apt-get update && apt-get install -y --no-install-recommends swaybg && rm -rf /var/lib/apt/lists/*

# Durable web hooks: the nginx config and the dashboard are regenerated from
# /defaults/default.conf and /usr/share/selkies/selkies-dashboard at every
# boot, so the edits go into those sources at build time.
RUN sed -i 's|location SUBFOLDER {|location /api/set-theme {\n  proxy_pass http://127.0.0.1:8090;\n}\nlocation /theme-sync.js {\n  alias /opt/ha-theme-sync/theme-sync.js;\n}\nlocation /api/open-url {\n  limit_except GET { deny all; }\n  proxy_http_version 1.1;\n  proxy_read_timeout 70s;\n  proxy_buffering off;\n  proxy_pass http://127.0.0.1:8091;\n}\nlocation /open-url.js {\n  alias /opt/ha-open-url/open-url.js;\n}\nlocation SUBFOLDER {|g' /defaults/default.conf \
 && ( grep -q theme-sync.js /usr/share/selkies/selkies-dashboard/index.html \
      || sed -i 's|</head>|<script src="./theme-sync.js"></script></head>|' /usr/share/selkies/selkies-dashboard/index.html ) \
 && ( grep -q open-url.js /usr/share/selkies/selkies-dashboard/index.html \
      || sed -i 's|</head>|<script src="./open-url.js"></script></head>|' /usr/share/selkies/selkies-dashboard/index.html ) \
 && grep -q api/set-theme /defaults/default.conf \
 && grep -q api/open-url /defaults/default.conf \
 && grep -q theme-sync.js /usr/share/selkies/selkies-dashboard/index.html \
 && grep -q open-url.js /usr/share/selkies/selkies-dashboard/index.html

LABEL \
  org.opencontainers.image.title="Home Assistant Obsidian" \
  org.opencontainers.image.description="Runs Obsidian within your Home Assistant setup providing integration of live-features" \
  org.opencontainers.image.source="https://github.com/crazyrokr/ha-obsidian/tree/main/obsidian" \
  org.opencontainers.image.licenses="MIT"
