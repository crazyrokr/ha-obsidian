# syntax=docker/dockerfile:1

# Base is a multi-arch (aarch64/amd64) linuxserver/obsidian image, pinned to a
# digest so CI builds each architecture from a reproducible, immutable base.
FROM lscr.io/linuxserver/obsidian:v1.13.4-ls142@sha256:90fd8a7b03ec5cafb53e596296bbbe99f2dca2ef553bbd3d40d295ef90419921

# Overlay the add-on's s6 init service (bootstrap config + permissions).
# The base image already ships its own init-obsidian-config service; our run
# script supersedes it, so base-image upgrades surface changes cleanly via diff.
COPY rootfs/ /

LABEL \
  org.opencontainers.image.title="Home Assistant Obsidian" \
  org.opencontainers.image.description="Runs Obsidian within your Home Assistant setup providing integration of live-features" \
  org.opencontainers.image.source="https://github.com/ibidani/ha-obsidian/tree/main/obsidian" \
  org.opencontainers.image.licenses="Apache License 2.0"
