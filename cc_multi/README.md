# claude-multi-manager

A bash-based multi-project launcher that manages Claude Code CLI sessions across multiple remote hosts using tmux and SSH.

## Features

- Launch a single Claude session in the current terminal by project name
- Launch a group of projects in a tmux session with tiled panes (up to 4 per group)
- Launch all project groups at once, each in its own tmux session
- Automatic reconnect loop in each pane: reconnect, drop to shell, or quit after disconnect
- YAML config with per-project SSH user, sudo user, project folder, and Claude CLI options
- Temporary launcher scripts are cleaned up on exit

## Prerequisites

- bash 4+
- python3 with PyYAML
- jq
- tmux
- SSH with key-based authentication configured for all remote hosts
- Claude Code CLI installed on each remote host

See [claude-multi-manager-dependencies.md](./claude-multi-manager-dependencies.md) for full dependency details and installation instructions.

## Installation

1. Clone or copy `claude-multi-manager.sh` to a directory on your PATH, or run it directly.
2. Make it executable:

```bash
chmod +x claude-multi-manager.sh
```

3. Create your config file at `~/.claude-projects.yaml` (see Configuration below).

## Configuration

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

### Field Reference

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Unique project identifier |
| `group` | No | Group label for multi-project launches |
| `host` | Yes | Remote hostname or IP address |
| `ssh_user` | Yes | SSH login user |
| `sudo_user` | No | User to `su` to on the remote host |
| `project_folder` | Yes | Absolute path to the project on the remote host |
| `claude_options` | No | CLI flags passed to `claude` (defaults to `--resume`) |

## Usage

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

For example, with the config above:

```bash
# Open api-service directly in this terminal
./claude-multi-manager.sh api-service

# Open backend group (api-service + worker) in a tiled tmux session
./claude-multi-manager.sh backend
tmux attach -t claude-backend

# Open every group
./claude-multi-manager.sh all
```

When a Claude session ends or disconnects, each pane prompts you to choose:

```
[r] Reconnect   [s] Shell   [q] Quit
```

## How It Works

1. **Config parsing** - `parse_config()` converts `~/.claude-projects.yaml` to JSON using an inline `python3` + PyYAML call. All subsequent lookups use `jq` against this JSON.

2. **Launcher generation** - For each project, two temporary scripts are created:
   - `/tmp/launcher-<name>.sh` (local) - uploads the remote script via `scp`, then opens an interactive SSH session with `-tt`, and wraps the connection in a reconnect loop.
   - `/tmp/remote-<name>.sh` (remote) - sources the user environment, `cd`s to the project folder, optionally `su`s to the configured sudo user, and runs `claude` with the configured options.

3. **Single project** - The launcher script is `exec`'d directly in the current terminal.

4. **Group launch** - A tmux session named `claude-<group>` is created, split into tiled panes, and each pane runs one project's launcher.

5. **All groups** - Each group gets its own tmux session created in sequence.

6. **Cleanup** - Temporary scripts in `/tmp` are removed when the script exits.

## Troubleshooting

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
sudo brew install jq       # macOS
```

**`tmux` not found**
Install tmux via your system package manager:
```bash
sudo apt install tmux      # Debian/Ubuntu
sudo brew install tmux     # macOS
```

**`claude: command not found` on the remote host**
The Claude Code CLI must be installed and on the `PATH` for the user that runs it on the remote host (the `sudo_user` if set, otherwise the `ssh_user`). Check the PATH sourced by non-interactive shells on the remote, or add the Claude binary location to the user's `.bashrc` or `.profile`.

**Groups show more than 4 panes**
tmux tiled layouts work best with up to 4 panes. Groups with more than 4 projects will open additional panes but the layout may not tile evenly.

## License

MIT License - see LICENSE file for details.
