# -*- coding: utf-8 -*-
"""
PowerShellQuoter — safe argument quoting for pwsh -Command invocations.

Strategy (§7.4):
- Default style: single-quoted strings.  PowerShell single-quoted strings
  prevent variable expansion and command substitution.  An embedded single
  quote is escaped by doubling it (``''``).
- Switch to double-quote style when the value contains a single quote AND
  does not contain ``$`` or a backtick — in that case the value can be
  expressed more cleanly without escaping every quote.  Inside double-quoted
  strings an embedded ``"`` is escaped as `` `" `` and a backtick as ` `` `.

Boolean parameters become bare switches (``-Detached``), not ``-Detached:$true``.
None-valued parameters are omitted entirely.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class PowerShellQuoter:
    """Build safe PowerShell command strings for use with ``pwsh -Command``."""

    # ------------------------------------------------------------------
    # Single-argument quoting
    # ------------------------------------------------------------------

    def quote_argument(self, s: str) -> str:
        r"""Return a PowerShell-safe quoted form of *s*.

        Quoting strategy:

        * **Single-quote style** (default): ``'value'``.  Embedded single
          quotes are doubled: ``it's`` → ``'it''s'``.
        * **Double-quote style** (fallback when *s* contains ``'`` but not
          ``$`` or ``\``): ``"value"``.  Embedded ``"`` → `` `" ``;
          embedded backtick → ` `` `.

        Args:
            s: Raw string to quote.

        Returns:
            A PowerShell-safe quoted string (including surrounding quote
            characters).
        """
        # Decide style.
        has_single = "'" in s
        has_dollar = "$" in s
        has_backtick = "`" in s

        if has_single and not has_dollar and not has_backtick:
            # Double-quote style avoids having to escape every ' as ''.
            escaped = s.replace("`", "``").replace('"', '`"')
            return f'"{escaped}"'

        # Single-quote style (default).
        escaped = s.replace("'", "''")
        return f"'{escaped}'"

    # ------------------------------------------------------------------
    # Full-command assembly
    # ------------------------------------------------------------------

    def quote_command(self, cmdlet: str, params: dict[str, Any]) -> str:
        """Assemble a full PowerShell cmdlet invocation string.

        Rules:
        - ``bool`` parameter values become bare flag switches: ``-Detached``
          (when ``True``); when ``False`` the parameter is omitted.
        - ``None``-valued parameters are omitted.
        - All other values are converted to ``str`` and quoted via
          :meth:`quote_argument`.

        Args:
            cmdlet: The PowerShell cmdlet name (e.g. ``"New-PsmuxSession"``).
            params: Mapping of parameter name → value.

        Returns:
            A fully assembled command string suitable for passing as the
            argument to ``pwsh -NoProfile -Command``.

        Example::

            >>> q = PowerShellQuoter()
            >>> q.quote_command("New-PsmuxSession",
            ...     {"Name": "my-sess", "Width": 120, "Height": 40, "Detached": True})
            "New-PsmuxSession -Name 'my-sess' -Width '120' -Height '40' -Detached"
        """
        parts: list[str] = [cmdlet]
        for name, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                if value:
                    parts.append(f"-{name}")
                # False → omit the flag entirely.
                continue
            parts.append(f"-{name}")
            parts.append(self.quote_argument(str(value)))
        return " ".join(parts)
