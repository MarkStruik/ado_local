from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

VSO_COMMAND_RE = re.compile(
    r"##vso\[(?P<command>[\w.]+)"
    r"(?P<props>(?:[\s]+[\w]+=[\"\']?[^\s\"\']+[\"\']?)*)"
    r"\](?P<value>.*)",
    re.IGNORECASE,
)
PROP_RE = re.compile(r"(\w+)=([\"\']?)([^\s\"\';]+)\2")


@dataclass
class VsoCommand:
    command: str
    properties: dict[str, str] = field(default_factory=dict)
    value: str = ""


def parse_vso_command(line: str) -> Optional[VsoCommand]:
    m = VSO_COMMAND_RE.match(line.strip())
    if not m:
        return None
    command = m.group("command").lower()
    props_str = m.group("props")
    value = m.group("value")
    properties: dict[str, str] = {}
    for pm in PROP_RE.finditer(props_str):
        properties[pm.group(1)] = pm.group(3)
    return VsoCommand(command=command, properties=properties, value=value)


class LoggingCommandProcessor:
    def __init__(self) -> None:
        self.variables: dict[str, str] = {}
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.build_number: Optional[str] = None
        self.task_result: Optional[str] = None
        self.attachments: list[dict[str, str]] = []
        self.summaries: list[str] = []
        self.secrets: list[str] = []

    def mask(self, line: str) -> str:
        masked = line
        for secret in self.secrets:
            if secret:
                masked = masked.replace(secret, "***")
        return masked

    def process_line(self, line: str) -> Optional[VsoCommand]:
        cmd = parse_vso_command(line)
        if cmd is None:
            return None
        self._handle_command(cmd)
        return cmd

    def _handle_command(self, cmd: VsoCommand) -> None:
        try:
            handler = getattr(self, f"_cmd_{cmd.command.replace('.', '_')}", None)
            if handler:
                handler(cmd)
            else:
                logger.debug(f"Unknown VSO command: {cmd.command}")
        except Exception as e:
            logger.warning(f"Failed to process VSO command '{cmd.command}': {e}")

    def _cmd_task_setvariable(self, cmd: VsoCommand) -> None:
        var_name = cmd.properties.get("variable")
        if var_name:
            self.variables[var_name] = cmd.value
        if cmd.properties.get("issecret", "").lower() == "true" and cmd.value:
            self.secrets.append(cmd.value)

    def _cmd_task_setsecret(self, cmd: VsoCommand) -> None:
        if cmd.value:
            self.secrets.append(cmd.value)

    def _cmd_task_complete(self, cmd: VsoCommand) -> None:
        self.task_result = cmd.properties.get("result", "succeeded").lower()

    def _cmd_task_logissue(self, cmd: VsoCommand) -> None:
        issue_type = cmd.properties.get("type", "warning")
        message = cmd.value
        if issue_type == "error":
            self.errors.append(message)
        else:
            self.warnings.append(message)

    def _cmd_build_updatebuildnumber(self, cmd: VsoCommand) -> None:
        self.build_number = cmd.value

    def _cmd_task_addattachment(self, cmd: VsoCommand) -> None:
        self.attachments.append({
            "type": cmd.properties.get("type", ""),
            "name": cmd.properties.get("name", ""),
            "path": cmd.value,
        })

    def _cmd_task_uploadsummary(self, cmd: VsoCommand) -> None:
        self.summaries.append(cmd.value)
