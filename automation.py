#!/usr/bin/env python3
"""Notion -> (lookup approved bricks) -> OpenAI draft -> write back.

Designed for a UX content bricks database.

Runs idempotently:
- Only processes rows where Status == STATUS_NEEDS_DRAFT
- By default, does NOT overwrite AI Draft if already present (set OVERWRITE_EXISTING=1 to force)

Configuration is via environment variables (see README.md).
"""

from __future__ import annotations

import json
import os
import sys
import time
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
from difflib import SequenceMatcher

NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")
NOTION_API_BASE = "https://api.notion.com/v1"
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")


def _env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and (val is None or val.strip() == ""):
        raise RuntimeError(f"Missing required env var: {name}")
    return val  # type: ignore


@dataclass
class Config:
    # Notion
    notion_token: str
    database_id: str

    # OpenAI
    openai_api_key: str
    openai_model: str

    # Notion properties
    prop_title: str
    prop_status: str
    prop_component_type: str
    prop_component_slot: str
    prop_approved_copy: str
    prop_matched_approved: str
    prop_ai_draft: str
    prop_ai_notes: str

    # Status values
    status_needs_draft: str
    status_approved: str
    status_after: str

    # Behaviour
    max_items_per_run: int
    max_candidates: int
    overwrite_existing: bool
    dry_run: bool


def load_config() -> Config:
    return Config(
        notion_token=_env("NOTION_TOKEN", required=True),
        database_id=_env("NOTION_DATABASE_ID", required=True),
        openai_api_key=_env("OPENAI_API_KEY", required=True),
        openai_model=_env("OPENAI_MODEL", default="gpt-4o-mini"),

        prop_title=_env("PROP_TITLE", default="Name"),
        prop_status=_env("PROP_STATUS", default="Status"),
        prop_component_type=_env("PROP_COMPONENT_TYPE", default="Component type"),
        prop_component_slot=_env("PROP_COMPONENT_SLOT", default="Component slot"),
        prop_approved_copy=_env("PROP_APPROVED_COPY", default="Approved copy"),
        prop_matched_approved=_env("PROP_MATCHED_APPROVED", default="Matched approved"),
        prop_ai_draft=_env("PROP_AI_DRAFT", default="AI draft"),
        prop_ai_notes=_env("PROP_AI_NOTES", default="AI notes"),

        status_needs_draft=_env("STATUS_NEEDS_DRAFT", default="Needs draft"),
        status_approved=_env("STATUS_APPROVED", default="Approved"),
        status_after=_env("STATUS_AFTER", default="Needs review"),

        max_items_per_run=int(_env("MAX_ITEMS_PER_RUN", default="10")),
        max_candidates=int(_env("MAX_CANDIDATES", default="25")),
        overwrite_existing=_env("OVERWRITE_EXISTING", default="0") == "1",
        dry_run=_env("DRY_RUN", default="0") == "1",
    )


def notion_headers(cfg: Config) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {cfg.notion_token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def openai_headers(cfg: Config) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {cfg.openai_api_key}",
        "Content-Type": "application/json",
    }


