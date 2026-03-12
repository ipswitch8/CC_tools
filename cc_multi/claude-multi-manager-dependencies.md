# claude-multi-manager.sh — Dependency Reference

**Script**: `/home/x/cc_multi/claude-multi-manager.sh`
**Last Updated**: 2026-03-12
**Description**: Bash-based multi-project launcher that manages Claude AI sessions across remote hosts via SSH and tmux.

---

## Summary Table

| # | Dependency | Type | Required | Location |
|---|-----------|------|----------|----------|
| 1 | `bash` | System binary | Required | Local host |
| 2 | `python3` | System binary | Required | Local host |
| 3 | `jq` | System binary | Required | Local host |
| 4 | `tmux` | System binary | Required (group launches) | Local host |
| 5 | `ssh` | System binary | Required | Local host |
| 6 | `scp` | System binary | Required | Local host |
| 7 | `sort` | System binary | Required | Local host |
| 8 | `head` | System binary | Required | Local host |
| 9 | `tr` | System binary | Required | Local host |
| 10 | `chmod` | System binary | Required | Local host |
| 11 | `rm` | System binary | Required | Local host |
| 12 | `cat` | System binary | Required | Local host |
| 13 | `read` | Bash built-in | Required | Local host |
| 14 | `sleep` | System binary | Required | Local host |
| 15 | `clear` | System binary | Required | Remote host |
| 16 | `su` | System binary | Required (sudo_user) | Remote host |
| 17 | `PyYAML` | Python library | Required | Local host |
| 18 | `json` | Python stdlib | Required | Local host |
| 19 | `~/.claude-projects.yaml` | Config file | Required | Local host |
| 20 | `claude` | CLI tool | Required | Remote host |
| 21 | `bash` (remote) | System binary | Required | Remote host |
| 22 | `~/.bashrc` / `~/.profile` | Shell init files | Required | Remote host |
| 23 | `/tmp/launcher-{name}.sh` | Generated file | Transient | Local host |
| 24 | `/tmp/remote-{name}.sh` | Generated file | Transient | Remote host |
| 25 | `CLAUDE_CONFIG` | Environment variable | Optional | Local host |
| 26 | `TERM` | Environment variable | Set by script | Remote host |
| 27 | `LANG` | Environment variable | Set by script | Remote host |
| 28 | `/tmp` directory | Filesystem path | Required | Local host |
| 29 | SSH key auth / agent | Auth mechanism | Required | Local host |
| 30 | `su` permission | OS permission | Conditional | Remote host |
| 31 | tmux (remote check) | System binary | Required | Local host |

---

## Section 1: Local System Binaries

These tools must be installed and available in `PATH` on the machine running the script.

### bash
- **Minimum version**: bash 4.0+ (required for associative arrays and `${var,,}` lowercase syntax)
- **Usage**: Script interpreter; uses bash-specific features including arrays, `readarray`, `[[ ]]` conditionals, `${var,,}` lowercase expansion, and `set -o pipefail`
- **Notes**: `/bin/sh` is not sufficient; the shebang must resolve to bash 4+. macOS ships bash 3.2 by default — users on macOS must install a newer bash via Homebrew.

### python3
- **Usage**: Invoked inline to parse the YAML config file and emit JSON for `jq` to consume (line 24)
- **Command pattern**: `python3 -c "import yaml, json; ..."`
- **Notes**: python2 is not supported. The inline call uses both `yaml` (third-party) and `json` (stdlib).

### jq
- **Usage**: Extensively used to query the JSON output of the python3 YAML parser — extracts fields such as `name`, `group`, `host`, `ssh_user`, `sudo_user`, `project_folder`, and `claude_options` from each project entry
- **Notes**: Must be jq 1.5 or newer. Not a standard system package on all distributions; commonly installed via `apt`, `brew`, or downloaded as a static binary.

### tmux
- **Usage**: Creates named sessions and panes for group launches; sets tmux options (e.g., `remain-on-exit`); arranges pane layouts
- **Notes**: Required for group-mode launches. Single-project launches may invoke tmux as well depending on implementation. Must be installed locally.

### ssh
- **Usage**: Opens remote connections to each project's host using the `-tt` flag to force pseudo-TTY allocation, enabling interactive Claude sessions
- **Notes**: Relies on key-based authentication or SSH agent forwarding. Password-based SSH is not handled and will cause the script to hang.

### scp
- **Usage**: Uploads the generated `/tmp/remote-{name}.sh` launcher script to the remote host before executing it
- **Notes**: Shares the same authentication requirements as `ssh`. Both must be able to reach the target host without interactive prompts.

### sort
- **Usage**: Invoked with the `-u` flag to deduplicate group names when building the group selection menu
- **Notes**: Standard POSIX utility; present on all supported platforms.

