
from __future__ import annotations
import json
import hashlib
import uuid
from typing import Any, Dict, List, Tuple

def _to_pascal(name: str) -> str:
    parts = [p for p in str(name).replace('-', '_').split('_') if p]
    return "_".join(p[:1].upper() + p[1:] for p in parts)

def _normalize_child_basename(resource: str, child_key: str) -> str:
    # Returns e.g. Patient_Identifier (no .json)
    prefix = (resource + "_").lower()
    n = str(child_key)
    if n.lower().startswith(prefix):
        n = n[len(prefix):]
    return f"{resource}_{_to_pascal(n)}"

def _normalize_child_filename(resource: str, child_key: str) -> str:
    return f"Output/{resource}/{_normalize_child_basename(resource, child_key)}.json"

def _reference_to_id(ref: Any) -> Any:
    if not isinstance(ref, str):
        return ref
    if ref.startswith("#") and len(ref) > 1:
        return ref[1:]
    value = ref.strip().split("#")[0].split("?")[0]
    if value.startswith("urn:"):
        return value.rsplit(":", 1)[-1]
    segments = [s for s in value.split("/") if s]
    if "_history" in segments:
        segments = segments[:segments.index("_history")]
    return segments[-1] if segments else ref

def split_parent_children(obj: Dict[str, Any]) -> Dict[str, Any]:
    res_type = obj.get("resourceType", "Unknown")
    id_val = obj.get("id"); mv = None
    meta = obj.get("meta")
    if isinstance(meta, dict):
        mv = meta.get("versionid") or meta.get("versionId")

    resource_key = (
        hashlib.sha256(json.dumps([res_type, id_val], ensure_ascii=False).encode("utf-8")).hexdigest()
        if id_val is not None else uuid.uuid4().hex
    )
    parent_row: Dict[str, Any] = {"resourceType": res_type, "__resource_key": resource_key}
    if id_val is not None: parent_row["id"] = id_val
    if mv is not None: parent_row["meta_versionid"] = mv
    parent_row["__snapshot"] = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    children: Dict[str, List[Dict[str, Any]]] = {}

    def ensure(name: str) -> List[Dict[str, Any]]:
        if name not in children: children[name] = []
        return children[name]

    def node_id(location: tuple) -> str:
        identity = [resource_key, location]
        raw = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def flatten(node: dict, path: tuple, location: tuple, row: dict, parent_node_id: str | None):
        for key, value in node.items():
            field_path = path + (key,)
            field_location = location + (key,)
            if isinstance(value, dict):
                flatten(value, field_path, field_location, row, parent_node_id)
            elif isinstance(value, list):
                if len(value) == 1 and isinstance(value[0], str):
                    row["_".join(field_path)] = value[0]
                else:
                    emit(value, field_path, field_location, parent_node_id)
            else:
                row["_".join(field_path)] = _reference_to_id(value) if key == "reference" else value

    def emit(items: list, path: tuple, location: tuple, parent_node_id: str | None):
        rows = ensure("_".join(path))
        for index, item in enumerate(items):
            item_location = location + (index,)
            current_node_id = node_id(item_location)
            row: Dict[str, Any] = {
                "__node_id": current_node_id,
                "__parent_node_id": parent_node_id,
                "__array_index": index,
                "__resource_key": resource_key,
            }
            if id_val is not None: row["id"] = id_val
            if mv is not None: row["meta_versionid"] = mv
            if isinstance(item, dict):
                flatten(item, path, item_location, row, current_node_id)
            elif isinstance(item, list):
                emit(item, path + ("item",), item_location, current_node_id)
            else:
                row["_".join(path)] = item
            rows.append(row)

    payload = {k: v for k, v in obj.items() if k not in ("id", "versionid", "resourceType")}
    if isinstance(meta, dict):
        payload["meta"] = {k: v for k, v in meta.items() if k not in ("versionid", "versionId")}
    flatten(payload, (), (), parent_row, None)
    return {"parent": [parent_row], "children": children}

def collect_resources_except_bundle(obj: Any, out: List[Dict[str, Any]] | None = None) -> List[Dict[str, Any]]:
    if out is None: out = []
    if isinstance(obj, dict):
        if obj.get("resourceType") == "Bundle":
            for e in obj.get("entry", []):
                if isinstance(e, dict) and "resource" in e:
                    collect_resources_except_bundle(e["resource"], out)
        elif "resourceType" in obj and obj.get("resourceType") != "Bundle":
            out.append(obj)
        else:
            for v in obj.values():
                collect_resources_except_bundle(v, out)
    elif isinstance(obj, list):
        for item in obj: collect_resources_except_bundle(item, out)
    return out

def group_by_resource_type(objs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for r in objs:
        rt = r.get("resourceType", "Unknown")
        res = split_parent_children(r)
        g = grouped.setdefault(rt, {"parent": [], "children": {}})
        g["parent"].extend(res.get("parent") or [])
        for k, rows in (res.get("children") or {}).items():
            g["children"].setdefault(k, []).extend(rows)
    return grouped

def ui_mapping(by_type: Dict[str, Dict[str, Any]]):
    """Return labels for UI: parent.json path and a list of child file paths,
       plus a PascalCase child_tables list e.g. Patient_Identifier.
    """
    out = {}
    for rt, data in by_type.items():
        child_keys = list((data.get("children") or {}).keys())
        child_files = [_normalize_child_filename(rt, k) for k in child_keys]
        child_tables = [_normalize_child_basename(rt, k) for k in child_keys]
        out[rt] = {
            "parent_label": f"Output/{rt}/parent.json",
            "children_files": child_files,
            "child_tables": child_tables,
        }
    return out

def build_zip_bytes(by_type: Dict[str, Dict[str, Any]]):
    """Create a ZIP with:
       - Output/<RT>/parent.json
       - Output/<RT>/<RT>_<Child>.json (one file per child)
       (No aggregated children.json)
    """
    import io, zipfile
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for rt, data in by_type.items():
            zf.writestr(f"Output/{rt}/parent.json", json.dumps(data.get("parent") or [], ensure_ascii=False, indent=2))
            for child_key, rows in (data.get("children") or {}).items():
                zf.writestr(_normalize_child_filename(rt, child_key), json.dumps(rows or [], ensure_ascii=False, indent=2))
    mem.seek(0)
    return mem.getvalue()
