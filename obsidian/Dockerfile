FROM lscr.io/linuxserver/obsidian:v1.12.7-ls132@sha256:df974792e7b1d9dcab27da9960296897bc5a4c6dcad31da42842104c4e91966e

RUN mkdir /share
RUN echo "[ -f /config/.config/obsidian/obsidian.json ] || \
    (cd /config/.config && mkdir obsidian && chown abc:abc obsidian && \
    echo '{\"frame\":\"native\",\"updateDisabled\":true}' > obsidian/obsidian.json && \
    chown abc:abc obsidian/obsidian.json)" >> /etc/s6-overlay/s6-rc.d/init-obsidian-config/run

LABEL \
  org.opencontainers.image.title="Home Assistant Obsidian" \
  org.opencontainers.image.description="Runs Obsidian within your Home Assistant setup providing integration of live-features" \
  org.opencontainers.image.source="https://github.com/crazyrokr/ha-obsidian/tree/main/obsidian" \
  org.opencontainers.image.licenses="Apache License 2.0"
