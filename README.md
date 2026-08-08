# Home Assistant App: Obsidian

Run a full desktop instance of [Obsidian](https://obsidian.md/) inside your Home Assistant, with live note editing served through the supervisor's Ingress.

![Supports aarch64 Architecture][aarch64-shield] ![Supports amd64 Architecture][amd64-shield]

## About

This app runs a headless Obsidian desktop instance within a container managed by
Home Assistant, exposing it through the web UI over Ingress. All of your notes are
stored on the Home Assistant filesystem, which means your vault lives alongside the
rest of your configuration — editable, backed up, and versioned like everything else
in HA.

It is based on the [LinuxServer.io Obsidian image](https://docs.linuxserver.io/images/docker-obsidian),
so it follows the same container conventions (PUID/PGID, `abc` user, s6-overlay init).

Key capabilities:

- **Obsidian in the browser** — full desktop UI served over HTTPS via Home Assistant Ingress; no extra network exposure required.
- **Persistent vault on disk** — your notes are stored under `/config` (the add-on's container file) and survive restarts.
- **Auto-update disabled** — the pinned container image is what updates; Obsidian's own updater is switched off so nothing overwrites the running build.
- **Works on first boot** — a default profile is bootstrapped automatically, so there's no interactive welcome/download wizard blocking the UI.

> **[aarch64] Architecture note:** Obsidian's desktop build requires a graphical-stack
> container. This image is available for `aarch64` and `amd64`; only these two
> architectures are supported.

## Installation

1. Go to **Settings → Add-ons → Add-on Store**, select the repository this project is
   published to (e.g. an Add-ons repository you maintain), and find **Obsidian**.
2. Click **Install** and wait for the installation to complete.
3. Start the add-on.
4. Open the add-on's **open Web UI** button — Obsidian loads in your browser.

No changes to your system configuration, MQTT, or integrations are required.

### If you are building from this repository

1. Clone this repository and make sure `.github/docker-lock.json` and `obsidian/config.yaml` point at the image you intend to publish.
2. Run the CI (push to `master` triggers it), which builds, signs, and publishes the
   multi-architecture image to `ghcr.io/ibidani/ha-obsidian`.
3. Point a Home Assistant add-on repository at this repo and install as above.

## Configuration

The add-on is pre-configured to work immediately. The only settings are Docker
environment variables the base image passes through.

### Application

```yaml
environment:
  PUID: "1000"
  PGID: "1000"
  TITLE: "Obsidian"
  START_DOCKER: "false"
  NO_GAMEPAD: "true"
  DISABLE_DRI3: "true"
  PIXELFLUX_WAYLAND: "false"
  SELKIES_UI_SHOW_SIDEBAR: "false"
  NO_DECOR: "true"
```

| Variable | Default | Description |
| --- | --- | --- |
| `PUID` / `PGID` | `1000` | The user/group ID the container runs as inside the host namespace. Leave at `1000` unless your vault ownership requires otherwise. |
| `TITLE` | `Obsidian` | Title shown in the web UI. |
| `NO_GAMEPAD` | `true` | Disable virtual gamepad support (not needed for notes). |
| `DISABLE_DRI3` | `true` | Disable DRI3/GPU passthrough; keeps rendering on the software stack for reliability. |
| `PIXELFLUX_WAYLAND` | `false` | Keep the default X11/compositor path. |
| `SELKIES_UI_SHOW_SIDEBAR` | `false` | Hides the extra control sidebar of the streaming UI. |
| `NO_DECOR` | `true` | Removes desktop window decorations. |

### Ports

| Port | Description | Published |
| --- | --- | --- |
| `3000` | Obsidian web UI (used by Ingress) | No (Ingress only) |
| `3001` | Desktop / noVNC port | No (optional to expose) |
| `8082` | Alternative VNC port | No (optional to expose) |

Access is primarily via Ingress; the ports exist so you may publish them if you want
direct (non-Ingress) access.

### Where are my notes?

Your vault (and the Obsidian profile/config) live at:

```
/config/.config/obsidian/
```

`/config` is mapped to the add-on's container folder. To back up or share your notes,
use the Home Assistant backup system or the file-management tools (e.g. Samba/File editor)
to read/write this folder.

## Updating

The add-on ships an exact pinned base image version. Updates come by:

1. **Automatic image pins** — CI checks the upstream image and updates the digest pin
   in `obsidian/Dockerfile` (see CI workflow).
2. **App updates** — bump `version` in `obsidian/config.yaml` and rebuild/publish via CI.

Rebuild the image (or check for add-on updates in the store) to pull in changes.

## Support

This is a community project, provided as-is.

- **Documentation / source**: [github.com/ibidani/ha-obsidian](https://github.com/ibidani/ha-obsidian)
- **Bugs & feature requests**: open an issue in the repository.

## License

[Apache License 2.0](LICENSE)

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
