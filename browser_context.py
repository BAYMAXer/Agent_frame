"""Task-specific AX context and snapshot-bound viewport regions.

The complete snapshot remains owned by BrowserSession. This module only builds
a bounded projection; references and URLs are never invented or retargeted.
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import defaultdict
from typing import Any


_HEADER_KEYS = ("kind", "snapshot_id", "url", "title", "viewport", "ax_quality", "warnings", "nodes")
_ENTRY_ROLES = {"article", "listitem", "row", "treeitem", "searchresult"}
_CONTAINER_ROLES = {"RootWebArea", "WebArea", "document", "main", "list", "table",
                    "navigation", "generic", "group", "section", "search", "form"}
_INTERACTIVE = {"link", "button", "textbox", "searchbox", "combobox", "checkbox", "radio",
                "tab", "menuitem", "option", "switch", "listbox", "slider", "spinbutton"}
_STOPWORDS = {"the", "and", "for", "from", "with", "into", "this", "that", "then", "open",
              "click", "find", "search", "download", "paper", "papers", "result", "results",
              "website", "page", "browser", "first", "second", "confirm", "select", "input",
              "type", "button", "link", "box", "field", "pdf", "html"}
_CHINESE_ACTIONS = ("搜索结果", "搜索框", "相关论文", "浏览器", "下载", "搜索", "查找", "论文",
                    "打开", "点击", "选择", "输入", "确认", "网页", "第一个", "第二个", "结果", "相关")


def serialize_ax(block: dict) -> str:
    """The exact AX text sent to the model, excluding local selection metadata."""
    return json.dumps({key: block[key] for key in _HEADER_KEYS if key in block},
                      ensure_ascii=False, separators=(",", ":"))


def ax_quality(block: dict) -> str:
    """Classify legacy snapshots as well as captures with explicit readiness data.

    A named RootWebArea describes the document, not its visible content. Region
    selection cannot recover content from a tree containing only that root.
    """
    declared = block.get("ax_quality")
    if declared in {"ready", "degraded", "empty"}:
        return declared
    if any(isinstance(node, dict) and node.get("role") not in _CONTAINER_ROLES
           and (node.get("name") or node.get("value") or node.get("role") in _INTERACTIVE)
           for node in block.get("nodes", [])):
        return "ready"
    return "empty" if block.get("url") in {"", "about:blank", None} else "degraded"


def normalize_roi(raw: Any, snapshot_id: str) -> dict | None:
    """Validate a snapshot-bound [x, y, width, height] rectangle in [0, 1].

    Bare coordinates are deliberately invalid: a rectangle from a previous
    screenshot must not silently select nodes from a new page snapshot.
    """
    if not isinstance(raw, dict) or not snapshot_id or raw.get("snapshot_id") != snapshot_id:
        return None
    values = raw.get("bbox", raw.get("roi", raw.get("region")))
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        return None
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) for value in values):
        return None
    x, y, width, height = map(float, values)
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        return None
    return {"snapshot_id": snapshot_id, "bbox": [x, y, width, height]}


def _text(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _query(subtask: Any) -> str:
    if isinstance(subtask, str):
        return subtask
    if isinstance(subtask, dict):
        parts = [str(subtask.get(key) or "") for key in ("goal", "instructions", "completion_criteria")]
        keywords = subtask.get("keywords", [])
        parts.extend(str(item) for item in keywords if isinstance(item, str))
        return " ".join(parts)
    return str(subtask or "")


def _terms(query: str) -> set[str]:
    query = query.casefold()
    for action in _CHINESE_ACTIONS:
        query = query.replace(action, " ")
    return {term for term in re.findall(r"[\w\u4e00-\u9fff]+", query)
            if len(term) > 1 and term not in _STOPWORDS}


def _compact_node(node: dict) -> dict:
    compact = {key: node[key] for key in ("ref", "parent", "role") if node.get(key) is not None}
    for key, limit in (("name", 240), ("value", 160)):
        value = _text(node.get(key), limit)
        if value:
            compact[key] = value
    # Ref-based actions resolve against the original snapshot. Keep exact IDs
    # and hrefs; clipping a URL would misrepresent the destination.
    for key in ("id", "href"):
        if node.get(key):
            compact[key] = node[key]
    states = {key: value for key, value in (node.get("states") or {}).items()
              if key in {"checked", "selected", "expanded", "disabled", "focused", "required", "readonly"}
              and (value is not False or key in {"checked", "selected", "expanded"})}
    if states:
        compact["states"] = states
    if node.get("direct"):
        compact["direct"] = True
    return compact


class BrowserAXSelector:
    """Select atomic result entries, their ancestors, and essential controls."""

    def __init__(self, max_chars: int = 5000):
        if type(max_chars) is not int or max_chars < 128:
            raise ValueError("AX character budget must be an integer of at least 128")
        self.max_chars = max_chars

    def select(self, block: dict, subtask: Any, roi: Any = None,
               viewport_only: bool = False) -> tuple[dict, dict]:
        started = time.perf_counter()
        if not isinstance(block, dict) or block.get("kind") != "browser_ax":
            raise ValueError("Expected a browser_ax snapshot")
        nodes = [node for node in block.get("nodes", [])
                 if isinstance(node, dict) and node.get("ref") is not None]
        by_ref = {node["ref"]: node for node in nodes}
        order = {node["ref"]: index for index, node in enumerate(nodes)}
        children = defaultdict(list)
        for node in nodes:
            children[node.get("parent")].append(node["ref"])
        snapshot_id = block.get("snapshot_id", "")
        region = normalize_roi(roi, snapshot_id) if roi is not None else None
        invalid_roi = roi is not None and region is None
        query = _query(subtask)
        terms = _terms(query)
        viewport = block.get("viewport") or {}

        def ancestors(ref):
            result, seen = set(), set()
            while ref in by_ref and ref not in seen:
                seen.add(ref)
                result.add(ref)
                ref = by_ref[ref].get("parent")
            return result

        def descendants(ref):
            result, todo = set(), [ref]
            while todo:
                current = todo.pop()
                if current in result or current not in by_ref:
                    continue
                result.add(current)
                todo.extend(children[current])
            return result

        def compact_refs(refs):
            # AX commonly repeats a title in both a link and its StaticText.
            result = set(refs)
            for ref in refs:
                node = by_ref[ref]
                parent = by_ref.get(node.get("parent"), {})
                if (node.get("role") in {"StaticText", "staticText", "text"}
                        and not children[ref] and node.get("name")
                        and _text(node["name"]) == _text(parent.get("name"))):
                    result.discard(ref)
            return result

        def visible(node):
            bounds = node.get("bounds")
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
                return bool(node.get("in_viewport")) and region is None
            try:
                x, y, width, height = map(float, bounds)
                vw, vh = float(viewport["width"]), float(viewport["height"])
                x -= float(viewport.get("x", viewport.get("scroll_x", 0)))
                y -= float(viewport.get("y", viewport.get("scroll_y", 0)))
            except (KeyError, TypeError, ValueError):
                return False
            if not all(math.isfinite(value) for value in (x, y, width, height, vw, vh)) or vw <= 0 or vh <= 0:
                return False
            rx, ry, rw, rh = region["bbox"] if region else [0, 0, 1, 1]
            return (width > 0 and height > 0 and x < (rx + rw) * vw and x + width > rx * vw
                    and y < (ry + rh) * vh and y + height > ry * vh)

        group_cache = {}

        def result_group(ref):
            path = ancestors(ref)
            entries = [item for item in path if by_ref[item].get("role") in _ENTRY_ROLES]
            if entries:
                # Nearest semantic result boundary, never the whole main/list.
                entry = next(item for item in self._parent_chain(ref, by_ref) if item in entries)
                if entry not in group_cache:
                    group_cache[entry] = descendants(entry)
                return ("entry", entry), group_cache[entry]
            heading = next((item for item in self._parent_chain(ref, by_ref)
                            if by_ref[item].get("role") == "heading"), None)
            if heading is None:
                parent = by_ref[ref].get("parent")
                siblings = children[parent]
                index = siblings.index(ref) if ref in siblings else -1
                # A PDF link/snippet may be a sibling of a title heading.
                prior = [item for item in siblings[:index + 1] if by_ref[item].get("role") == "heading"]
                heading = prior[-1] if prior else None
            if heading is not None:
                if heading not in group_cache:
                    sibling_refs = children[by_ref[heading].get("parent")]
                    index = sibling_refs.index(heading)
                    members = descendants(heading)
                    # Local neighbours only. Do not expand the root subtree.
                    for sibling in sibling_refs[index + 1:]:
                        node = by_ref[sibling]
                        if node.get("role") == "heading" or node.get("role") in _CONTAINER_ROLES | _ENTRY_ROLES:
                            break
                        members.update(descendants(sibling))
                    if index > 0:
                        prior = by_ref[sibling_refs[index - 1]]
                        if (prior.get("role") == "link" and
                                not any(by_ref[item].get("role") == "heading" for item in sibling_refs[:index - 1]) and
                                (re.search(r"\b(pdf|html)\b", str(prior.get("name", "")), re.I)
                                 or str(prior.get("href", "")).lower().endswith(".pdf"))):
                            members.update(descendants(prior["ref"]))
                    group_cache[heading] = members
                return ("heading", heading), group_cache[heading]
            return ("node", ref), {ref}

        mandatory = {}
        search_inputs = []
        for node in nodes:
            role, ref = node.get("role"), node["ref"]
            is_search_input = role == "searchbox" or (role in {"textbox", "combobox"}
                and re.search(r"search|query|搜索|检索", str(node.get("name", "")), re.I))
            if is_search_input:
                search_inputs.append(ref)
            if role in {"dialog", "alert", "status", "search"}:
                mandatory[("context", ref)] = descendants(ref) | ancestors(ref)
            elif is_search_input or (role == "button" and re.fullmatch(
                    r"search|搜索|检索|搜索按钮", str(node.get("name", "")).strip(), re.I)):
                mandatory[("context", ref)] = ancestors(ref)

        candidates = {}
        for node in nodes:
            if node.get("role") in _CONTAINER_ROLES or node.get("role") in _ENTRY_ROLES:
                continue
            text = (str(node.get("name", "")) + " " + str(node.get("value", ""))).casefold()
            score = sum(1 for term in terms if term in text)
            if not score and region is None and not viewport_only:
                continue
            key, members = result_group(node["ref"])
            if (region is not None or viewport_only) and not any(visible(by_ref[item]) for item in members):
                continue
            if key not in candidates:
                candidates[key] = {"refs": members | ancestors(node["ref"]), "score": score}
            else:
                candidates[key]["score"] = max(candidates[key]["score"], score)

        # A new query is not present in the input yet. A unique search input is
        # enough to dispatch typing without an unnecessary region-model call.
        search_intent = bool(re.search(r"search|搜索|检索|输入|type", query, re.I))
        has_match = any(item["score"] for item in candidates.values())
        if search_intent and len(search_inputs) == 1 and not has_match:
            has_match = True
        best = max((item["score"] for item in candidates.values()), default=0)
        ambiguous = (region is None and not viewport_only and best > 0
                     and sum(item["score"] == best for item in candidates.values()) > 1)

        selected = {"kind": "browser_ax", "snapshot_id": snapshot_id, "nodes": []}
        selected["ax_quality"] = ax_quality(block)
        for key in ("url", "title"):
            if block.get(key):
                selected[key] = _text(block[key], 160) if key == "title" else block[key]
        if viewport:
            selected["viewport"] = {key: viewport[key] for key in ("width", "height", "x", "y") if key in viewport}
        if block.get("warnings"):
            selected["warnings"] = [_text(item, 160) for item in block["warnings"]]
        header_overflow = False
        if len(serialize_ax(selected)) > self.max_chars:
            header_overflow = True
            for key in ("url", "title", "viewport"):
                selected.pop(key, None)
            if len(serialize_ax(selected)) > self.max_chars:
                selected["warnings"] = ["AX context exceeds budget; inspect the full local snapshot"]
            if len(serialize_ax(selected)) > self.max_chars:
                raise ValueError("Snapshot identity does not fit the AX character budget")

        def render(refs):
            payload = dict(selected)
            payload["nodes"] = [_compact_node(by_ref[ref]) for ref in sorted(compact_refs(refs), key=order.get)]
            return payload

        ranked = sorted(candidates.values(), key=lambda item: (-item["score"], min(order[ref] for ref in item["refs"])))
        all_groups = [*mandatory.values(), *(item["refs"] for item in ranked)]
        all_refs = set().union(*all_groups) if all_groups else set()
        candidate_chars = len(serialize_ax(render(all_refs)))
        chosen, omitted_groups = set(), 0
        for group in all_groups:
            proposal = chosen | group
            if len(serialize_ax(render(proposal))) <= self.max_chars:
                chosen = proposal
            elif not group <= chosen:
                omitted_groups += 1
        selected = render(chosen)
        # A blank managed page should go straight to goto, without vision.
        has_content = any(node.get("direct") or (node.get("role") not in _CONTAINER_ROLES
                          and bool(node.get("name") or node.get("value"))) for node in nodes)
        blank_page = not has_content and str(block.get("url", "")).lower() in {"", "about:blank", "chrome://newtab/", "edge://newtab/"}
        reasons = []
        if invalid_roi:
            reasons.append("invalid_roi")
        if header_overflow or candidate_chars > self.max_chars:
            reasons.append("over_budget")
        if not has_match and region is None and not viewport_only and not blank_page:
            reasons.append("no_match")
        if ambiguous:
            reasons.append("ambiguous_target")
        metadata = {"snapshot_id": snapshot_id, "original_count": len(nodes), "selected_count": len(selected["nodes"]),
                    "original": len(nodes), "selected": len(selected["nodes"]), "total": len(nodes),
                    "omitted": len(nodes) - len(selected["nodes"]), "omitted_groups": omitted_groups,
                    "original_serialized_chars": len(json.dumps(block, ensure_ascii=False, separators=(",", ":"))),
                    "candidate_serialized_chars": candidate_chars, "serialized_chars": len(serialize_ax(selected)),
                    "needs_region": bool(reasons), "reason": ",".join(reasons) or ("blank_page" if blank_page else "selected"),
                    "roi": region, "viewport_only": bool(viewport_only),
                    "exposed_refs": [node["ref"] for node in selected["nodes"]],
                    "selection_ms": round((time.perf_counter() - started) * 1000, 3)}
        metadata["full_serialized_chars"] = metadata["original_serialized_chars"]
        selected["_ax_selected"] = True
        selected["_ax_metadata"] = metadata
        return selected, metadata

    @staticmethod
    def _parent_chain(ref, by_ref):
        seen = set()
        while ref in by_ref and ref not in seen:
            seen.add(ref)
            yield ref
            ref = by_ref[ref].get("parent")