def notion_post(cfg: Config, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{NOTION_API_BASE}{path}"
    r = requests.post(url, headers=notion_headers(cfg), data=json.dumps(payload), timeout=60)
    if not r.ok:
        raise RuntimeError(f"Notion POST {path} failed: {r.status_code} {r.text}")
    return r.json()


def notion_patch(cfg: Config, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{NOTION_API_BASE}{path}"
    r = requests.patch(url, headers=notion_headers(cfg), data=json.dumps(payload), timeout=60)
    if not r.ok:
        raise RuntimeError(f"Notion PATCH {path} failed: {r.status_code} {r.text}")
    return r.json()


def extract_title(page: Dict[str, Any], prop_title: str) -> str:
    props = page.get("properties", {})
    p = props.get(prop_title)
    if not p:
        return ""
    if p.get("type") != "title":
        # try to find first title property
        for k, v in props.items():
            if v.get("type") == "title":
                return "".join([t.get("plain_text", "") for t in v.get("title", [])]).strip()
        return ""
    return "".join([t.get("plain_text", "") for t in p.get("title", [])]).strip()


def extract_rich_text(page: Dict[str, Any], prop_name: str) -> str:
    p = page.get("properties", {}).get(prop_name)
    if not p:
        return ""
    t = p.get("type")
    if t == "rich_text":
        return "".join([x.get("plain_text", "") for x in p.get("rich_text", [])]).strip()
    if t == "title":
        return "".join([x.get("plain_text", "") for x in p.get("title", [])]).strip()
    if t == "select":
        sel = p.get("select")
        return (sel or {}).get("name", "")
    if t == "multi_select":
        return ", ".join([x.get("name", "") for x in p.get("multi_select", []) if x.get("name")])
    if t == "date":
        d = p.get("date")
        if not d:
            return ""
        return d.get("start", "")
    return ""


def extract_select(page: Dict[str, Any], prop_name: str) -> str:
    p = page.get("properties", {}).get(prop_name)
    if not p or p.get("type") != "select":
        return ""
    sel = p.get("select")
    return (sel or {}).get("name", "")


def extract_field_text(page: Dict[str, Any], prop_name: str) -> str:
    # Best-effort for type/slot fields which might be select or rich_text
    return extract_rich_text(page, prop_name)


def query_needs_draft(cfg: Config) -> List[Dict[str, Any]]:
    payload = {
        "filter": {
            "property": cfg.prop_status,
            "select": {"equals": cfg.status_needs_draft},
        },
        "page_size": min(cfg.max_items_per_run, 100),
    }
    out: List[Dict[str, Any]] = []
    cursor = None
    while True:
        if cursor:
            payload["start_cursor"] = cursor
        res = notion_post(cfg, f"/databases/{cfg.database_id}/query", payload)
        out.extend(res.get("results", []))
        if len(out) >= cfg.max_items_per_run:
            return out[: cfg.max_items_per_run]
        if not res.get("has_more"):
            return out
        cursor = res.get("next_cursor")


def query_approved_candidates(cfg: Config, component_type: str, component_slot: str) -> List[Dict[str, Any]]:
    # Both fields may be stored as select or rich_text. Notion filter differs.
    # We'll try select-equals first; if it returns 0, fall back to contains on rich_text.

    def _query(filter_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
        payload = {"filter": filter_obj, "page_size": min(cfg.max_candidates, 100)}
        res = notion_post(cfg, f"/databases/{cfg.database_id}/query", payload)
        return res.get("results", [])

    base = {"property": cfg.prop_status, "select": {"equals": cfg.status_approved}}

    # Attempt select filters
    select_filter = {
        "and": [
            base,
            {"property": cfg.prop_component_type, "select": {"equals": component_type}},
            {"property": cfg.prop_component_slot, "select": {"equals": component_slot}},
        ]
    }
    results = _query(select_filter)
    if results:
        return results[: cfg.max_candidates]

    # Fall back: try rich_text contains
    rt_filter = {
        "and": [
            base,
            {"property": cfg.prop_component_type, "rich_text": {"contains": component_type}},
            {"property": cfg.prop_component_slot, "rich_text": {"contains": component_slot}},
        ]
    }
    results = _query(rt_filter)
    return results[: cfg.max_candidates]


def normalise_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def rank_candidates(
    request_title: str,
    request_context: str,
    candidates: List[Dict[str, Any]],
    cfg: Config,
) -> List[Tuple[float, Dict[str, Any], str]]:
    """Return list of (score, page, approved_copy_text) sorted desc."""
    req = normalise_text(request_title + " " + request_context)
    ranked: List[Tuple[float, Dict[str, Any], str]] = []
    for p in candidates:
        title = extract_title(p, cfg.prop_title)
        approved = extract_rich_text(p, cfg.prop_approved_copy)
        hay = normalise_text(title + " " + approved)
        if not hay:
            continue
        score = SequenceMatcher(None, req, hay).ratio()
        ranked.append((score, p, approved))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked


def openai_chat(cfg: Config, messages: List[Dict[str, str]], temperature: float = 0.4) -> str:
    payload = {
        "model": cfg.openai_model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    r = requests.post(
        f"{OPENAI_API_BASE}/chat/completions",
        headers=openai_headers(cfg),
        data=json.dumps(payload),
        timeout=120,
    )
    if not r.ok:
        raise RuntimeError(f"OpenAI chat.completions failed: {r.status_code} {r.text}")
    data = r.json()
    return data["choices"][0]["message"]["content"]


def build_prompt(
    request: Dict[str, Any],
    cfg: Config,
    ranked: List[Tuple[float, Dict[str, Any], str]],
) -> Tuple[str, str, str, str]:
    title = extract_title(request, cfg.prop_title)
    ctype = extract_field_text(request, cfg.prop_component_type)
    cslot = extract_field_text(request, cfg.prop_component_slot)

    # Build a compact context string
    context = {
        "title": title,
        "component_type": ctype,
        "component_slot": cslot,
    }

    candidates_payload = []
    for score, page, approved in ranked[:3]:
        candidates_payload.append(
            {
                "score": round(score, 3),
                "title": extract_title(page, cfg.prop_title),
                "approved_copy": approved,
                "page_id": page.get("id"),
            }
        )

    system = (
        "You are a UX writing assistant. You must help draft copy for a product UI. "
        "Prefer reusing or adapting existing approved copy when it is close. "
        "Keep it clear, concise, and consistent in tone. Use British spelling."
    )

    user = {
        "task": "Draft UX copy for this request row.",
        "request": context,
        "approved_candidates": candidates_payload,
        "instructions": [
            "If an approved candidate is a strong match, adapt it rather than inventing a new pattern.",
            "Return 2 variants max unless no candidates exist (then return up to 3).",
            "Do not include markdown.",
            "Output JSON with keys: matched_summary, draft_variants, recommended, notes.",
            "draft_variants is an array of objects: {label, text}.",
            "recommended is one of the labels from draft_variants.",
        ],
    }

    return system, json.dumps(user, ensure_ascii=False), title, f"{ctype} | {cslot}"


def notion_update_row(
    cfg: Config,
    page_id: str,
    matched_approved: str,
    ai_draft: str,
    ai_notes: str,
    new_status: Optional[str],
) -> None:
    props: Dict[str, Any] = {
        cfg.prop_matched_approved: {"rich_text": [{"type": "text", "text": {"content": matched_approved}}]},
        cfg.prop_ai_draft: {"rich_text": [{"type": "text", "text": {"content": ai_draft}}]},
        cfg.prop_ai_notes: {"rich_text": [{"type": "text", "text": {"content": ai_notes}}]},
    }
    if new_status:
        props[cfg.prop_status] = {"select": {"name": new_status}}

    if cfg.dry_run:
        print(f"[DRY_RUN] Would update page {page_id} with status={new_status!r}")
        return

    notion_patch(cfg, f"/pages/{page_id}", {"properties": props})


def format_matched(ranked: List[Tuple[float, Dict[str, Any], str]], cfg: Config) -> str:
    if not ranked:
        return "No close approved matches found for this type/slot."
    lines = []
    for score, page, approved in ranked[:3]:
        title = extract_title(page, cfg.prop_title) or "(untitled)"
        pid = page.get("id", "")
        lines.append(f"- {title} (score {score:.2f}, id {pid}): {approved}")
    return "\n".join(lines)


def main() -> int:
    cfg = load_config()

    needs = query_needs_draft(cfg)
    if not needs:
        print("No rows with Status = Needs draft.")
        return 0

    print(f"Found {len(needs)} row(s) needing draft.")

    for idx, row in enumerate(needs, 1):
        page_id = row.get("id")
        title = extract_title(row, cfg.prop_title)
        ctype = extract_field_text(row, cfg.prop_component_type)
        cslot = extract_field_text(row, cfg.prop_component_slot)
        existing_draft = extract_rich_text(row, cfg.prop_ai_draft)

        if existing_draft and not cfg.overwrite_existing:
            print(f"[{idx}/{len(needs)}] Skip (AI draft already exists): {title}")
            continue

        if not ctype or not cslot:
            print(f"[{idx}/{len(needs)}] Skip (missing type/slot): {title}")
            continue

        print(f"[{idx}/{len(needs)}] Processing: {title} | {ctype} / {cslot}")

        candidates = query_approved_candidates(cfg, ctype, cslot)
        ranked = rank_candidates(title, f"{ctype} {cslot}", candidates, cfg)

        system, user_json, _t, _k = build_prompt(row, cfg, ranked)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_json},
        ]

        try:
            content = openai_chat(cfg, messages)
            payload = json.loads(content)
        except Exception as e:
            print(f"  ERROR calling OpenAI or parsing JSON: {e}")
            continue

        matched_summary = payload.get("matched_summary", "")
        variants = payload.get("draft_variants", [])
        recommended = payload.get("recommended", "")
        notes = payload.get("notes", "")

        # Build draft text for Notion
        draft_lines = []
        if isinstance(variants, list):
            for v in variants[:3]:
                if not isinstance(v, dict):
                    continue
                label = str(v.get("label", "Variant"))
                text = str(v.get("text", "")).strip()
                if not text:
                    continue
                mark = " (recommended)" if label == recommended else ""
                draft_lines.append(f"{label}{mark}: {text}")
        ai_draft = "\n".join(draft_lines).strip() or str(content)

        matched_field = matched_summary.strip() or format_matched(ranked, cfg)
        ai_notes = (notes or "").strip()
        if ai_notes:
            ai_notes = ai_notes
        else:
            ai_notes = "Auto-generated draft. Review against approved library before shipping."

        notion_update_row(
            cfg=cfg,
            page_id=page_id,
            matched_approved=matched_field,
            ai_draft=ai_draft,
            ai_notes=ai_notes,
            new_status=cfg.status_after,
        )

        # Be polite to APIs
        time.sleep(0.6)

    print("Done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
