# cc_multi — Claude Code multi-session tooling

This repository contains two related tools for managing multiple Claude Code CLI sessions over tmux/SSH:

1. **`claude-multi-manager.sh`** — the original bash launcher (production-ready, documented below).
2. **CPSM** (`cpsm/`) — a cross-platform Python+PySide6 desktop GUI being built to supersede the bash tool. See [`docs/SPEC-v1.3.md`](docs/SPEC-v1.3.md) for the full design, and [`.claude/pipeline.json`](.claude/pipeline.json) for the phased implementation plan.

The bash tool will continue to work unchanged. CPSM imports its `~/.claude-projects.yaml` config into the new `~/.cpsm.yaml` schema without modifying the source file.

---

## CPSM (in development)

Cross-Platform Session Manager. A desktop GUI for managing tmux/SSH sessions and Claude Code projects on Linux (with tmux) and Windows (with itmux or PSMUX).

### Status

Under construction. Phase 1 (project scaffold) is the first complete milestone. See `.claude/pipeline.json` for the 22-phase plan.

### Quick start (developer)

```bash
python -m venv .venv
source .venv/bin/activate            # Linux/macOS
# .venv\Scripts\activate              # Windows
pip install -e .[dev]
python -m cpsm --version
pytest -q
```

### Highlights of the design

- **Schema-driven config** (`.cpsm.yaml`) with pydantic v2 discriminated unions per `launch_profile` (`claude-remote`, `claude-local`, `ssh-shell`, `local-shell`, `custom`).
- **Read-only import** of legacy `~/.claude-projects.yaml`.
- **Per-group multi-screen layouts** with viewports placed at `geometry_pct` on physical monitors detected via Qt's `QScreen`.
- **Multi-group preview / edit** with color-coded overlay, conflict detection, and split-and-lock layout invariant.
- **Drag-drop screen map** with edge/center drop zones, modifier keys, and a confirmation popup for ambiguous center drops.
- **Empty-slot pane preservation** (`_placeholder.sh`) so removing a pane never moves its neighbors.
- **OS keychain** (libsecret / Windows Credential Manager / macOS Keychain) for SSH passphrases. No passwords or private keys ever stored in YAML.

Full spec: [`docs/SPEC-v1.3.md`](docs/SPEC-v1.3.md).

### License

MIT — see [LICENSE](LICENSE).

---

## claude-multi-manager.sh (legacy bash tool)

A bash-based multi-project launcher that manages Claude Code CLI sessions across multiple remote hosts using tmux and SSH.

### Features

- Launch a single Claude session in the current terminal by project name
- Launch a group of projects in a tmux session with tiled panes (up to 4 per group)
- Launch all project groups at once, each in its own tmux session
- Automatic reconnect loop in each pane: reconnect, drop to shell, or quit after disconnect
- YAML config with per-project SSH user, sudo user, project folder, and Claude CLI options
- Temporary launcher scripts are cleaned up on exit

### Prerequisites

- bash 4+
- python3 with PyYAML
- jq
- tmux
- SSH with key-based authentication configured for all remote hosts
- Claude Code CLI installed on each remote host

See [`claude-multi-manager-dependencies.md`](./claude-multi-manager-dependencies.md) for full dependency details and installation instructions.

### Installation

1. Clone or copy `claude-multi-manager.sh` to a directory on your `PATH`, or run it directly.
2. Make it executable:

```bash
chmod +x claude-multi-manager.sh
```

3. Create your config file at `~/.claude-projects.yaml` (see Configuration below).

### Configuration

The config file lives at `~/.claude-projects.yaml` by default. Override the path with the `CLAUDE_CONFIG` environment variable:

```bash
CLAUDE_CONFIG=/path/to/my-projects.yaml ./claude-multi-manager.sh
```

The file must have a top-level `projects:` key containing a list of project definitions.

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

  - name: web-frontend
    group: frontend
    host: 192.168.1.11
    ssh_user: deploy
    sudo_user: webuser
    project_folder: /var/www/frontend
    claude_options: "--resume"
```

#### Field reference

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Unique project identifier |
| `group` | No | Group label for multi-project launches |
| `host` | Yes | Remote hostname or IP address |
| `ssh_user` | Yes | SSH login user |
| `sudo_user` | No | User to `su` to on the remote host |
| `project_folder` | Yes | Absolute path to the project on the remote host |
| `claude_options` | No | CLI flags passed to `claude` (defaults to `--resume`) |

### Usage

```bash
# Show usage and list available groups and projects
./claude-multi-manager.sh

# Show detailed help
./claude-multi-manager.sh --help

# Launch a single project in the current terminal
./claude-multi-manager.sh <project-name>

# Launch all projects in a group in a new tmux session
./claude-multi-manager.sh <group-name>

# Launch all groups, each in its own tmux session
./claude-multi-manager.sh all
```

After launching a group, attach to its tmux session:

```bash
tmux attach -t claude-<groupname>
```

When a Claude session ends or disconnects, each pane prompts you to choose:

```
[r] Reconnect   [s] Shell   [q] Quit
```

### How it works

1. **Config parsing** — `parse_config()` converts `~/.claude-projects.yaml` to JSON using an inline `python3` + PyYAML call. Subsequent lookups use `jq` against this JSON.
2. **Launcher generation** — for each project, two temporary scripts are created:
   - `/tmp/launcher-<name>.sh` (local): uploads the remote script via `scp`, opens an interactive SSH session with `-tt`, and wraps the connection in a reconnect loop.
   - `/tmp/remote-<name>.sh` (remote): sources the user environment, `cd`s to the project folder, optionally `su`s to the configured sudo user, and runs `claude` with the configured options.
3. **Single project** — the launcher script is `exec`'d directly in the current terminal.
4. **Group launch** — a tmux session named `claude-<group>` is created, split into tiled panes, and each pane runs one project's launcher.
5. **All groups** — each group gets its own tmux session created in sequence.
6. **Cleanup** — temporary scripts in `/tmp` are removed when the script exits.

### Troubleshooting

**SSH connection fails or prompts for a password**
Key-based authentication must be set up for each `ssh_user@host` pair. Run `ssh-copy-id <ssh_user>@<host>` and verify you can connect without a password prompt before using this tool.

**`python3` or PyYAML not found**
Config parsing requires `python3` and the `PyYAML` package. Install PyYAML with:
```bash
pip3 install pyyaml
# or on Debian/Ubuntu:
sudo apt install python3-yaml
```

**`jq` not found**
Install jq via your system package manager:
```bash
sudo apt install jq        # Debian/Ubuntu
brew install jq            # macOS
```

**`tmux` not found**
Install tmux via your system package manager:
```bash
sudo apt install tmux      # Debian/Ubuntu
brew install tmux          # macOS
```

**`claude: command not found` on the remote host**
The Claude Code CLI must be installed and on the `PATH` for the user that runs it on the remote host (the `sudo_user` if set, otherwise the `ssh_user`). Check the PATH sourced by non-interactive shells on the remote, or add the Claude binary location to the user's `.bashrc` or `.profile`.

**Groups show more than 4 panes**
tmux tiled layouts work best with up to 4 panes. Groups with more than 4 projects will open additional panes but the layout may not tile evenly.

CPSM addresses this with arbitrary multi-screen layouts and per-pane geometry — see the design spec.
