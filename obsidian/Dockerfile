FROM lscr.io/linuxserver/obsidian:v1.12.7-ls132@sha256:1a781867b7b98663453ba5a0154f51ae95a928411137099fe41e61b19dc496ab

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
