# CPSM — Cross-Platform Session Manager

A desktop GUI for managing tmux/SSH sessions and Claude Code projects across
many remote hosts. Linux primary, Windows via `itmux` or `PSMUX`.

CPSM supersedes the legacy `claude-multi-manager.sh` bash launcher (still
shipped, [documented at the bottom](#claude-multi-managersh-legacy-bash-tool)).

---

## Quick Start

### From the AppImage (Linux x86_64)

```bash
# 1. Make it executable and run
chmod +x CPSM-0.1.0-x86_64.AppImage
./CPSM-0.1.0-x86_64.AppImage

# 2. (Optional) install system-wide with menu entry + dependencies
./packaging/install.sh CPSM-0.1.0-x86_64.AppImage          # per-user
sudo ./packaging/install.sh CPSM-0.1.0-x86_64.AppImage     # system-wide
```

The first launch shows a Welcome dialog: **Open** an existing `.cpsm.yaml`,
**Import** a legacy `~/.claude-projects.yaml`, or start with an **Empty**
config.

### From source

```bash
python -m venv .venv
source .venv/bin/activate                    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m cpsm
```

---

## Table of contents

- [Installing](#installing)
- [Configuration file](#configuration-file)
- [Launch profiles](#launch-profiles)
- [Groups and layouts](#groups-and-layouts)
- [GUI tour](#gui-tour)
- [SSH key management](#ssh-key-management)
- [Session discovery and adoption](#session-discovery-and-adoption)
- [Cross-platform notes](#cross-platform-notes)
- [Building from source](#building-from-source)
- [Configuration paths and env vars](#configuration-paths-and-env-vars)
- [Legacy bash tool](#claude-multi-managersh-legacy-bash-tool)
- [License](#license)

---

## Installing

| Method | Audience | Command |
|---|---|---|
| **AppImage** (recommended) | Linux end users | `./CPSM-0.1.0-x86_64.AppImage` |
| **`install.sh`** | Linux end users wanting menu integration + deps | `./packaging/install.sh CPSM-0.1.0-x86_64.AppImage` |
| **pip / source** | Developers, packagers | `pip install -e ".[dev]"` |
| **PyInstaller bundle** | Packagers building installers | `pyinstaller --noconfirm packaging/cpsm.spec` |

`install.sh` autodetects the package manager (apt, dnf/yum, pacman, zypper,
apk) and installs `tmux`, `sshpass`, `openssh-client`, plus a terminal emulator
if none is found. It is idempotent — re-running upgrades the AppImage in place.

Register the freedesktop launcher (only needed if you ran the AppImage
manually rather than via `install.sh`): the Welcome dialog prompts on first
run, or you can do it from **Tools → Settings**.

---

## Configuration file

CPSM uses a single YAML file (default: `~/.cpsm.yaml`) validated by Pydantic
v2. Every change in the GUI auto-saves — there is no Save button. Comments
and key order survive round-trip saves.

Top-level keys:

```yaml
schema_version: 1
settings:                  # global preferences
ssh_keys:        []        # SSH key registry (see SSH key management)
connections:     []        # individual hosts/sessions
groups:          []        # named bundles of connections (each owns a layout)
launch_templates:[]        # custom `bash` snippets for profile=custom
```

Every id must match `^[a-z0-9][a-z0-9-]{1,62}$`.

### `settings` block

| Key | Default | Notes |
|---|---|---|
| `default_multiplexer` | `auto` | `tmux` / `itmux` / `psmux` / `auto` |
| `default_terminal` | `auto` | `gnome-terminal`, `konsole`, `alacritty`, `kitty`, `xterm`, `wezterm`, `wt` |
| `ssh_binary` | `auto` | `openssh` or `plink` |
| `default_claude_options` | `--resume` | Appended to every `claude` invocation |
| `default_ssh_options` | `-o ConnectTimeout=10 -o ServerAliveInterval=30` | |
| `known_hosts_strict` | `true` | When `false`, accepts unknown host keys |
| `status_poll_interval_ms` | `3000` | How often the GUI polls tmux for status |
| `layout_conflict_default` | `move` | `move` / `keep` / `error` when re-launching with a different layout |
| `layout_preserve_on_remove` | `true` | Removing a pane leaves an empty placeholder so neighbors don't shift |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

All of these are editable from **Tools → Settings**.

---

## Launch profiles

Each connection has a `launch_profile` that selects how it starts. The
Connection editor in the GUI shows only the fields valid for the chosen
profile.

### `claude-remote` — Claude Code over SSH

```yaml
- id: api-service
  launch_profile: claude-remote
  host: 192.168.1.10
  user: ubuntu
  port: 22                         # default 22
  project_folder: /srv/api-service
  claude_options: "--resume"
  identity_file_ref: prod-key      # references ssh_keys[].id
  auth_method: ask                 # ask | key | password
  sudo_user: appuser               # optional `su` step on the remote
  jump_host: bastion@10.0.0.1      # optional, up to 4 hops
  keepalive_interval_s: 30
  connection_timeout_s: 10
  pre_commands:  ["nvm use 20"]
  post_commands: []
  env: { LANG: "C.UTF-8" }
  auto_reconnect: true
  auto_reconnect_on_clean_exit: false
  reconnect_backoff_ms: [1000, 3000, 10000, 30000]
  reconnect_max_attempts: 0        # 0 = unlimited
  tags: [prod]
  notes: "Pinned to Node 20 LTS"
```

### `claude-local` — Claude Code on this machine

```yaml
- id: my-project
  launch_profile: claude-local
  project_folder: /home/me/code/my-project
  claude_options: "--resume"
```

### `ssh-shell` — Plain interactive SSH

```yaml
- id: bastion
  launch_profile: ssh-shell
  host: bastion.example.net
  user: ops
  identity_file_ref: ops-key
  project_folder: /srv/ops          # optional: cd here on connect
```

### `local-shell` — Local terminal at a directory

```yaml
- id: home
  launch_profile: local-shell
  project_folder: /home/me
```

### `custom` — Anything else, via a launch template

```yaml
launch_templates:
  - id: db-shell
    description: "Connect to the production read replica"
    bash: |
      ssh -t {{ user }}@{{ host }} 'psql -U {{ env.PGUSER }} -d {{ env.PGDATABASE }}'

connections:
  - id: prod-db
    launch_profile: custom
    custom_template_id: db-shell
    user: dba
    host: db-replica.internal
    env: { PGUSER: dba, PGDATABASE: app }
```

The template body is rendered with the connection's fields as context.
Manage templates in **Tools → Launcher Templates**.

---

## Groups and layouts

A **group** bundles connections that should run together as panes within one
tmux session. Each group **owns its layout** — the screen-map geometry is a
property of the group, not a separate top-level concept.

```yaml
groups:
  - id: backend
    name: "Backend services"
    color: "#3FA9F5"               # rendered in sidebar + screen map
    members: [api-service, worker]
    launch_order: parallel         # sequential | parallel
    launch_delay_ms: 0
    isolation: shared              # shared | per-group
    layout_conflict: move          # move | keep | error
    auto_attach: false
```

- **`isolation: shared`** — all groups attach to one tmux session named
  `cpsm-shared`. Pane indices are global across the session.
- **`isolation: per-group`** — each group gets a session named after its id.

The pane geometry for a group is described by viewports placed at percentage
coordinates on each physical monitor:

```yaml
screen_layouts:
  - id: backend-layout
    name: "Backend on monitor 0"
    monitors:
      - identifier: "DP-1"          # OS-reported monitor name (optional)
        monitor_index_hint: 0       # fallback when identifier doesn't match
        viewports:
          - id: top-left
            geometry_pct: { x:  0, y:  0, w: 50, h: 50 }
            tmux_layout: tiled       # tiled | even-h | even-v | main-h | main-v | custom
            panes:
              - { connection_id: api-service }
              - { connection_id: worker }
```

Validation enforces:
- viewport ids unique within a monitor;
- viewports within one monitor cannot overlap by more than 1% area;
- a `connection_id` cannot appear twice in the same viewport;
- `inherits_from` must reference an existing layout (no cycles).

CPSM also supports nested **split trees** (mixed horizontal/vertical splits
with explicit ratios) — automatically derived from the flat `panes` list when
absent, edited live in the screen map widget when present.

---

## GUI tour

CPSM is GUI-only. Everything is reachable from menus, the sidebar, or the
screen map.

### Window layout

```
┌──────────────────────────────────────────────────────────────────┐
│ Menu bar:   File │ Edit │ View │ Sessions │ Tools │ Help         │
│ Toolbar                                                          │
├──────────────────┬───────────────────────────┬───────────────────┤
│ Sidebar          │ Screens canvas            │ Inspector         │
│ (dock left)      │ (visual screen map)       │ (dock right)      │
│                  │                           │                   │
│  ▼ Connections   │  ┌────────┬────────┐      │  Properties of    │
│    api-service   │  │  api…  │ worker │      │  the selected     │
│    worker        │  ├────────┴────────┤      │  sidebar item     │
│                  │  │   web-frontend  │      │                   │
│  ▼ Groups        │  └─────────────────┘      │                   │
│    backend       │                           │                   │
│                  │                           │                   │
│  ▼ Discovered    │                           │                   │
│    (auto-found)  │                           │                   │
├──────────────────┴───────────────────────────┴───────────────────┤
│ Status bar: config path · multiplexer health · validation status │
└──────────────────────────────────────────────────────────────────┘
```

- **Sidebar** (dock, left) — Connections, Groups, and Discovered. Each
  Connection nests under its Group, and matched discovered sessions nest
  under their owning Connection.
- **Screens canvas** (centre) — live render of your physical monitors with
  the active group's layout overlaid. Drag/drop, splits, status colors, etc.
- **Inspector** (dock, right) — properties of the currently-selected
  sidebar item. Toggle with **View → Toggle Inspector** (F4).
- **Status bar** (bottom) — current `.cpsm.yaml` path, multiplexer health,
  validation state.

### Menu bar

| Menu | Items |
|---|---|
| **File** | New Connection · New Group · Open Config · Import (legacy YAML) · Quit |
| **Edit** | Find |
| **View** | Toggle Inspector |
| **Sessions** | Launch (selected) · Stop · Reconnect |
| **Tools** | Launcher Templates · Manage SSH Keys · Settings |
| **Help** | About |

Saves and validation happen automatically — there is no explicit Save or
Validate command.

### Connection editor

Profile-aware form: fields appear/disappear as you switch `launch_profile`.
Validates as you type; saves are gated on a clean validation pass.

Covered:
- identity (id, name, profile)
- network (host, port, user, jump host, keepalive, connection timeout)
- auth (`identity_file_ref`, `auth_method`, `sudo_user`)
- environment (`env` key/value table, `pre_commands`, `post_commands`)
- launch behavior (`claude_options`, `project_folder`, `custom_template_id`)
- reconnect policy (auto-reconnect, backoff, max attempts)
- metadata (tags, notes)

Duplicating a connection auto-opens the editor on the new copy.

### Group editor

Drag connections into the members list, set color, isolation, launch order,
and the default layout.

### Layout editor

Add/remove monitors, rename viewports, edit geometry numerically, pick the
tmux layout per viewport.

### Screens canvas

A live render of your physical monitors with the active group's layout
overlaid.

- **Drag-drop** — drag a connection from the sidebar onto a viewport edge or
  center. Edge zones split, center zone replaces. Ambiguous center drops
  open a disambiguation popup.
- **Modifiers** — `Shift` forces horizontal split, `Ctrl` forces vertical,
  `Alt` swaps with the existing pane.
- **Multi-group overlay** — toggle multiple groups visible at once;
  conflicting panes are marked.
- **Status colors** (4-state model) — green (running + attached), amber
  (running, detached), red (failed/exited), gray (unknown / not yet polled).
- **Empty-pane preservation** — removing a pane leaves a `_placeholder.sh`
  process so neighbors never shift; close the placeholder explicitly to
  re-tile.

### SSH key manager

**Tools → Manage SSH Keys.** Generate keys with `ed25519`/`rsa`/`ecdsa`,
deploy via `ssh-copy-id`, and review which connections each key was deployed
to. Passphrases are stored in the OS keychain (libsecret on Linux,
Credential Manager on Windows, Keychain on macOS) — never in YAML.

### Launcher templates dialog

**Tools → Launcher Templates.** CRUD for the `launch_templates[]` array.
Live preview of the rendered bash against a sample connection.

### Settings dialog

**Tools → Settings.** All keys from the [`settings` block](#settings-block),
plus a multiplexer auto-detect + path probe button.

### Other dialogs

These are reached from context menus, the screen map, or the launch flow —
not the menu bar.

- **Welcome** — first-run dispatcher (Open / Import / Empty).
- **Adopt session** — right-click a Discovered session to claim it as a CPSM
  connection (pre-fills from the discovery data).
- **Launch conflict** — when launching a connection that already has a live
  session: focus existing, kill+relaunch, or open in a new pane.
- **Generate / deploy SSH key** — wizards wrapping `ssh-keygen` and
  `ssh-copy-id`.
- **Drop disambiguation** — when a center-drop on the screen map is
  ambiguous (replace? split? swap?).
- **Screens save** — confirm overwrite when saving over an existing layout.
- **Validation errors** — modal popup if an auto-save would write an
  invalid document; lists every issue with click-to-jump.
- **About** — version, license, build info.

---

## SSH key management

```yaml
ssh_keys:
  - id: prod-key
    name: "Production deploy key"
    type: ed25519
    private_path: ~/.ssh/cpsm_prod_ed25519
    public_path:  ~/.ssh/cpsm_prod_ed25519.pub
    passphrase_ref: keyring://cpsm/prod-key       # OS keychain only
    created_at: 2026-04-01T12:00:00Z
    deployments:
      - connection_id: api-service
        deployed_at:   2026-04-01T12:05:00Z
        method:        ssh-copy-id
```

- **Keys never live in YAML.** Only paths and a keyring reference.
- **Auth method per connection:**
  - `ask` — prompt on first launch and remember the choice;
  - `key` — always use `identity_file_ref`, deploy if needed;
  - `password` — force password auth (never prompt for key).
- **Deployment tracking** — every successful `ssh-copy-id` writes an entry
  under the key's `deployments[]` list.

Edit the registry from **Tools → Manage SSH Keys**.

---

## Session discovery and adoption

CPSM walks `/proc` (Linux) on every poll cycle and classifies running
processes into:

- **`claude-local`** — a local `claude` process you started outside CPSM.
- **`claude-remote`** — an `ssh ... claude` invocation.
- **`ssh-shell`** — a plain interactive `ssh`.
- **`tmux-session`** — a `tmux` server you might want to attach.

Discovered sessions show up under the sidebar's **Discovered** node and are
**auto-correlated** to existing connections by:

1. **`claude-local` matcher** — exact `cwd` match against
   `connections[].project_folder`.
2. **Remote host+user matcher** — single match → assigned. Multiple matches
   sharing host+user (e.g. three `root@10.10.10.44` connections that differ
   only by `project_folder`) are deferred to step 3.
3. **Remote probe** — CPSM SSHes into the remote, reads `/proc` for the ssh
   peer's PID, extracts its `cwd`, and matches that against each candidate
   Connection's `project_folder`.

Right-click a discovered row → **Adopt** to convert it into a real
`connections[]` entry, pre-filled from the discovery data.

Set `CPSM_LOG_LEVEL=INFO` in the environment to trace probe activity:

```bash
CPSM_LOG_LEVEL=INFO ./CPSM-0.1.0-x86_64.AppImage 2>/tmp/cpsm.log
```

---

## Cross-platform notes

| Platform | Multiplexer | Terminal default | Notes |
|---|---|---|---|
| Linux | `tmux` | `gnome-terminal` / `konsole` / `xterm` (auto) | Primary target. `python-xlib` enables monitor-name fallback. |
| Windows | `itmux` (tmux-compatible) | Windows Terminal (`wt`) | Sockets via named pipes. PowerShell quoting handled internally. |
| Windows | `PSMUX` (PowerShell-native) | Windows Terminal | JSON cmdlet output; pixel vs cell geometry normalized in the backend. |
| macOS | `tmux` | iTerm2 / Terminal.app | Untested on every release; should work for `claude-remote` and `ssh-shell`. |

`settings.default_multiplexer: auto` probes for tmux first, then itmux, then
PSMUX. Override per environment by setting `CPSM_MULTIPLEXER`.

---

## Building from source

```bash
# Prerequisites: Python 3.11+, libsecret-1-0 + libssl3 on Linux
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest -q

# Lint + type-check
ruff check .
mypy cpsm

# Build a one-folder PyInstaller bundle (dist/cpsm/)
pyinstaller --noconfirm packaging/cpsm.spec

# Build the Linux AppImage
appimage-builder --recipe packaging/AppImageBuilder.yml --skip-tests
```

The minimal buildable fileset (for distributing source):

```
cpsm/                                # entire package incl. resources/
pyproject.toml
README.md
LICENSE
packaging/cpsm.spec
packaging/AppImageBuilder.yml
packaging/install.sh                 # optional, Linux installer
tests/                               # strongly recommended
docs/SPEC-v1.3.md                    # design spec
```

`build/`, `dist/`, `AppDir/`, `appimage-build/`, `.venv/`, `__pycache__/`,
`.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, and the AppImage itself are
all regenerated on build.

---

## Configuration paths and env vars

Config path is resolved in this order (first hit wins):

1. `$CPSM_CONFIG` environment variable
2. `$XDG_CONFIG_HOME/cpsm/.cpsm.yaml` (Linux) /
   `%APPDATA%\cpsm\.cpsm.yaml` (Windows)
3. `~/.cpsm.yaml`

Env vars CPSM honors:

| Var | Effect |
|---|---|
| `CPSM_CONFIG` | Override the config-file path |
| `CPSM_LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` — overrides `settings.log_level` |
| `CPSM_MULTIPLEXER` | Force `tmux`/`itmux`/`psmux` |
| `XDG_CONFIG_HOME` | Standard Linux config root |
| `APPDATA` | Standard Windows config root |

---

## claude-multi-manager.sh (legacy bash tool)

The original bash launcher is preserved unchanged for users who don't want a
GUI. CPSM imports its `~/.claude-projects.yaml` into the new `~/.cpsm.yaml`
schema **without modifying the source file**, so both tools can run side by
side.

### Features

- Launch a single Claude session in the current terminal by project name
- Launch a group of projects in a tmux session with tiled panes (up to 4
  per group)
- Launch all project groups at once, each in its own tmux session
- Automatic reconnect loop in each pane: reconnect, drop to shell, or quit
- YAML config with per-project SSH user, sudo user, project folder, and
  Claude CLI options
- Temporary launcher scripts cleaned up on exit

### Prerequisites

- bash 4+
- `python3` with `PyYAML`
- `jq`
- `tmux`
- SSH key-based auth configured for every remote host
- Claude Code CLI installed on each remote host

See `claude-multi-manager-dependencies.md` for full installation details.

### Configuration

Default path: `~/.claude-projects.yaml`. Override with `CLAUDE_CONFIG=/path`.

```yaml
projects:
  - name: api-service
    group: backend
    host: 192.168.1.10
    ssh_user: ubuntu
    sudo_user: appuser
    project_folder: /srv/api-service
    claude_options: "--resume"
  - name: worker
    group: backend
    host: 192.168.1.12
    ssh_user: ubuntu
    sudo_user: appuser
    project_folder: /srv/worker
    claude_options: "--resume"
```

| Field | Required | Description |
|---|---|---|
| `name` | yes | Unique project identifier |
| `group` | no | Group label for multi-project launches |
| `host` | yes | Remote hostname or IP |
| `ssh_user` | yes | SSH login user |
| `sudo_user` | no | User to `su` to on the remote host |
| `project_folder` | yes | Absolute path to the project on the remote host |
| `claude_options` | no | CLI flags (defaults to `--resume`) |

### Usage

```bash
./claude-multi-manager.sh                    # show usage + list groups
./claude-multi-manager.sh --help             # detailed help
./claude-multi-manager.sh <project>          # single project, current terminal
./claude-multi-manager.sh <group>            # all projects in group, in tmux
./claude-multi-manager.sh all                # every group, one tmux/group

tmux attach -t claude-<groupname>            # attach later
```

When a Claude session ends or disconnects each pane prompts:

```
[r] Reconnect   [s] Shell   [q] Quit
```

### How it works

1. **Config parsing** — `parse_config()` runs `python3` + PyYAML inline to
   convert YAML → JSON, then queries with `jq`.
2. **Launcher generation** — per project: a local `/tmp/launcher-<name>.sh`
   uploads the remote half via `scp`, opens an interactive SSH session with
   `-tt`, and wraps it in a reconnect loop.
3. **Single project** — local launcher is `exec`'d in the current terminal.
4. **Group launch** — `tmux new -s claude-<group>` then `split-window`
   tiled, each pane runs one project's launcher.
5. **All groups** — one tmux session per group, created sequentially.
6. **Cleanup** — `/tmp/launcher-*.sh` and `/tmp/remote-*.sh` removed on exit.

CPSM addresses the bash tool's single biggest limitation — tmux's tiled
layouts breaking down past 4 panes — with arbitrary multi-screen layouts,
per-pane geometry, and the visual screen map editor.

---

## License

AGPLv3 — see [LICENSE](LICENSE).
