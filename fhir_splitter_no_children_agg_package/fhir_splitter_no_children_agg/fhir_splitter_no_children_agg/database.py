from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    select,
    text,
    update,
)

from .splitter import _normalize_child_basename


SCD_COLUMNS = {"meta_versionid", "record_ind", "effective_start_date", "effective_end_date"}


def _safe_identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    if cleaned == "_row_id":
        cleaned = "data_row_id"
    if cleaned != value or len(cleaned) > 48:
        suffix = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned[:48]}_{suffix}"
    return cleaned


def _collect_table_rows(
    grouped: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    tables: dict[str, list[dict[str, Any]]] = defaultdict(list)
    child_prefixes: dict[str, str] = {}
    for resource_type, data in grouped.items():
        parent_table = _safe_identifier(f"fhir_{resource_type}_parent")
        parents = data.get("parent") or []
        seen_parents: dict[str, str] = {}
        for parent in parents:
            resource_id = parent.get("id")
            if resource_id is not None:
                resource_id = str(resource_id)
                record_hash = _record_hash(parent)
                if resource_id in seen_parents and seen_parents[resource_id] != record_hash:
                    raise RuntimeError(
                        f"Multiple different {resource_type} resources with id {resource_id} "
                        "were uploaded together. Load each version separately."
                    )
                seen_parents[resource_id] = record_hash
        tables[parent_table].extend(parents)
        child_prefixes[parent_table] = f"fhir_{resource_type}_"
        for child_key, rows in (data.get("children") or {}).items():
            child_name = _normalize_child_basename(resource_type, child_key)
            child_table = _safe_identifier(f"fhir_{child_name}")
            tables[child_table].extend({**row, "meta_versionid": row.get("meta_versionid")} for row in rows or [])
    return dict(tables), child_prefixes


def _record_hash(row: dict[str, Any]) -> str:
    snapshot = row.get("__snapshot")
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except json.JSONDecodeError:
            pass
    canonical = json.dumps(snapshot if snapshot is not None else row, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {}
    for key, value in row.items():
        if key == "_row_id":
            continue
        if key == "__snapshot" and isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("Invalid resource snapshot") from exc
        elif key != "__snapshot":
            if isinstance(value, (dict, list)):
                raise ValueError(f"Unnormalized structured value in column {key}")
            if value is not None and key != "__array_index" and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
        normalized[_safe_identifier(str(key))] = value
    if normalized.get("id") is not None:
        normalized["id"] = str(normalized["id"])
    if normalized.get("meta_versionid") is not None:
        normalized["meta_versionid"] = str(normalized["meta_versionid"])
    return normalized


def _column_type(name: str):
    if name == "__snapshot":
        return JSON(none_as_null=True)
    if name == "__array_index":
        return Integer()
    return Text()


def _ensure_table(
    connection,
    table_name: str,
    rows: list[dict[str, Any]],
    is_parent: bool,
) -> Table:
    normalized_rows = [_normalize_row(row) for row in rows]
    excluded = {"id", "__record_hash"} | (SCD_COLUMNS if is_parent else set())
    column_names = sorted({key for row in normalized_rows for key in row} - excluded)
    column_types = {
        name: _column_type(name)
        for name in column_names
    }
    metadata = MetaData()
    columns = [
        Column("_row_id", Integer, primary_key=True, autoincrement=True),
        Column("id", String(255), nullable=True),
    ]
    if is_parent:
        columns.append(Column("__record_hash", String(64), nullable=True))
    if is_parent:
        columns.extend(
            [
                Column("meta_versionid", String(64), nullable=True),
                Column("record_ind", String(1), nullable=True),
                Column("effective_start_date", DateTime, nullable=True),
                Column("effective_end_date", DateTime, nullable=True),
            ]
        )
    columns.extend(Column(name, column_types[name], nullable=True) for name in column_names)
    Table(table_name, metadata, *columns).create(connection, checkfirst=True)

    preparer = connection.dialect.identifier_preparer
    quoted_table = preparer.quote(table_name)
    column_info = {column["name"]: column for column in inspect(connection).get_columns(table_name)}

    if "id" not in column_info:
        connection.execute(text(f"ALTER TABLE {quoted_table} ADD COLUMN `id` VARCHAR(255) NULL"))
    elif isinstance(column_info["id"]["type"], JSON):
        migration_column = "__id_text_migration"
        if migration_column in column_info:
            raise RuntimeError(f"Incomplete id column migration in table {table_name}")
        quoted_migration = preparer.quote(migration_column)
        quoted_id = preparer.quote("id")
        connection.execute(
            text(f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_migration} VARCHAR(255) NULL")
        )
        if connection.dialect.name == "mysql":
            conversion = f"JSON_UNQUOTE({quoted_id})"
        elif connection.dialect.name == "sqlite":
            conversion = f"CAST(json_extract({quoted_id}, '$') AS TEXT)"
        else:
            raise RuntimeError("Migrating the existing JSON id column is supported on MySQL and SQLite")
        connection.execute(
            text(f"UPDATE {quoted_table} SET {quoted_migration} = {conversion} WHERE {quoted_id} IS NOT NULL")
        )
        connection.execute(text(f"ALTER TABLE {quoted_table} DROP COLUMN {quoted_id}"))
        if connection.dialect.name == "mysql":
            connection.execute(
                text(f"ALTER TABLE {quoted_table} RENAME COLUMN {quoted_migration} TO {quoted_id}")
            )
        else:
            connection.execute(
                text(f"ALTER TABLE {quoted_table} RENAME COLUMN {quoted_migration} TO {quoted_id}")
            )

    column_info = {column["name"] for column in inspect(connection).get_columns(table_name)}
    if is_parent and "__record_hash" not in column_info:
        connection.execute(
            text(f"ALTER TABLE {quoted_table} ADD COLUMN `__record_hash` VARCHAR(64) NULL")
        )
        column_info.add("__record_hash")

    scd_definitions = {
        "meta_versionid": "VARCHAR(64)",
        "record_ind": "VARCHAR(1)",
        "effective_start_date": "DATETIME",
        "effective_end_date": "DATETIME",
    }
    for scd_name, scd_type in (scd_definitions.items() if is_parent else ()):
        if scd_name not in column_info:
            connection.execute(
                text(f"ALTER TABLE {quoted_table} ADD COLUMN {preparer.quote(scd_name)} {scd_type} NULL")
            )
            if scd_name == "effective_start_date":
                # rows loaded before SCD2 support become the current version
                connection.execute(
                    text(
                        f"UPDATE {quoted_table} SET record_ind = 'Y', effective_start_date = :now "
                        f"WHERE record_ind IS NULL"
                    ),
                    {"now": datetime.now(timezone.utc).replace(tzinfo=None)},
                )
    column_info = {column["name"]: column for column in inspect(connection).get_columns(table_name)}
    for column_name in column_names:
        quoted_column = preparer.quote(column_name)
        sql_type = column_types[column_name].compile(dialect=connection.dialect)
        if column_name not in column_info:
            connection.execute(
                text(f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_column} {sql_type} NULL")
            )
            continue

        if isinstance(column_info[column_name]["type"], JSON) and isinstance(column_types[column_name], Text):
            migration_column = _safe_identifier(f"__{column_name}_text_migration")
            if migration_column in column_info:
                raise RuntimeError(f"Incomplete text column migration in table {table_name}: {column_name}")
            quoted_migration = preparer.quote(migration_column)
            connection.execute(
                text(f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_migration} {sql_type} NULL")
            )
            if connection.dialect.name == "mysql":
                conversion = f"JSON_UNQUOTE({quoted_column})"
            elif connection.dialect.name == "sqlite":
                conversion = f"CAST(json_extract({quoted_column}, '$') AS TEXT)"
            else:
                raise RuntimeError("Migrating existing JSON text columns is supported on MySQL and SQLite")
            connection.execute(
                text(
                    f"UPDATE {quoted_table} SET {quoted_migration} = {conversion} "
                    f"WHERE {quoted_column} IS NOT NULL"
                )
            )
            connection.execute(text(f"ALTER TABLE {quoted_table} DROP COLUMN {quoted_column}"))
            connection.execute(
                text(f"ALTER TABLE {quoted_table} RENAME COLUMN {quoted_migration} TO {quoted_column}")
            )

    return Table(table_name, MetaData(), autoload_with=connection)


def _ensure_parent_id_index(connection, table: Table) -> None:
    # SCD2 keeps several versions per id, so id can no longer be unique.
    for index in inspect(connection).get_indexes(table.name):
        if index["unique"] and index["column_names"] == ["id"]:
            Index(index["name"], table.c.id, unique=True).drop(connection)
    index_name = _safe_identifier(f"ix_{table.name}_id")
    Index(index_name, table.c.id).create(connection, checkfirst=True)


def _next_version(existing_versions: list[Any]) -> str:
    numeric = []
    for version in existing_versions:
        try:
            numeric.append(int(str(version)))
        except (TypeError, ValueError):
            pass
    return str((max(numeric) if numeric else len(existing_versions)) + 1)


def _scd2_plan(connection, table: Table, rows_by_id: dict[str, dict[str, Any]]):
    """Decide per id whether to insert a new version. Returns (rows to insert, versions by id, new ids, updated ids)."""
    existing: dict[str, list[Any]] = defaultdict(list)
    if rows_by_id:
        for rid, version, record_hash, record_ind in connection.execute(
            select(table.c.id, table.c.meta_versionid, table.c.__record_hash, table.c.record_ind).where(
                table.c.id.in_(list(rows_by_id))
            )
        ).all():
            existing[rid].append((None if version is None else str(version), record_hash, record_ind))

    to_insert: list[dict[str, Any]] = []
    versions: dict[str, str] = {}
    unchanged: set[str] = set()
    updated: set[str] = set()
    for rid, row in rows_by_id.items():
        history = existing.get(rid, [])
        incoming_version = row.get("meta_versionid")
        if incoming_version is not None:
            # versioned resource: a version already stored is never reloaded
            if any(version == incoming_version for version, _, _ in history):
                unchanged.add(rid)
                versions[rid] = incoming_version
                continue
        else:
            current = [h for h in history if h[2] == "Y"]
            if current and current[0][1] == row["__record_hash"]:
                unchanged.add(rid)
                versions[rid] = current[0][0]
                continue
            row["meta_versionid"] = _next_version([version for version, _, _ in history])
        versions[rid] = row["meta_versionid"]
        if history:
            updated.add(rid)
        to_insert.append(row)
    return to_insert, versions, unchanged, updated


def _child_row_key(table: Table, row: dict[str, Any]) -> str:
    values = {
        column.name: row.get(column.name)
        for column in table.columns
        if column.name not in {"_row_id", "record_ind", "effective_start_date", "effective_end_date"}
    }
    return json.dumps(values, sort_keys=True, default=str, ensure_ascii=False)

def _match_column_types(table: Table, row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: json.dumps(value, ensure_ascii=False)
        if isinstance(table.c[key].type, Text) and isinstance(value, (dict, list))
        else value
        for key, value in row.items()
    }


def _expire_active(connection, table: Table, ids: set[str], now: datetime) -> None:
    if ids:
        connection.execute(
            update(table)
            .where(table.c.id.in_(list(ids)), table.c.record_ind == "Y")
            .values(record_ind="N", effective_end_date=now)
        )


def save_grouped_resources(grouped: dict[str, dict[str, Any]]) -> dict[str, Any]:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")

    engine = create_engine(database_url, pool_pre_ping=True)
    table_rows, child_prefixes = _collect_table_rows(grouped)
    parent_tables = set(child_prefixes)
    inserted_rows = 0
    updated_rows = 0
    unchanged_rows = 0

    try:
        with engine.begin() as connection:
            existing_names = set(inspect(connection).get_table_names())
            child_tables_by_parent: dict[str, set[str]] = {}
            all_table_rows = dict(table_rows)
            for parent_table, prefix in child_prefixes.items():
                matching_children = {
                    name for name in existing_names
                    if name.startswith(prefix) and name != parent_table
                }
                matching_children.update(
                    name for name in table_rows
                    if name.startswith(prefix) and name != parent_table
                )
                child_tables_by_parent[parent_table] = matching_children
                for child_table in matching_children:
                    all_table_rows.setdefault(child_table, [])

            tables = {
                table_name: _ensure_table(
                    connection,
                    table_name,
                    rows,
                    table_name in parent_tables,
                )
                for table_name, rows in all_table_rows.items()
            }
            for parent_table in parent_tables:
                _ensure_parent_id_index(connection, tables[parent_table])
                tables[parent_table] = Table(parent_table, MetaData(), autoload_with=connection)

            changed_ids_by_parent: dict[str, set[str]] = {}
            unchanged_ids_by_parent: dict[str, set[str]] = {}
            versions_by_parent: dict[str, dict[str, str]] = {}
            parent_rows_to_save: dict[str, list[dict[str, Any]]] = {}
            for parent_table in parent_tables:
                rows_by_id: dict[str, dict[str, Any]] = {}
                rows_without_id = []
                for source_row in table_rows.get(parent_table, []):
                    row = _normalize_row(source_row)
                    row["__record_hash"] = _record_hash(source_row)
                    if row.get("id") is None:
                        rows_without_id.append(row)
                    else:
                        rows_by_id[row["id"]] = row

                table = tables[parent_table]
                rows_to_save, versions, unchanged_ids, updated_ids = _scd2_plan(connection, table, rows_by_id)
                changed_ids = {row["id"] for row in rows_to_save}
                unchanged_rows += len(unchanged_ids)
                updated_rows += len(updated_ids)
                inserted_rows += len(changed_ids) - len(updated_ids)
                for row in rows_without_id:
                    row["__record_hash"] = _record_hash(row)
                    rows_to_save.append(row)
                    inserted_rows += 1

                changed_ids_by_parent[parent_table] = changed_ids
                unchanged_ids_by_parent[parent_table] = unchanged_ids
                versions_by_parent[parent_table] = versions
                parent_rows_to_save[parent_table] = rows_to_save

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            for parent_table, changed_ids in changed_ids_by_parent.items():
                _expire_active(connection, tables[parent_table], changed_ids, now)

            scd_values = {"record_ind": "Y", "effective_start_date": now, "effective_end_date": None}
            for parent_table, rows in parent_rows_to_save.items():
                table = tables[parent_table]
                for row in rows:
                    connection.execute(table.insert().values(**_match_column_types(table, row), **scd_values))

            for table_name, source_rows in table_rows.items():
                if table_name in parent_tables:
                    continue
                table = tables[table_name]
                parent_table = next(
                    (parent for parent, prefix in child_prefixes.items() if table_name.startswith(prefix)),
                    None,
                )
                if parent_table is None:
                    raise RuntimeError(f"No parent table found for child table {table_name}")
                changed_ids = changed_ids_by_parent[parent_table]
                seen: set[str] = set()
                if changed_ids:
                    for existing_row in connection.execute(
                        select(table).where(table.c.id.in_(list(changed_ids)))
                    ).mappings():
                        seen.add(_child_row_key(table, dict(existing_row)))
                for source_row in source_rows:
                    row = _normalize_row(source_row)
                    resource_id = row.get("id")
                    if resource_id is not None and resource_id not in changed_ids:
                        continue
                    if resource_id is not None:
                        row["meta_versionid"] = versions_by_parent[parent_table][resource_id]
                    row = _match_column_types(table, row)
                    key = _child_row_key(table, row)
                    if key in seen:
                        continue
                    seen.add(key)
                    connection.execute(table.insert().values(**row))
                    inserted_rows += 1
    finally:
        engine.dispose()

    return {
        "tables": sorted(table_rows),
        "rows_inserted": inserted_rows,
        "rows_updated": updated_rows,
        "rows_unchanged": unchanged_rows,
    }