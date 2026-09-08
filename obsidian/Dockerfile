FROM lscr.io/linuxserver/obsidian:v1.13.7-ls144@sha256:c6c86336a2cf57506b0db2beb09ef4a50170808820a59c20063e669d12122dd4

RUN mkdir /share

# HA theme sync: daemon, client script, CLI enablement and s6 service.
COPY ha-theme-sync/theme_server.py /opt/ha-theme-sync/theme_server.py
COPY ha-theme-sync/theme-sync.js /opt/ha-theme-sync/theme-sync.js
COPY ha-theme-sync/enable_cli.py /opt/ha-theme-sync/enable_cli.py
COPY root/etc/services.d/theme-api/run /etc/services.d/theme-api/run

# Enable the Obsidian command line interface on every boot: an idempotent
# merge of "cli": true into the global config (preserving user keys), which
# the daemon needs to run `obsidian reload` after a theme change.
RUN echo 'python3 /opt/ha-theme-sync/enable_cli.py' >> /etc/s6-overlay/s6-rc.d/init-obsidian-config/run \
 && grep -q enable_cli.py /etc/s6-overlay/s6-rc.d/init-obsidian-config/run

# Durable web hooks: the nginx config and the dashboard are regenerated from
# /defaults/default.conf and /usr/share/selkies/selkies-dashboard at every
# boot, so the edits go into those sources at build time.
RUN chmod +x /etc/services.d/theme-api/run \
 && sed -i 's|location SUBFOLDER {|location /api/set-theme {\n  proxy_pass http://127.0.0.1:8090;\n}\nlocation /theme-sync.js {\n  alias /opt/ha-theme-sync/theme-sync.js;\n}\nlocation SUBFOLDER {|g' /defaults/default.conf \
 && ( grep -q theme-sync.js /usr/share/selkies/selkies-dashboard/index.html \
      || sed -i 's|</head>|<script src="./theme-sync.js"></script></head>|' /usr/share/selkies/selkies-dashboard/index.html ) \
 && grep -q api/set-theme /defaults/default.conf \
 && grep -q theme-sync.js /usr/share/selkies/selkies-dashboard/index.html

LABEL \
  org.opencontainers.image.title="Home Assistant Obsidian" \
  org.opencontainers.image.description="Runs Obsidian within your Home Assistant setup providing integration of live-features" \
  org.opencontainers.image.source="https://github.com/crazyrokr/ha-obsidian/tree/main/obsidian" \
  org.opencontainers.image.licenses="Apache License 2.0"