### head
- **Usage**: Used with `-n1` to extract the first match from filtered output (e.g., selecting a project by name)
- **Notes**: Standard POSIX utility.

### tr
- **Usage**: Character translation — lowercase conversion and character deletion operations during input normalization
- **Notes**: Standard POSIX utility.

### chmod
- **Usage**: Sets executable permissions (`+x`) on the generated `/tmp/launcher-{name}.sh` scripts before they are run
- **Notes**: Standard POSIX utility.

### rm
- **Usage**: Cleans up temporary scripts from `/tmp` after use
- **Notes**: Standard POSIX utility.

### cat
- **Usage**: Used within heredoc constructs to generate the content of launcher and remote scripts
- **Notes**: Standard POSIX utility.

### sleep
- **Usage**: Introduces delays between operations (e.g., waiting for SSH connections to establish or tmux panes to initialize)
- **Notes**: Standard POSIX utility.

---

## Section 2: Remote Host System Binaries

These tools must be installed and accessible on each remote host defined in the config file.

### clear
- **Usage**: Invoked at the start of the remote session script to reset the terminal display before launching Claude
- **Notes**: Present on most Linux/Unix systems. Requires a valid `TERM` environment variable (set to `xterm-256color` by the script).

### su
- **Usage**: Used to switch to the `sudo_user` specified in the project config (`su - $sudo_user`) before invoking the Claude CLI
- **Notes**: Only required when the `sudo_user` field is set in the project config and differs from `ssh_user`. The `ssh_user` account must have permission to `su` to the target user, either via passwordless `su` configuration or sudo rules.

### bash (remote)
- **Usage**: Remote scripts are executed with `bash -il` (interactive login shell) to ensure the full user environment — PATH, nvm, pyenv, etc. — is loaded before Claude is invoked
- **Notes**: Must support the `-il` flags. Same bash 4+ version recommendation applies if any remote-side bash scripting is involved.

### claude
- **Usage**: The primary application being launched. Invoked as `claude $claude_opts` where `claude_opts` comes from the project config's `claude_options` field
- **Notes**: Must be installed and in PATH for the target user's login shell on the remote host. The script does not verify its presence before attempting to launch it.

---

## Section 3: Python Libraries

Used within the inline `python3 -c` invocation on the local host.

### PyYAML (`yaml` module)
- **Install**: `pip install pyyaml` or system package (e.g., `python3-yaml`)
- **Usage**: Parses the YAML configuration file (`~/.claude-projects.yaml`) into Python data structures
- **Notes**: This is a third-party library and is not part of the Python standard library. It must be installed in the Python 3 environment that the `python3` binary resolves to. If using a virtual environment or pyenv, ensure PyYAML is available in the active environment.

### json (stdlib)
- **Usage**: Serializes the parsed YAML data to a JSON string, which is then consumed by `jq`
- **Notes**: Part of the Python standard library. No installation required.

---

## Section 4: Configuration Files

### `~/.claude-projects.yaml`
- **Path**: `$HOME/.claude-projects.yaml` (default) or the path specified by the `CLAUDE_CONFIG` environment variable
- **Required**: Yes — the script cannot function without a valid config file
- **Format**: YAML list of project objects

**Expected project object fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Required | Unique identifier for the project |
| `group` | string | Optional | Group label for launching multiple projects together |
| `host` | string | Required | Remote hostname or IP address |
| `ssh_user` | string | Required | Username for SSH connection |
| `sudo_user` | string | Optional | Username to `su` to on the remote host |
| `project_folder` | string | Required | Absolute path to the project directory on the remote host |
| `claude_options` | string | Optional | Additional CLI flags passed directly to the `claude` command |

**Minimal example**:
```yaml
- name: my-project
  host: dev.example.com
  ssh_user: deploy
  project_folder: /home/deploy/my-project
```

**Full example**:
```yaml
- name: api-service
  group: backend
  host: 192.168.1.10
  ssh_user: ubuntu
  sudo_user: appuser
  project_folder: /srv/api-service
  claude_options: --dangerously-skip-permissions
```

---

## Section 5: Generated and Temporary Files

These files are created at runtime and are not permanent.

### `/tmp/launcher-{name}.sh`
- **Location**: Local host
- **Lifecycle**: Created immediately before use; removed by the script after the session ends (or on error)
- **Purpose**: A local shell script that wraps the SSH invocation for a specific project, used to launch the session in a tmux pane
- **Notes**: The `/tmp` directory must be writable by the user running the script.

