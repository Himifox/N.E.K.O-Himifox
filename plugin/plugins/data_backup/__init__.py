"""N.E.K.O native data backup plugin."""

from __future__ import annotations

import asyncio

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
)

from .backup import BACKUP_GROUPS, BackupEngine, BackupError


@neko_plugin
class DataBackupPlugin(NekoPluginBase):
    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._engine: BackupEngine | None = None

    @lifecycle(id="startup")
    async def startup(self, **_):
        config = await self.config.dump(timeout=5.0)
        backup_config = config.get("backup", {}) if isinstance(config, dict) else {}
        retention = (
            backup_config.get("retention", 10)
            if isinstance(backup_config, dict)
            else 10
        )
        plugin_data = self.data_path().resolve(strict=False)
        data_root = plugin_data.parents[2]
        self._engine = BackupEngine(
            data_root, plugin_data / "snapshots", retention=int(retention)
        )
        self.register_static_ui("static", cache_control="no-store")
        self.set_list_actions(
            [
                {
                    "id": "open_ui",
                    "label": "打开备份管理",
                    "kind": "ui",
                    "target": f"/plugin/{self.plugin_id}/ui/",
                    "open_in": "new_tab",
                }
            ]
        )
        return Ok(self._engine.status())

    def _backup(self) -> BackupEngine:
        if self._engine is None:
            raise BackupError("backup plugin is not started")
        return self._engine

    @plugin_entry(
        id="backup_status",
        name="查看备份状态",
        description="返回固定备份组与已有快照。",
        timeout=30.0,
    )
    async def backup_status(self, **_):
        try:
            return Ok(await asyncio.to_thread(self._backup().status))
        except BackupError as exc:
            return Err(SdkError(str(exc)))

    @plugin_entry(
        id="backup_create",
        name="创建数据快照",
        description="为 core 或 assets 固定数据组创建快照。",
        input_schema={
            "type": "object",
            "properties": {"group": {"type": "string", "enum": list(BACKUP_GROUPS)}},
            "required": ["group"],
            "additionalProperties": False,
        },
        timeout=600.0,
    )
    async def backup_create(self, group: str, **_):
        try:
            return Ok(await asyncio.to_thread(self._backup().create_snapshot, group))
        except (BackupError, OSError) as exc:
            return Err(SdkError(str(exc)))

    @plugin_entry(
        id="backup_restore",
        name="恢复数据快照",
        description="仅在确认值与快照 ID 完全一致时恢复，并先创建安全快照。",
        input_schema={
            "type": "object",
            "properties": {
                "group": {"type": "string", "enum": list(BACKUP_GROUPS)},
                "snapshot_id": {"type": "string"},
                "confirmation": {"type": "string"},
            },
            "required": ["group", "snapshot_id", "confirmation"],
            "additionalProperties": False,
        },
        timeout=900.0,
    )
    async def backup_restore(
        self, group: str, snapshot_id: str, confirmation: str, **_
    ):
        if confirmation != snapshot_id:
            return Err(SdkError("confirmation must match the snapshot id"))
        try:
            result = await asyncio.to_thread(
                self._backup().restore_snapshot, group, snapshot_id
            )
            return Ok(result)
        except (BackupError, OSError) as exc:
            return Err(SdkError(str(exc)))

    @plugin_entry(
        id="backup_delete",
        name="删除数据快照",
        description="仅在确认值与快照 ID 完全一致时删除快照。",
        input_schema={
            "type": "object",
            "properties": {
                "group": {"type": "string", "enum": list(BACKUP_GROUPS)},
                "snapshot_id": {"type": "string"},
                "confirmation": {"type": "string"},
            },
            "required": ["group", "snapshot_id", "confirmation"],
            "additionalProperties": False,
        },
        timeout=300.0,
    )
    async def backup_delete(self, group: str, snapshot_id: str, confirmation: str, **_):
        if confirmation != snapshot_id:
            return Err(SdkError("confirmation must match the snapshot id"))
        try:
            return Ok(
                await asyncio.to_thread(
                    self._backup().delete_snapshot, group, snapshot_id
                )
            )
        except (BackupError, OSError) as exc:
            return Err(SdkError(str(exc)))


__all__ = ["DataBackupPlugin"]
