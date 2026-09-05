# syntax=docker/dockerfile:1

# Base is a multi-arch (aarch64/amd64) linuxserver/obsidian image, pinned to a
# digest so CI builds each architecture from a reproducible, immutable base.
FROM lscr.io/linuxserver/obsidian:v1.13.7-ls144@sha256:c6c86336a2cf57506b0db2beb09ef4a50170808820a59c20063e669d12122dd4

# Overlay the add-on's s6 init service (bootstrap config + permissions).
# The base image already ships its own init-obsidian-config service; our run
# script supersedes it, so base-image upgrades surface changes cleanly via diff.
COPY rootfs/ /

LABEL \
  org.opencontainers.image.title="Home Assistant Obsidian" \
  org.opencontainers.image.description="Runs Obsidian within your Home Assistant setup providing integration of live-features" \
  org.opencontainers.image.source="https://github.com/crazyrokr/ha-obsidian/tree/main/obsidian" \
  org.opencontainers.image.licenses="Apache License 2.0"