### `/tmp/remote-{name}.sh`
- **Location**: Initially created on local host, then uploaded via `scp` to the remote host's `/tmp/`
- **Lifecycle**: Uploaded before the session starts; should be cleaned up after use
- **Purpose**: The script that runs on the remote host — sources the user environment, optionally calls `su`, changes to the project directory, sets environment variables, and invokes `claude`
- **Notes**: The remote user must have write access to `/tmp` on the remote host. The file is made executable via `chmod` before execution.

---

## Section 6: Environment Variables

### `CLAUDE_CONFIG`
- **Required**: No (optional override)
- **Default**: `$HOME/.claude-projects.yaml`
- **Usage**: If set, the script uses this path instead of the default config file location
- **Example**: `export CLAUDE_CONFIG=/etc/claude/projects.yaml`

### `TERM`
- **Required**: No (set by script)
- **Value**: `xterm-256color`
- **Usage**: Set in the remote environment before launching Claude to ensure proper terminal color and capability support
- **Notes**: The remote host must have `xterm-256color` in its terminfo database, which is standard on most Linux systems.

### `LANG`
- **Required**: No (set by script)
- **Value**: `C.UTF-8`
- **Usage**: Set in the remote environment to ensure consistent UTF-8 character encoding for Claude's output
- **Notes**: The `C.UTF-8` locale must be available on the remote host. Most modern Linux distributions include it by default.

---

## Section 7: Implicit and Infrastructure Dependencies

These are not explicit binary or library dependencies but are required for the script to function correctly.

### `/tmp` directory (local)
- The script writes launcher scripts to `/tmp`. This directory must exist and be writable by the executing user. This is a safe assumption on all standard Linux/Unix systems.

### SSH key-based authentication
- Neither `ssh` nor `scp` invocations prompt for passwords. The local user must have an SSH key pair with the public key installed in `~/.ssh/authorized_keys` on each remote host, or an SSH agent must be running with the appropriate key loaded.
- Password-based SSH will cause the script to hang indefinitely waiting for input.

### `su` permission on remote host
- When `sudo_user` is configured and differs from `ssh_user`, the `ssh_user` account must be permitted to run `su - $sudo_user` without a password. This typically requires either a PAM configuration change or a `sudo` rule granting passwordless `su` access.

### Remote `/tmp` directory
- The remote execution script is uploaded to `/tmp` on the remote host via `scp`. The `ssh_user` must have write access to `/tmp` on the remote host.

### Remote network reachability
- Each `host` value in the config must be reachable from the local machine on SSH port 22 (or the configured SSH port). Firewalls, VPNs, or jump hosts are not handled by the script.

---

## Section 8: Installation Checklist

Use this checklist to verify all dependencies are satisfied before running the script.

### Local Host

- [ ] `bash` version 4.0 or newer (`bash --version`)
- [ ] `python3` available in PATH (`python3 --version`)
- [ ] `PyYAML` installed for the active python3 (`python3 -c "import yaml; print(yaml.__version__)"`)
- [ ] `jq` installed (`jq --version`)
- [ ] `tmux` installed (`tmux -V`)
- [ ] `ssh` and `scp` installed (`ssh -V`)
- [ ] Standard utilities present: `sort`, `head`, `tr`, `chmod`, `rm`, `cat`, `sleep`
- [ ] `~/.claude-projects.yaml` exists and is valid YAML, or `CLAUDE_CONFIG` is set to a valid path
- [ ] SSH key loaded in agent or key file configured for each remote host
- [ ] `/tmp` is writable

### Remote Host (per project)

- [ ] `bash` available with `-il` support
- [ ] `claude` CLI installed and in PATH for the target user's login shell
- [ ] `clear` available
- [ ] `su` available and `ssh_user` can `su` to `sudo_user` without a password (if `sudo_user` is configured)
- [ ] `/tmp` is writable by `ssh_user`
- [ ] `~/.bashrc` and/or `~/.profile` correctly configure PATH and any required environment (e.g., nvm, pyenv) so that `claude` is found
- [ ] `xterm-256color` terminfo entry present
- [ ] `C.UTF-8` locale available

---

## Quick Install Reference

### Install jq

```bash
# Debian/Ubuntu
sudo apt-get install jq

# RHEL/CentOS/Fedora
sudo dnf install jq

# macOS
brew install jq
```

### Install PyYAML

```bash
pip3 install pyyaml

# Or via system package manager (Debian/Ubuntu)
sudo apt-get install python3-yaml
```

### Install tmux

```bash
# Debian/Ubuntu
sudo apt-get install tmux

# RHEL/CentOS/Fedora
sudo dnf install tmux

# macOS
brew install tmux
```

### Install bash 4+ on macOS

```bash
brew install bash
# Then update /etc/shells and chsh, or invoke the script explicitly with the new bash path
```
