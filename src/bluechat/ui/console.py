"""Shared Rich console with conservative color behavior."""

from rich.console import Console

console = Console(highlight=False, soft_wrap=True)
