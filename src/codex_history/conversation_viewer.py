from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files
from typing import Any


def _json_for_script(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


@lru_cache(maxsize=3)
def _browser_asset(filename: str) -> str:
    value = (
        files("codex_history")
        .joinpath("vendor")
        .joinpath(filename)
        .read_text(encoding="utf-8")
    )
    return value.replace("</script", "<\\/script")


def _contains_mermaid(payload: dict[str, Any]) -> bool:
    return any(
        re.search(r"(?im)^\s*(?:```|~~~)\s*mermaid\b", str(message.get("content") or ""))
        for message in payload.get("messages", [])
        if isinstance(message, dict)
    )


def render_conversation_html(payload: dict[str, Any]) -> str:
    title = str(payload.get("title") or "Codex conversation evidence")
    safe_title = (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    data = _json_for_script(payload)
    dompurify_js = _browser_asset("dompurify-3.4.12.min.js")
    marked_js = _browser_asset("marked-18.0.7.umd.js")
    mermaid_script = ""
    if _contains_mermaid(payload):
        mermaid_script = (
            '<script data-vendor="mermaid-11.16.0">'
            f'{_browser_asset("mermaid-11.16.0.min.js")}</script>'
        )
    return f"""<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; font-src 'none'; base-uri 'none'; form-action 'none'">
  <title>{safe_title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8f7;
      --panel: #f1f3f2;
      --panel-2: #ffffff;
      --line: #d4d9d6;
      --line-soft: #e4e7e5;
      --text: #202522;
      --muted: #59635e;
      --faint: #68716d;
      --accent: #bd4d34;
      --accent-soft: #f8e7e2;
      --user: #eaf3f6;
      --tool: #edf6ef;
      --goal: #faf4e1;
      --green: #1f6a3a;
      --blue: #176485;
      --amber: #765716;
      --danger: #a63f3f;
      --header: #ffffff;
      --hover: #e6eae7;
      --hover-strong: #dfe4e1;
      --hover-border: #7d8882;
      --input: #ffffff;
      --surface-soft: #fafbfa;
      --active-border: #d49482;
      --section: #353b38;
      --assistant: #353b38;
      --raw: #4f5954;
      --attachment: #ffffff;
      --selection: #ffffff;
      --shadow: rgba(27, 35, 31, .18);
      --header-h: 58px;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    :root[data-theme="dark"] {{
      color-scheme: dark;
      --bg: #111315;
      --panel: #171a1d;
      --panel-2: #1d2124;
      --line: #34393d;
      --line-soft: #282d30;
      --text: #ecebea;
      --muted: #9ca1a5;
      --faint: #71777b;
      --accent: #e86f51;
      --accent-soft: #3b2823;
      --user: #243139;
      --tool: #1a211d;
      --goal: #2d281a;
      --green: #77b58a;
      --blue: #79a8c5;
      --amber: #d5aa5d;
      --danger: #d87373;
      --header: #151719;
      --hover: #252a2d;
      --hover-strong: #2b3033;
      --hover-border: #596065;
      --input: #101214;
      --surface-soft: #15181a;
      --active-border: #684035;
      --section: #d8d7d5;
      --assistant: #d8d7d5;
      --raw: #b8bcbe;
      --attachment: #0d0f10;
      --selection: #141719;
      --shadow: rgba(0, 0, 0, .53);
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; min-height: 100%; background: var(--bg); color: var(--text); }}
    body {{ overflow: hidden; }}
    button, input {{ font: inherit; }}
    button {{ color: inherit; }}
    .icon {{ width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; pointer-events: none; }}
    .app-header {{
      height: var(--header-h); display: flex; align-items: center; gap: 14px; padding: 0 16px;
      border-bottom: 1px solid var(--line); background: var(--header);
    }}
    .brand {{ min-width: 0; flex: 1; }}
    .brand h1 {{ margin: 0; font-size: 15px; line-height: 1.25; font-weight: 650; letter-spacing: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .brand-meta {{ margin-top: 3px; color: var(--muted); font-size: 11px; letter-spacing: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .header-actions {{ flex: 0 0 auto; display: flex; gap: 7px; align-items: center; }}
    .icon-btn, .command-btn {{
      border: 1px solid var(--line); background: var(--panel-2); min-height: 34px; border-radius: 6px;
      display: inline-flex; align-items: center; justify-content: center; gap: 7px; cursor: pointer;
    }}
    .icon-btn {{ width: 34px; padding: 0; }}
    .command-btn {{ padding: 0 11px; font-size: 12px; }}
    .icon-btn:hover, .command-btn:hover {{ border-color: var(--hover-border); background: var(--hover); }}
    .icon-btn:focus-visible, .command-btn:focus-visible, input:focus-visible, .thread-item:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
    .workspace {{ height: calc(100vh - var(--header-h)); display: grid; grid-template-columns: 252px minmax(0, 1fr) 326px; }}
    .sidebar, .evidence-tray {{ min-height: 0; background: var(--panel); }}
    .sidebar {{ border-right: 1px solid var(--line); display: flex; flex-direction: column; }}
    .sidebar-head, .tray-head {{ height: 48px; padding: 0 12px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--line-soft); }}
    .section-title {{ margin: 0; font-size: 12px; font-weight: 650; letter-spacing: 0; color: var(--section); }}
    .count {{ color: var(--faint); font: 11px ui-monospace, SFMono-Regular, Consolas, monospace; }}
    .thread-list {{ overflow: auto; padding: 8px; }}
    .thread-item {{
      width: 100%; text-align: left; border: 1px solid transparent; border-radius: 6px; background: transparent;
      padding: 9px 10px; margin: 0 0 3px; cursor: pointer; display: block;
    }}
    .thread-item:hover {{ background: var(--hover); }}
    .thread-item.active {{ background: var(--accent-soft); border-color: var(--active-border); }}
    .thread-title {{ font-size: 12px; line-height: 1.45; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; overflow-wrap: anywhere; }}
    .thread-meta {{ margin-top: 5px; color: var(--muted); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }}
    .main {{ min-width: 0; min-height: 0; display: grid; grid-template-rows: auto auto minmax(0, 1fr); }}
    .filters {{ padding: 10px 14px; border-bottom: 1px solid var(--line-soft); background: var(--surface-soft); display: grid; grid-template-columns: minmax(180px, 1fr) auto; gap: 10px; align-items: center; }}
    .mobile-thread-select {{ display: none; width: 100%; height: 35px; padding: 0 9px; border-radius: 6px; border: 1px solid var(--line); background: var(--input); color: var(--text); }}
    .search-wrap {{ position: relative; }}
    .search-wrap .icon {{ position: absolute; left: 10px; top: 9px; color: var(--muted); }}
    .search {{ width: 100%; height: 35px; padding: 0 10px 0 34px; border-radius: 6px; border: 1px solid var(--line); background: var(--input); color: var(--text); }}
    .filter-controls {{ display: flex; gap: 8px; align-items: center; justify-content: flex-end; }}
    .view-modes {{ flex: 0 0 auto; display: flex; padding: 2px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); }}
    .mode-btn {{ height: 27px; padding: 0 7px; border: 0; border-radius: 4px; background: transparent; color: var(--muted); display: inline-flex; align-items: center; gap: 5px; font-size: 10px; cursor: pointer; }}
    .mode-btn .icon {{ width: 14px; height: 14px; }}
    .mode-btn.active {{ color: var(--text); background: var(--panel-2); box-shadow: 0 1px 2px var(--shadow); }}
    .role-filters {{ display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }}
    .role-chip {{ position: relative; }}
    .role-chip input {{ position: absolute; opacity: 0; pointer-events: none; }}
    .role-chip span {{ display: block; padding: 7px 9px; border: 1px solid var(--line); border-radius: 5px; color: var(--muted); font-size: 11px; cursor: pointer; }}
    .role-chip input:checked + span {{ color: var(--text); border-color: var(--hover-border); background: var(--panel-2); }}
    .range-row {{ min-height: 43px; display: flex; gap: 8px; align-items: center; padding: 7px 14px; border-bottom: 1px solid var(--line-soft); background: var(--bg); }}
    .range-row input {{ height: 30px; min-width: 0; color: var(--muted); background: var(--panel); border: 1px solid var(--line); border-radius: 5px; padding: 0 7px; font-size: 11px; }}
    .range-label {{ color: var(--faint); font-size: 10px; }}
    .result-summary {{ margin-left: auto; color: var(--muted); font: 11px ui-monospace, SFMono-Regular, Consolas, monospace; white-space: nowrap; }}
    .timeline {{ min-height: 0; overflow: auto; scroll-behavior: smooth; }}
    .timeline-inner {{ width: min(100%, 980px); margin: 0 auto; padding: 12px 18px 80px; }}
    .thread-banner {{ padding: 10px 0 13px; border-bottom: 1px solid var(--line-soft); margin-bottom: 5px; }}
    .thread-banner h2 {{ margin: 0; font-size: 14px; font-weight: 650; letter-spacing: 0; overflow-wrap: anywhere; }}
    .thread-banner p {{ margin: 5px 0 0; color: var(--muted); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }}
    .message {{ display: grid; grid-template-columns: 26px minmax(0, 1fr); gap: 9px; padding: 12px 5px; border-bottom: 1px solid var(--line-soft); }}
    .message.user {{ background: var(--user); border-radius: 6px; border-bottom-color: transparent; margin: 7px 0; padding: 12px; }}
    .message.tool_call, .message.tool_output {{ background: var(--tool); border-left: 2px solid var(--green); padding-left: 11px; }}
    .message.goal {{ background: var(--goal); border-left: 2px solid var(--amber); padding-left: 11px; }}
    .select-cell {{ padding-top: 1px; }}
    .select-cell input {{ accent-color: var(--accent); width: 15px; height: 15px; cursor: pointer; }}
    .message-head {{ min-width: 0; display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
    .role-name {{ font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--muted); }}
    .user .role-name {{ color: var(--blue); }}
    .assistant .role-name {{ color: var(--assistant); }}
    .tool_call .role-name, .tool_output .role-name {{ color: var(--green); }}
    .goal .role-name {{ color: var(--amber); }}
    .message-time {{ color: var(--faint); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }}
    .message-tools {{ margin-left: auto; display: flex; gap: 3px; }}
    .mini-btn {{ width: 27px; height: 27px; border: 0; background: transparent; color: var(--muted); border-radius: 5px; display: inline-flex; align-items: center; justify-content: center; cursor: pointer; }}
    .mini-btn:hover {{ color: var(--text); background: var(--hover-strong); }}
    .message-body {{ position: relative; min-width: 0; }}
    .message-content {{ margin: 0; overflow-wrap: anywhere; letter-spacing: 0; }}
    .source-content {{ min-width: 0; max-width: 100%; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-all; font: 13px/1.62 ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; }}
    .markdown-body {{ font: 14px/1.65 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .markdown-body > :first-child {{ margin-top: 0; }}
    .markdown-body > :last-child {{ margin-bottom: 0; }}
    .markdown-body h1, .markdown-body h2, .markdown-body h3, .markdown-body h4 {{ margin: 1.15em 0 .5em; line-height: 1.3; font-weight: 680; letter-spacing: 0; }}
    .markdown-body h1 {{ font-size: 20px; }}
    .markdown-body h2 {{ font-size: 17px; padding-bottom: 5px; border-bottom: 1px solid var(--line-soft); }}
    .markdown-body h3 {{ font-size: 15px; }}
    .markdown-body h4 {{ font-size: 14px; }}
    .markdown-body p, .markdown-body ul, .markdown-body ol, .markdown-body blockquote, .markdown-body pre, .markdown-body table {{ margin: .7em 0; }}
    .markdown-body ul, .markdown-body ol {{ padding-left: 1.7em; }}
    .markdown-body li + li {{ margin-top: .2em; }}
    .markdown-body blockquote {{ padding: 2px 0 2px 12px; border-left: 3px solid var(--line); color: var(--muted); }}
    .markdown-body code {{ padding: 2px 4px; border-radius: 4px; background: var(--panel); font: .9em ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; }}
    .markdown-body pre {{ max-width: 100%; overflow: auto; padding: 11px 12px; border: 1px solid var(--line-soft); border-radius: 6px; background: var(--panel); }}
    .markdown-body pre code {{ padding: 0; background: transparent; font-size: 12px; line-height: 1.55; }}
    .markdown-body table {{ display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; font-size: 12px; }}
    .markdown-body th, .markdown-body td {{ min-width: 90px; padding: 7px 9px; border: 1px solid var(--line); text-align: left; vertical-align: top; }}
    .markdown-body th {{ background: var(--panel); font-weight: 680; }}
    .markdown-body a {{ color: var(--blue); text-decoration-thickness: 1px; text-underline-offset: 2px; }}
    .markdown-body hr {{ border: 0; border-top: 1px solid var(--line); margin: 1.2em 0; }}
    .mermaid-host {{ max-width: 100%; overflow: auto; padding: 10px; border: 1px solid var(--line-soft); border-radius: 6px; background: var(--panel-2); text-align: center; }}
    .mermaid-host svg {{ display: inline-block; max-width: 100%; height: auto; }}
    .mermaid-error {{ text-align: left; color: var(--danger); }}
    .message-content.clamped {{ max-height: 340px; overflow: hidden; }}
    .message-content.clamped::after {{ content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 56px; background: linear-gradient(transparent, var(--bg)); pointer-events: none; }}
    .user .message-content.clamped::after {{ background: linear-gradient(transparent, var(--user)); }}
    .tool_call .message-content.clamped::after, .tool_output .message-content.clamped::after {{ background: linear-gradient(transparent, var(--tool)); }}
    .expand-btn {{ margin-top: 8px; border: 0; color: var(--blue); background: transparent; padding: 0; font-size: 11px; cursor: pointer; }}
    .provenance {{ margin-top: 9px; color: var(--faint); font: 10px/1.45 ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }}
    .raw {{ margin-top: 9px; border-top: 1px solid var(--line-soft); padding-top: 8px; }}
    .raw summary {{ color: var(--muted); font-size: 11px; cursor: pointer; }}
    .raw pre {{ white-space: pre-wrap; overflow-wrap: anywhere; font: 10px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; color: var(--raw); }}
    .attachments {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 8px; margin-top: 10px; }}
    .attachment {{ min-width: 0; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; background: var(--attachment); }}
    .attachment-image {{ width: 100%; display: block; max-height: 360px; object-fit: contain; border-bottom: 1px solid var(--line-soft); background: var(--surface-soft); }}
    .attachment-file {{ min-height: 76px; padding: 12px; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid var(--line-soft); background: var(--surface-soft); }}
    .attachment-file .icon {{ width: 28px; height: 28px; flex: 0 0 auto; color: var(--muted); }}
    .attachment-file-copy {{ min-width: 0; }}
    .attachment-file-copy strong {{ display: block; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .attachment-file-copy span {{ display: block; margin-top: 4px; color: var(--muted); font: 10px ui-monospace, SFMono-Regular, Consolas, monospace; }}
    .attachment-info {{ padding: 8px 10px; }}
    .attachment-name {{ margin: 0; font-size: 11px; font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .attachment-meta, .attachment-path {{ margin-top: 4px; color: var(--muted); font: 9px/1.45 ui-monospace, SFMono-Regular, Consolas, monospace; white-space: normal; overflow-wrap: anywhere; word-break: break-all; }}
    .attachment-status {{ color: var(--green); }}
    .attachment-status.skipped {{ color: var(--amber); }}
    .attachment-status.missing {{ color: var(--danger); }}
    .attachment-actions {{ display: flex; gap: 5px; margin-top: 8px; }}
    .attachment-action {{ min-height: 29px; padding: 0 8px; border: 1px solid var(--line); border-radius: 5px; background: var(--panel-2); color: var(--text); display: inline-flex; align-items: center; gap: 5px; font-size: 10px; cursor: pointer; }}
    .attachment-action:hover {{ border-color: var(--hover-border); background: var(--hover); }}
    .attachment-action .icon {{ width: 14px; height: 14px; }}
    .attachment-preview {{ margin-top: 7px; }}
    .attachment-preview summary {{ color: var(--blue); font-size: 10px; cursor: pointer; }}
    .attachment-preview pre {{ max-height: 220px; overflow: auto; margin: 6px 0 0; padding: 8px; border: 1px solid var(--line-soft); border-radius: 5px; background: var(--panel); white-space: pre-wrap; overflow-wrap: anywhere; font: 10px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; }}
    .load-more {{ width: 100%; height: 38px; margin-top: 12px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); cursor: pointer; }}
    .empty {{ padding: 60px 20px; text-align: center; color: var(--muted); font-size: 12px; }}
    .evidence-tray {{ border-left: 1px solid var(--line); display: grid; grid-template-rows: 48px auto minmax(0, 1fr) auto; }}
    .tray-actions {{ display: flex; gap: 5px; padding: 8px 10px; border-bottom: 1px solid var(--line-soft); }}
    .tray-actions .command-btn {{ flex: 1; padding: 0 7px; }}
    .selection-list {{ min-height: 0; overflow: auto; padding: 8px; }}
    .selection-item {{ display: grid; grid-template-columns: 20px minmax(0, 1fr) auto; gap: 7px; align-items: start; padding: 8px; border: 1px solid var(--line-soft); border-radius: 6px; margin-bottom: 6px; background: var(--selection); }}
    .selection-item.dragging {{ opacity: .45; }}
    .drag-handle {{ color: var(--faint); cursor: grab; padding-top: 2px; }}
    .selection-copy {{ min-width: 0; }}
    .selection-copy strong {{ display: block; font-size: 10px; color: var(--muted); }}
    .selection-copy p {{ margin: 4px 0 0; font: 10px/1.4 ui-monospace, SFMono-Regular, Consolas, monospace; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; overflow: hidden; overflow-wrap: anywhere; }}
    .selection-controls {{ display: grid; grid-template-columns: repeat(3, 27px); gap: 2px; }}
    .selection-controls button:disabled {{ opacity: .25; cursor: default; }}
    .tray-footer {{ padding: 9px 10px; border-top: 1px solid var(--line-soft); display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; }}
    .tray-footer .command-btn {{ padding: 0 5px; }}
    .mobile-only {{ display: none; }}
    @media (max-width: 1080px) {{
      .workspace {{ grid-template-columns: 224px minmax(0, 1fr); }}
      .evidence-tray {{ position: fixed; z-index: 20; right: 0; top: var(--header-h); bottom: 0; width: min(360px, 92vw); transform: translateX(100%); transition: transform .18s ease; box-shadow: -14px 0 32px var(--shadow); }}
      body.tray-open .evidence-tray {{ transform: translateX(0); }}
      .mobile-only {{ display: inline-flex; }}
    }}
    @media (max-width: 720px) {{
      body {{ overflow: auto; }}
      .app-header {{ position: sticky; top: 0; z-index: 12; }}
      .workspace {{ height: calc(100dvh - var(--header-h)); grid-template-columns: 1fr; }}
      .sidebar {{ display: none; }}
      .filters {{ grid-template-columns: 1fr; }}
      .mobile-thread-select {{ display: block; }}
      .filter-controls {{ justify-content: flex-start; flex-wrap: wrap; }}
      .role-filters {{ justify-content: flex-start; overflow-x: auto; flex-wrap: nowrap; }}
      .range-row {{ flex-wrap: wrap; }}
      .range-row input {{ flex: 1 1 112px; width: 112px; max-width: 120px; }}
      .result-summary {{ width: 100%; margin-left: 0; }}
      .timeline-inner {{ padding: 8px 10px 70px; }}
      .message {{ width: 100%; grid-template-columns: 22px minmax(0, 1fr); gap: 7px; overflow: hidden; }}
      .message-body, .message-content {{ width: 100%; min-width: 0; max-width: 100%; overflow: hidden; }}
      .attachments {{ grid-template-columns: minmax(0, 1fr); }}
      .attachment {{ width: 100%; max-width: 100%; }}
      .attachment-meta, .attachment-path {{ word-break: break-all; }}
      .command-label {{ display: none; }}
      .command-btn {{ width: 34px; padding: 0; }}
    }}
    @media print {{
      body {{ overflow: visible; background: white; color: black; }}
      .app-header, .sidebar, .filters, .range-row, .evidence-tray, .select-cell, .message-tools, .load-more {{ display: none !important; }}
      .workspace, .main {{ display: block; height: auto; }}
      .timeline {{ overflow: visible; }}
      .timeline-inner {{ width: 100%; padding: 0; }}
      .message, .message.user, .message.tool_call, .message.tool_output, .message.goal {{ background: white; color: black; border-color: #bbb; break-inside: avoid; }}
      .message-content.clamped {{ max-height: none; }}
      .message-content.clamped::after {{ display: none; }}
    }}
  </style>
</head>
<body>
  <svg width="0" height="0" aria-hidden="true" style="position:absolute"><defs>
    <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"></circle><path d="m21 21-4.3-4.3"></path></symbol>
    <symbol id="i-panel" viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2"></rect><path d="M15 3v18"></path></symbol>
    <symbol id="i-check" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"></path></symbol>
    <symbol id="i-copy" viewBox="0 0 24 24"><rect width="14" height="14" x="8" y="8" rx="2"></rect><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"></path></symbol>
    <symbol id="i-download" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><path d="m7 10 5 5 5-5"></path><path d="M12 15V3"></path></symbol>
    <symbol id="i-link" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1"></path><path d="M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7.1 7.1l1.1-1.1"></path></symbol>
    <symbol id="i-trash" viewBox="0 0 24 24"><path d="M3 6h18"></path><path d="M8 6V4h8v2"></path><path d="m19 6-1 14H6L5 6"></path></symbol>
    <symbol id="i-grip" viewBox="0 0 24 24"><circle cx="9" cy="12" r="1"></circle><circle cx="9" cy="5" r="1"></circle><circle cx="9" cy="19" r="1"></circle><circle cx="15" cy="12" r="1"></circle><circle cx="15" cy="5" r="1"></circle><circle cx="15" cy="19" r="1"></circle></symbol>
    <symbol id="i-printer" viewBox="0 0 24 24"><path d="M6 9V2h12v7"></path><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"></path><rect width="12" height="8" x="6" y="14"></rect></symbol>
    <symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2"></path><path d="M12 20v2"></path><path d="m4.93 4.93 1.42 1.42"></path><path d="m17.66 17.66 1.41 1.41"></path><path d="M2 12h2"></path><path d="M20 12h2"></path><path d="m6.34 17.66-1.41 1.41"></path><path d="m19.07 4.93-1.41 1.41"></path></symbol>
    <symbol id="i-moon" viewBox="0 0 24 24"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9"></path></symbol>
    <symbol id="i-up" viewBox="0 0 24 24"><path d="m18 15-6-6-6 6"></path></symbol>
    <symbol id="i-down" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"></path></symbol>
    <symbol id="i-eye" viewBox="0 0 24 24"><path d="M2.1 12a10 10 0 0 1 19.8 0 10 10 0 0 1-19.8 0"></path><circle cx="12" cy="12" r="3"></circle></symbol>
    <symbol id="i-code" viewBox="0 0 24 24"><path d="m16 18 6-6-6-6"></path><path d="m8 6-6 6 6 6"></path></symbol>
    <symbol id="i-file" viewBox="0 0 24 24"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"></path><polyline points="14 2 14 8 20 8"></polyline></symbol>
    <symbol id="i-external" viewBox="0 0 24 24"><path d="M15 3h6v6"></path><path d="M10 14 21 3"></path><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path></symbol>
  </defs></svg>
  <header class="app-header">
    <div class="brand"><h1 id="app-title"></h1><div class="brand-meta" id="brand-meta"></div></div>
    <div class="header-actions">
      <button class="icon-btn mobile-only" id="toggle-tray" title="证据集合" aria-label="证据集合"><svg class="icon"><use href="#i-panel"></use></svg></button>
      <button class="icon-btn" id="toggle-theme" title="切换到深色模式" aria-label="切换到深色模式" aria-pressed="false"><svg class="icon"><use href="#i-moon"></use></svg></button>
      <button class="command-btn" id="print-page" title="打印当前视图"><svg class="icon"><use href="#i-printer"></use></svg><span class="command-label">打印</span></button>
    </div>
  </header>
  <div class="workspace">
    <aside class="sidebar">
      <div class="sidebar-head"><h2 class="section-title">会话</h2><span class="count" id="thread-count"></span></div>
      <nav class="thread-list" id="thread-list"></nav>
    </aside>
    <main class="main">
      <div class="filters">
        <select class="mobile-thread-select" id="mobile-thread" aria-label="选择会话"></select>
        <label class="search-wrap"><svg class="icon"><use href="#i-search"></use></svg><input class="search" id="search" type="search" placeholder="搜索消息、工具或事件 ID" autocomplete="off"></label>
        <div class="filter-controls">
          <div class="view-modes" role="group" aria-label="消息显示模式">
            <button class="mode-btn active" type="button" data-view-mode="rendered" title="渲染 Markdown"><svg class="icon"><use href="#i-eye"></use></svg><span>渲染</span></button>
            <button class="mode-btn" type="button" data-view-mode="source" title="显示 Markdown 原文"><svg class="icon"><use href="#i-code"></use></svg><span>原文</span></button>
          </div>
          <div class="role-filters" id="role-filters"></div>
        </div>
      </div>
      <div class="range-row">
        <span class="range-label">时间</span><input id="since" type="datetime-local" title="开始时间"><span class="range-label">至</span><input id="until" type="datetime-local" title="结束时间">
        <button class="command-btn" id="add-visible" title="把当前可见消息加入证据集合"><svg class="icon"><use href="#i-check"></use></svg><span class="command-label">加入可见项</span></button>
        <span class="result-summary" id="result-summary"></span>
      </div>
      <section class="timeline" id="timeline"><div class="timeline-inner" id="timeline-inner"></div></section>
    </main>
    <aside class="evidence-tray" id="evidence-tray">
      <div class="tray-head"><h2 class="section-title">证据集合</h2><span class="count" id="selection-count"></span></div>
      <div class="tray-actions">
        <button class="command-btn" id="copy-md" title="复制 Markdown"><svg class="icon"><use href="#i-copy"></use></svg><span>Markdown</span></button>
        <button class="command-btn" id="clear-selection" title="清空集合"><svg class="icon"><use href="#i-trash"></use></svg><span>清空</span></button>
      </div>
      <div class="selection-list" id="selection-list"></div>
      <div class="tray-footer">
        <button class="command-btn" data-export="json" title="导出 JSON"><svg class="icon"><use href="#i-download"></use></svg><span>JSON</span></button>
        <button class="command-btn" data-export="md" title="导出 Markdown"><svg class="icon"><use href="#i-download"></use></svg><span>MD</span></button>
        <button class="command-btn" data-export="html" title="导出 HTML"><svg class="icon"><use href="#i-download"></use></svg><span>HTML</span></button>
      </div>
    </aside>
  </div>
  <script data-vendor="dompurify-3.4.12">{dompurify_js}</script>
  <script data-vendor="marked-18.0.7">{marked_js}</script>
  {mermaid_script}
  <script id="codex-history-data" type="application/json">{data}</script>
  <script>
  (() => {{
    'use strict';
    const dataset = JSON.parse(document.getElementById('codex-history-data').textContent);
    const byId = new Map(dataset.messages.map(message => [message.id, message]));
    const threadById = new Map(dataset.threads.map(thread => [thread.thread_id, thread]));
    const artifactByDigest = dataset.artifacts || {{}};
    const roleLabels = {{user:'USER',assistant:'ASSISTANT',tool_call:'TOOL CALL',tool_output:'TOOL OUTPUT',goal:'GOAL'}};
    const roleOrder = ['user','assistant','tool_call','tool_output','goal'];
    const storageKey = `codex-history-evidence:${{dataset.export_id}}`;
    const state = {{
      thread: 'all', search: '', roles: new Set(roleOrder), since: '', until: '',
      viewMode: 'rendered', selection: loadSelection(), visibleLimit: 140, filtered: [], expanded: new Set()
    }};
    const dom = Object.fromEntries(['app-title','brand-meta','thread-count','thread-list','mobile-thread','search','role-filters','since','until','result-summary','timeline','timeline-inner','selection-count','selection-list','toggle-tray','toggle-theme','print-page','add-visible','copy-md','clear-selection'].map(id => [id.replaceAll('-','_'), document.getElementById(id)]));

    function loadSelection() {{
      try {{ return JSON.parse(localStorage.getItem(storageKey) || '[]').filter(id => byId.has(id)); }}
      catch (_) {{ return []; }}
    }}
    function saveSelection() {{ try {{ localStorage.setItem(storageKey, JSON.stringify(state.selection)); }} catch (_) {{}} }}
    function loadTheme() {{ try {{ const value=localStorage.getItem('codex-history-viewer-theme'); return value==='dark'?'dark':'light'; }} catch (_) {{ return 'light'; }} }}
    function setTheme(theme, persist=false) {{
      const dark=theme==='dark'; document.documentElement.dataset.theme=dark?'dark':'light';
      const use=dom.toggle_theme.querySelector('use'); use.setAttribute('href',dark?'#i-sun':'#i-moon');
      const label=dark?'切换到浅色模式':'切换到深色模式'; dom.toggle_theme.title=label; dom.toggle_theme.setAttribute('aria-label',label); dom.toggle_theme.setAttribute('aria-pressed',String(dark));
      if (persist) {{ try {{ localStorage.setItem('codex-history-viewer-theme',dark?'dark':'light'); }} catch (_) {{}} }}
    }}
    function el(tag, className, text) {{
      const node = document.createElement(tag); if (className) node.className = className;
      if (text !== undefined) node.textContent = text; return node;
    }}
    function svg(name) {{
      const icon = document.createElementNS('http://www.w3.org/2000/svg','svg'); icon.setAttribute('class','icon');
      const use = document.createElementNS('http://www.w3.org/2000/svg','use'); use.setAttribute('href',`#i-${{name}}`); icon.append(use); return icon;
    }}
    function formatTime(value) {{
      if (!value) return 'time unknown'; const date = new Date(value); if (Number.isNaN(date.valueOf())) return value;
      return new Intl.DateTimeFormat('zh-CN',{{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}}).format(date);
    }}
    function short(value, size=12) {{ return value ? value.slice(0,size) : 'none'; }}
    function attachmentPayload(item) {{ return artifactByDigest[item.sha256] || item || {{}}; }}
    function dataUrlBlob(dataUrl) {{
      const comma=dataUrl.indexOf(','); if (comma<0) throw new Error('Invalid attachment data');
      const header=dataUrl.slice(5,comma); const mime=(header.split(';')[0] || 'application/octet-stream'); const body=dataUrl.slice(comma+1);
      if (!header.includes(';base64')) return new Blob([decodeURIComponent(body)],{{type:mime}});
      const binary=atob(body); const chunks=[];
      for (let offset=0; offset<binary.length; offset+=32768) {{
        const part=binary.slice(offset,offset+32768); const bytes=new Uint8Array(part.length);
        for (let index=0; index<part.length; index+=1) bytes[index]=part.charCodeAt(index);
        chunks.push(bytes);
      }}
      return new Blob(chunks,{{type:mime}});
    }}
    function useAttachment(item, asDownload) {{
      const dataUrl=attachmentPayload(item).data_url; if (!dataUrl) return;
      const url=URL.createObjectURL(dataUrlBlob(dataUrl));
      if (asDownload) {{
        const link=document.createElement('a'); link.href=url; link.download=item.display_name || `${{item.sha256}}${{item.extension || ''}}`; link.click();
      }} else {{
        window.open(url,'_blank','noopener,noreferrer');
      }}
      setTimeout(()=>URL.revokeObjectURL(url),60000);
    }}
    function attachmentStatus(item) {{
      const labels={{embedded:'已打包',available_not_embedded:'已定位，未打包',skipped_file_limit:'超过单文件限制',skipped_total_limit:'超过导出总量限制',missing_file:'CAS 文件缺失',missing_record:'附件记录缺失'}};
      return labels[item.status] || item.status || '状态未知';
    }}
    function attachmentNode(item) {{
      const payload=attachmentPayload(item); const dataUrl=payload.data_url || ''; const card=el('section','attachment');
      if (item.kind==='image' && dataUrl) {{
        const image=document.createElement('img'); image.className='attachment-image'; image.src=dataUrl; image.alt=item.display_name || `Image ${{short(item.sha256)}}`; card.append(image);
      }} else {{
        const file=el('div','attachment-file'); file.append(svg('file')); const copy=el('div','attachment-file-copy'); copy.append(el('strong','',item.extension?.replace('.','').toUpperCase() || item.kind?.toUpperCase() || 'FILE'),el('span','',item.size_human || 'size unknown')); file.append(copy); card.append(file);
      }}
      const info=el('div','attachment-info'); const name=el('p','attachment-name',item.display_name || item.uri); name.title=item.display_name || item.uri; info.append(name);
      const stateClass=item.status?.startsWith('missing')?' missing':item.status?.startsWith('skipped')?' skipped':'';
      const meta=el('div','attachment-meta'); meta.append(document.createTextNode(`${{item.kind || 'file'}} · ${{item.size_human || 'size unknown'}} · `),el('span',`attachment-status${{stateClass}}`,attachmentStatus(item)),document.createTextNode(` · sha256:${{short(item.sha256)}}`)); info.append(meta);
      info.append(el('div','attachment-path',item.mime_type || 'application/octet-stream'));
      if (item.source_paths?.length) {{ const paths=el('details','attachment-preview'); paths.append(el('summary','','原始路径'),el('pre','',item.source_paths.join('\\n'))); info.append(paths); }}
      if (dataUrl) {{
        const actions=el('div','attachment-actions');
        if (item.can_open) {{ const open=el('button','attachment-action'); open.type='button'; open.title='在浏览器中打开附件'; open.append(svg('external'),document.createTextNode('打开')); open.addEventListener('click',()=>useAttachment(item,false)); actions.append(open); }}
        const save=el('button','attachment-action'); save.type='button'; save.title='保存附件原文件'; save.append(svg('download'),document.createTextNode('下载')); save.addEventListener('click',()=>useAttachment(item,true)); actions.append(save); info.append(actions);
      }}
      if (payload.text_preview) {{ const details=el('details','attachment-preview'); details.append(el('summary','','文本预览'),el('pre','',payload.text_preview + (payload.text_preview_truncated?'\\n\\n[preview truncated]':''))); info.append(details); }}
      card.append(info); return card;
    }}
    function markdownNode(source) {{
      const node=el('div','message-content markdown-body');
      const rendered=marked.parse(source,{{gfm:true,breaks:false}});
      node.innerHTML=DOMPurify.sanitize(rendered,{{USE_PROFILES:{{html:true}},FORBID_TAGS:['style','form','input','button','textarea','select','option']}});
      return node;
    }}
    async function renderMermaid(root) {{
      if (!window.mermaid) return;
      const blocks=[...root.querySelectorAll('pre code.language-mermaid')]; if (!blocks.length) return;
      mermaid.initialize({{startOnLoad:false,securityLevel:'strict',theme:document.documentElement.dataset.theme==='dark'?'dark':'neutral',flowchart:{{htmlLabels:false}}}});
      for (const block of blocks) {{
        const pre=block.closest('pre'); if (!pre) continue; const source=block.textContent; const host=el('div','mermaid mermaid-host',source); pre.replaceWith(host);
        try {{ await mermaid.run({{nodes:[host],suppressErrors:true}}); }}
        catch (_) {{ const fallback=el('pre','mermaid-error'); const code=el('code','language-mermaid',source); fallback.append(code); host.replaceWith(fallback); }}
      }}
    }}
    function activeMessages() {{
      const query = state.search.trim().toLocaleLowerCase();
      const since = state.since ? new Date(state.since).valueOf() : null;
      const until = state.until ? new Date(state.until).valueOf() : null;
      return dataset.messages.filter(message => {{
        if (state.thread !== 'all' && message.thread_id !== state.thread) return false;
        if (!state.roles.has(message.role)) return false;
        const time = message.timestamp ? new Date(message.timestamp).valueOf() : null;
        if (since !== null && (time === null || time < since)) return false;
        if (until !== null && (time === null || time > until)) return false;
        if (query) {{
          const thread = threadById.get(message.thread_id);
          const attachmentText=(message.attachments || []).flatMap(item=>[item.display_name,item.mime_type,...(item.source_paths || [])]).join(' ');
          const haystack = `${{message.content}} ${{message.tool_name}} ${{message.event_id}} ${{thread?.title || ''}} ${{attachmentText}}`.toLocaleLowerCase();
          if (!haystack.includes(query)) return false;
        }}
        return true;
      }});
    }}
    function renderThreads() {{
      dom.thread_list.replaceChildren();
      const all = threadButton({{thread_id:'all',title:'全部会话',message_count:dataset.messages.length}}, true); dom.thread_list.append(all);
      dataset.threads.forEach(thread => dom.thread_list.append(threadButton(thread,false)));
      dom.mobile_thread.replaceChildren();
      [{{thread_id:'all',title:'全部会话'}},...dataset.threads].forEach(thread => {{ const option=el('option','',thread.title); option.value=thread.thread_id; option.selected=state.thread===thread.thread_id; dom.mobile_thread.append(option); }});
      dom.thread_count.textContent = String(dataset.threads.length);
    }}
    function threadButton(thread, isAll) {{
      const button = el('button',`thread-item${{state.thread===thread.thread_id?' active':''}}`); button.type='button';
      button.append(el('span','thread-title',thread.title));
      const count = isAll ? dataset.messages.length : thread.message_count;
      button.append(el('span','thread-meta',`${{count}} events${{isAll?'':` · ${{short(thread.thread_id)}}`}}`));
      button.addEventListener('click',() => {{ state.thread=thread.thread_id; state.visibleLimit=140; renderAll(); }});
      return button;
    }}
    function renderRoles() {{
      dom.role_filters.replaceChildren();
      roleOrder.forEach(role => {{
        const label=el('label','role-chip'); const input=document.createElement('input'); input.type='checkbox'; input.checked=state.roles.has(role);
        input.addEventListener('change',() => {{ input.checked?state.roles.add(role):state.roles.delete(role); state.visibleLimit=140; renderTimeline(); }});
        label.append(input,el('span','',roleLabels[role])); dom.role_filters.append(label);
      }});
    }}
    function renderViewModes() {{ document.querySelectorAll('[data-view-mode]').forEach(button => button.classList.toggle('active',button.dataset.viewMode===state.viewMode)); }}
    function renderTimeline() {{
      const messages=activeMessages(); state.filtered=messages; dom.timeline_inner.replaceChildren();
      const current=state.thread==='all'?null:threadById.get(state.thread);
      const banner=el('div','thread-banner'); banner.append(el('h2','',current?.title || '全部会话'));
      banner.append(el('p','',current?`${{current.thread_id}} · ${{current.first_activity_at || 'unknown'}} .. ${{current.last_activity_at || 'unknown'}}`:`${{dataset.threads.length}} threads · ${{dataset.export_id}}`));
      dom.timeline_inner.append(banner);
      dom.result_summary.textContent=`${{Math.min(messages.length,state.visibleLimit)}} / ${{messages.length}}`;
      if (!messages.length) {{ dom.timeline_inner.append(el('div','empty','没有匹配的消息')); return; }}
      messages.slice(0,state.visibleLimit).forEach(message => dom.timeline_inner.append(messageNode(message)));
      if (state.visibleLimit < messages.length) {{
        const more=el('button','load-more',`继续加载 ${{Math.min(140,messages.length-state.visibleLimit)}} 条`); more.type='button';
        more.addEventListener('click',() => {{ state.visibleLimit+=140; renderTimeline(); }}); dom.timeline_inner.append(more);
      }}
      if (state.viewMode==='rendered') void renderMermaid(dom.timeline_inner);
      const anchor=decodeURIComponent(location.hash.slice(1)); if (anchor && byId.has(anchor)) requestAnimationFrame(() => document.getElementById(anchor)?.scrollIntoView({{block:'center'}}));
    }}
    function messageNode(message) {{
      const article=el('article',`message ${{message.role}}`); article.id=message.id;
      const selectCell=el('div','select-cell'); const checkbox=document.createElement('input'); checkbox.type='checkbox'; checkbox.checked=state.selection.includes(message.id); checkbox.title='加入证据集合';
      checkbox.addEventListener('change',() => toggleSelection(message.id,checkbox.checked)); selectCell.append(checkbox);
      const body=el('div','message-body'); const head=el('div','message-head'); head.append(el('span','role-name',roleLabels[message.role] || message.role));
      if (message.tool_name) head.append(el('span','message-time',message.tool_name));
      head.append(el('span','message-time',formatTime(message.timestamp)));
      const tools=el('div','message-tools'); const link=el('button','mini-btn'); link.type='button'; link.title='复制证据链接'; link.append(svg('link'));
      link.addEventListener('click',async() => {{ location.hash=message.id; await navigator.clipboard?.writeText(location.href); }}); tools.append(link); head.append(tools);
      const renderMarkdown=state.viewMode==='rendered' && !message.internal && ['user','assistant'].includes(message.role);
      const content=renderMarkdown?markdownNode(message.content):el('pre','message-content source-content',message.content); const long=message.content.length>2400 || message.content.split('\\n').length>28;
      if (long && !state.expanded.has(message.id)) content.classList.add('clamped');
      body.append(head,content);
      if (long) {{ const expand=el('button','expand-btn',state.expanded.has(message.id)?'收起':'展开全文'); expand.type='button'; expand.addEventListener('click',() => {{ state.expanded.has(message.id)?state.expanded.delete(message.id):state.expanded.add(message.id); renderTimeline(); }}); body.append(expand); }}
      if (message.attachments?.length) {{
        const attachments=el('div','attachments'); message.attachments.forEach(item => attachments.append(attachmentNode(item))); body.append(attachments);
      }}
      body.append(el('div','provenance',`thread=${{short(message.thread_id)}} · turn=${{message.turn_number ?? 'n/a'}} · line=${{message.line_no}} · event=${{message.event_id}} · sha256=${{message.content_sha256}}`));
      if (message.raw_event) {{ const details=el('details','raw'); const summary=el('summary','','Raw canonical event'); const pre=el('pre','',JSON.stringify(message.raw_event,null,2)); details.append(summary,pre); body.append(details); }}
      article.append(selectCell,body); return article;
    }}
    function toggleSelection(id, enabled) {{
      const exists=state.selection.includes(id); if (enabled&&!exists) state.selection.push(id); if (!enabled&&exists) state.selection=state.selection.filter(item=>item!==id);
      saveSelection(); renderSelection();
    }}
    function moveSelection(from,to) {{ if (to<0 || to>=state.selection.length || from===to) return; const [moved]=state.selection.splice(from,1); state.selection.splice(to,0,moved); saveSelection(); renderSelection(); }}
    function renderSelection() {{
      dom.selection_list.replaceChildren(); dom.selection_count.textContent=String(state.selection.length);
      state.selection.forEach((id,index) => {{
        const message=byId.get(id); if (!message) return; const item=el('div','selection-item'); item.draggable=true; item.dataset.index=String(index);
        const grip=el('span','drag-handle'); grip.append(svg('grip')); const copy=el('div','selection-copy'); copy.append(el('strong','',`${{index+1}} · ${{roleLabels[message.role]}} · ${{formatTime(message.timestamp)}}`),el('p','',message.content));
        const controls=el('div','selection-controls'); const up=el('button','mini-btn'); up.type='button'; up.title='上移'; up.disabled=index===0; up.append(svg('up')); up.addEventListener('click',()=>moveSelection(index,index-1)); const down=el('button','mini-btn'); down.type='button'; down.title='下移'; down.disabled=index===state.selection.length-1; down.append(svg('down')); down.addEventListener('click',()=>moveSelection(index,index+1)); const remove=el('button','mini-btn'); remove.type='button'; remove.title='移除'; remove.append(svg('trash')); remove.addEventListener('click',()=>toggleSelection(id,false)); controls.append(up,down,remove); item.append(grip,copy,controls);
        item.addEventListener('dragstart',event => {{ item.classList.add('dragging'); event.dataTransfer.setData('text/plain',String(index)); }}); item.addEventListener('dragend',()=>item.classList.remove('dragging'));
        item.addEventListener('dragover',event=>event.preventDefault()); item.addEventListener('drop',event => {{ event.preventDefault(); const from=Number(event.dataTransfer.getData('text/plain')); const to=Number(item.dataset.index); if (Number.isNaN(from)) return; moveSelection(from,to); }});
        dom.selection_list.append(item);
      }});
    }}
    function selectedMessages() {{ return state.selection.map(id=>byId.get(id)).filter(Boolean); }}
    function markdown(messages) {{
      const lines=[`# ${{dataset.title}}`,'',`Export: ${{dataset.export_id}}`,''];
      messages.forEach(message => {{ const thread=threadById.get(message.thread_id); lines.push(`## ${{roleLabels[message.role]}} · ${{formatTime(message.timestamp)}}`,'',`Thread: ${{thread?.title || message.thread_id}}`,'',message.content); (message.attachments || []).forEach(item => lines.push('',`Attachment: ${{item.uri}} (${{item.available?'embedded':'not embedded'}})`)); lines.push('',`Evidence: thread=${{message.thread_id}} turn=${{message.turn_number ?? 'n/a'}} line=${{message.line_no}} event=${{message.event_id}} sha256=${{message.content_sha256}}`,''); }}); return lines.join('\\n');
    }}
    function markdownV2(messages) {{
      const lines=[`# ${{dataset.title}}`,'',`Export: ${{dataset.export_id}}`,''];
      messages.forEach(message => {{
        const thread=threadById.get(message.thread_id); lines.push(`## ${{roleLabels[message.role]}} · ${{formatTime(message.timestamp)}}`,'',`Thread: ${{thread?.title || message.thread_id}}`,'',message.content);
        (message.attachments || []).forEach(item => {{
          lines.push('',`Attachment: ${{item.display_name || item.uri}}`,'',`- Type: ${{item.kind}} (${{item.mime_type}})`,`- Size: ${{item.size_human}}`,`- Status: ${{attachmentStatus(item)}}`,`- SHA-256: ${{item.sha256}}`);
          if(item.source_paths?.length) lines.push(`- Source: ${{item.source_paths.join(' | ')}}`);
        }});
        lines.push('',`Evidence: thread=${{message.thread_id}} turn=${{message.turn_number ?? 'n/a'}} line=${{message.line_no}} event=${{message.event_id}} sha256=${{message.content_sha256}}`,'');
      }}); return lines.join('\\n');
    }}
    function download(name,mime,content) {{ const blob=new Blob([content],{{type:mime}}); const url=URL.createObjectURL(blob); const link=document.createElement('a'); link.href=url; link.download=name; link.click(); setTimeout(()=>URL.revokeObjectURL(url),1000); }}
    function escapeHtml(value) {{ return String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'); }}
    function selectionAttachmentHtml(item) {{
      const payload=attachmentPayload(item); const dataUrl=payload.data_url || ''; const name=escapeHtml(item.display_name || item.uri); const source=(item.source_paths || []).map(escapeHtml).join('<br>');
      const image=item.kind==='image'&&dataUrl?`<img style="display:block;max-width:100%;max-height:480px;object-fit:contain" src="${{escapeHtml(dataUrl)}}" alt="${{name}}">`:`<div class="file" style="padding:18px;background:#f1f3f2;font:12px ui-monospace,monospace">${{escapeHtml(item.extension?.replace('.','').toUpperCase() || item.kind || 'FILE')}} · ${{escapeHtml(item.size_human || '')}}</div>`;
      const open=dataUrl&&item.can_open?`<a style="padding:4px 8px;border:1px solid #d4d9d6;border-radius:5px;color:#176485;text-decoration:none" href="${{escapeHtml(dataUrl)}}" target="_blank" rel="noopener noreferrer">打开</a>`:'';
      const save=dataUrl?`<a style="padding:4px 8px;border:1px solid #d4d9d6;border-radius:5px;color:#176485;text-decoration:none" href="${{escapeHtml(dataUrl)}}" download="${{name}}">下载</a>`:'';
      const preview=payload.text_preview?`<details><summary>文本预览</summary><pre>${{escapeHtml(payload.text_preview)}}${{payload.text_preview_truncated?'\\n\\n[preview truncated]':''}}</pre></details>`:'';
      return `<section class="attachment" style="margin:12px 0;padding:10px;border:1px solid #d4d9d6;border-radius:6px"><div class="attachment-copy"><strong style="display:block;overflow-wrap:anywhere">${{name}}</strong><span style="display:block;margin-top:5px;color:#59635e;font:10px/1.45 ui-monospace,monospace">${{escapeHtml(item.kind)}} · ${{escapeHtml(item.mime_type)}} · ${{escapeHtml(attachmentStatus(item))}} · sha256:${{escapeHtml(short(item.sha256))}}</span>${{source?`<span style="display:block;margin-top:5px;color:#59635e;font:10px/1.45 ui-monospace,monospace">${{source}}</span>`:''}}<nav style="display:flex;gap:8px;margin:8px 0">${{open}}${{save}}</nav></div>${{image}}${{preview}}</section>`;
    }}
    function selectionHtml(messages) {{
      const rows=messages.map(message=>{{ const thread=threadById.get(message.thread_id); const canRender=!message.internal&&['user','assistant'].includes(message.role); const rendered=canRender?DOMPurify.sanitize(marked.parse(message.content,{{gfm:true}}),{{USE_PROFILES:{{html:true}},FORBID_TAGS:['style','form','input','button','textarea','select','option']}}):`<pre>${{escapeHtml(message.content)}}</pre>`; const source=canRender?`<details><summary>Markdown 原文</summary><pre>${{escapeHtml(message.content)}}</pre></details>`:''; const attachments=(message.attachments || []).map(selectionAttachmentHtml).join(''); const raw=message.raw_event?`<details><summary>Raw canonical event</summary><pre>${{escapeHtml(JSON.stringify(message.raw_event,null,2))}}</pre></details>`:''; return `<article><h2>${{escapeHtml(roleLabels[message.role])}} <small>${{escapeHtml(formatTime(message.timestamp))}}</small></h2><p class="thread">${{escapeHtml(thread?.title || message.thread_id)}}</p><div class="markdown">${{rendered}}</div>${{source}}${{attachments}}${{raw}}<footer>thread=${{escapeHtml(message.thread_id)}} · turn=${{message.turn_number ?? 'n/a'}} · line=${{message.line_no}} · event=${{escapeHtml(message.event_id)}} · sha256=${{escapeHtml(message.content_sha256)}}</footer></article>`; }}).join('');
      const vendor=document.querySelector('script[data-vendor^="mermaid-"]'); const openScript='<scr'+'ipt>'; const closeScript='</scr'+'ipt>'; const mermaidRuntime=vendor?openScript+vendor.textContent+closeScript+openScript+`mermaid.initialize({{startOnLoad:false,securityLevel:'strict',theme:'neutral',flowchart:{{htmlLabels:false}}}});const nodes=[];document.querySelectorAll('pre code.language-mermaid').forEach(block=>{{const pre=block.closest('pre');const host=document.createElement('div');host.className='mermaid';host.textContent=block.textContent;pre.replaceWith(host);nodes.push(host)}});if(nodes.length)mermaid.run({{nodes,suppressErrors:true}});`+closeScript:'';
      return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'"><title>${{escapeHtml(dataset.title)}}</title><style>body{{max-width:920px;margin:auto;padding:24px;background:#fff;color:#202522;font:14px/1.65 system-ui}}article{{padding:18px 0;border-bottom:1px solid #d4d9d6}}h1{{font-size:20px}}h2{{font-size:13px}}h3{{font-size:15px}}small,footer,.thread,figcaption,summary{{color:#59635e;font:11px ui-monospace,monospace}}pre{{max-width:100%;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;padding:10px;border:1px solid #e4e7e5;border-radius:6px;background:#f1f3f2;font:12px/1.6 ui-monospace,monospace}}code{{font-family:ui-monospace,monospace}}table{{display:block;max-width:100%;overflow:auto;border-collapse:collapse}}th,td{{min-width:90px;padding:7px 9px;border:1px solid #d4d9d6;text-align:left}}th{{background:#f1f3f2}}blockquote{{padding-left:12px;border-left:3px solid #d4d9d6;color:#59635e}}img{{display:block;max-width:100%;max-height:720px;object-fit:contain}}figure{{margin:12px 0}}details{{margin:10px 0}}.mermaid{{max-width:100%;overflow:auto;text-align:center}}</style><body><h1>${{escapeHtml(dataset.title)}}</h1>${{rows}}${{mermaidRuntime}}</body></html>`;
    }}
    async function copyText(value) {{
      if (!value) return;
      if (navigator.clipboard?.writeText) {{ try {{ await navigator.clipboard.writeText(value); return; }} catch (_) {{}} }}
      const area=document.createElement('textarea'); area.value=value; area.style.position='fixed'; area.style.opacity='0'; document.body.append(area); area.select(); document.execCommand('copy'); area.remove();
    }}
    function exportSelection(format) {{
      const messages=selectedMessages(); if (!messages.length) return; const base=`${{dataset.export_id}}-selection`;
      if (format==='json') {{ const digests=new Set(messages.flatMap(message=>(message.attachments || []).map(item=>item.sha256))); const artifacts=Object.fromEntries([...digests].filter(digest=>artifactByDigest[digest]).map(digest=>[digest,artifactByDigest[digest]])); download(`${{base}}.json`,'application/json',JSON.stringify({{schema_version:'codex-history-evidence-selection-v2',source_export_id:dataset.export_id,messages,artifacts}},null,2)); }}
      if (format==='md') download(`${{base}}.md`,'text/markdown;charset=utf-8',markdownV2(messages));
      if (format==='html') download(`${{base}}.html`,'text/html;charset=utf-8',selectionHtml(messages));
    }}
    function renderAll() {{ renderThreads(); renderRoles(); renderViewModes(); renderTimeline(); renderSelection(); }}

    setTheme(loadTheme());
    dom.app_title.textContent=dataset.title; dom.brand_meta.textContent=`${{dataset.statistics.threads}} threads · ${{dataset.statistics.messages}} events · ${{dataset.export_id}}`;
    dom.mobile_thread.addEventListener('change',() => {{ state.thread=dom.mobile_thread.value; state.visibleLimit=140; renderAll(); }});
    dom.search.addEventListener('input',() => {{ state.search=dom.search.value; state.visibleLimit=140; renderTimeline(); }});
    document.querySelectorAll('[data-view-mode]').forEach(button=>button.addEventListener('click',()=>{{ state.viewMode=button.dataset.viewMode; renderViewModes(); renderTimeline(); }}));
    dom.since.addEventListener('change',() => {{ state.since=dom.since.value; state.visibleLimit=140; renderTimeline(); }});
    dom.until.addEventListener('change',() => {{ state.until=dom.until.value; state.visibleLimit=140; renderTimeline(); }});
    dom.toggle_tray.addEventListener('click',()=>document.body.classList.toggle('tray-open'));
    dom.toggle_theme.addEventListener('click',()=>{{ setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark',true); renderTimeline(); }});
    dom.print_page.addEventListener('click',()=>window.print());
    dom.add_visible.addEventListener('click',() => {{ state.filtered.forEach(message => {{ if(!state.selection.includes(message.id)) state.selection.push(message.id); }}); saveSelection(); renderSelection(); }});
    dom.clear_selection.addEventListener('click',() => {{ state.selection=[]; saveSelection(); renderSelection(); renderTimeline(); }});
    dom.copy_md.addEventListener('click',()=>copyText(markdownV2(selectedMessages())));
    document.querySelectorAll('[data-export]').forEach(button=>button.addEventListener('click',()=>exportSelection(button.dataset.export)));
    window.addEventListener('hashchange',renderTimeline);
    renderAll();
  }})();
  </script>
</body>
</html>
"""
