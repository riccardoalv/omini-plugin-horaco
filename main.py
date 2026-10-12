"""Omini plugin for Horaco and compatible managed switches (run by the Omini core)."""

from omini_sdk import plugin

from omini_horaco.collect import apply, backup, collect, test

plugin.collect(collect)
plugin.test(test)
plugin.apply(apply)
if hasattr(plugin, "backup"):  # an older Omini has no backups
    plugin.backup(backup)

if __name__ == "__main__":
    plugin.run()
