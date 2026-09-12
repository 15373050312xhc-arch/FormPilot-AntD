"""
dump_form_schema.py — 客户端排单模式 第 1 步：抓源码

流程：
1. 启动浏览器（复用 v2 的 profile 保留登录态）
2. 用户登录并进入表单页
3. 点击右下角"抓取表单源码"按钮
4. 工具会：
   a. 点开隐藏模块（实践经历/个人荣誉/社团活动）
   b. 逐个点开 ant-select 下拉框提取选项
   c. 扫描所有字段（input/textarea/select/ant-select/ant-picker/ant-radio-group/ant-cascader）
   d. 提取 radio group 内所有选项的可见文本
5. 输出：
   - form_schema.json  结构化字段清单（含 anchor / kind / options）
   - form_prompt.txt  已拼好的提示词，用户复制粘贴到豆包/DeepSeek 客户端

用户拿到 LLM 返回的 JSON，保存为 form_plan.json，
然后运行 apply_form_plan.py 进入第 2 步执行填写。

锚点设计：优先用 DOM id（如 resume_basicInfo_name）；
没 id 的用 label + section 组合；都没有的用 placeholder。
uid 是会话内一次性，schema 里不暴露 uid，只暴露 anchor。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
DEFAULT_PROFILE_DIR = ROOT_DIR / "work" / "browser-resume-autofill-profile"
DEFAULT_OUT_DIR = SCRIPT_DIR

# 复用 v2 的 JS 和工具函数
sys.path.insert(0, str(SCRIPT_DIR))
from form_filler_v2 import (  # noqa: E402
    SCAN_FIELDS_V2_JS,
    INSTALL_WIDGET_JS,
    SET_WIDGET_STATUS_JS,
    click_add_buttons_for_hidden_sections,
    launch_browser_context,
    normalize_label,
    normalize_primary_label,
)


# ─────────────────────────────────────────────────────────
# JS: 抓取完整页面 HTML（精简：去掉 script/style/noscript/svg）
# ─────────────────────────────────────────────────────────

DUMP_PAGE_HTML_JS = r"""
() => {
  // 克隆整个文档，去掉噪音节点，保留 form 结构
  const clone = document.documentElement.cloneNode(true);

  // 删除不需要的标签
  const dropTags = ['script', 'style', 'noscript', 'svg', 'link', 'meta'];
  for (const tag of dropTags) {
    clone.querySelectorAll(tag).forEach(el => el.remove());
  }

  // 删除 data-resume-autofill-id 属性（dump 阶段的临时标记，干扰阅读）
  clone.querySelectorAll('[data-resume-autofill-id]').forEach(el => {
    el.removeAttribute('data-resume-autofill-id');
  });

  // 删除 resume-autofill-widget（工具自己注入的按钮）
  const widget = clone.querySelector('#resume-autofill-widget');
  if (widget) widget.remove();

  // 返回精简后的 outerHTML
  return '<!doctype html>' + clone.outerHTML;
}
"""


# ─────────────────────────────────────────────────────────
# JS: 超精简表单结构（只保留 form 内字段相关标签+必要属性+文本）
# 用于直接嵌入 prompt 给 LLM 看，体积可砍 90%+
# ─────────────────────────────────────────────────────────

DUMP_FORM_OUTLINE_JS = r"""
() => {
  const keepTags = new Set([
    'form', 'label', 'input', 'textarea', 'select', 'option',
    'div', 'span', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'fieldset', 'legend', 'ul', 'li',
  ]);
  // 只保留这些属性
  const keepAttrs = new Set([
    'id', 'name', 'type', 'value', 'placeholder', 'for',
    'maxlength', 'readonly', 'disabled', 'checked', 'selected',
    'role', 'aria-label',
  ]);
  // class 里有价值的模式（ant-form-item-required / ant-radio-group / 等）
  const keepClassPattern = /ant-form-item-required|ant-radio-group|ant-select|ant-picker|ant-cascader|ant-checkbox|el-radio-group|el-select|el-date|el-cascader|item-name|item-box|section|form-item/;

  // 找 form 元素；找不到就用 body
  let root = document.querySelector('form');
  if (!root) root = document.body;
  if (!root) return '';

  const text = (n) => (n && (n.textContent || n.innerText || '') || '').replace(/\s+/g, ' ').trim();

  // 递归构建精简 HTML 字符串
  const out = [];
  const walk = (el, depth) => {
    if (depth > 20) return;
    const tag = el.tagName.toLowerCase();
    if (!keepTags.has(tag)) return;

    // 跳过不可见
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return;

    // 构造属性
    const attrs = [];
    for (const attr of el.attributes || []) {
      const name = attr.name.toLowerCase();
      if (keepAttrs.has(name)) {
        const v = (attr.value || '').slice(0, 120);
        if (v) attrs.push(`${name}="${v.replace(/"/g, '&quot;')}"`);
      }
      if (name === 'class') {
        const cls = attr.value || '';
        const matches = (cls.split(/\s+/)).filter(c => keepClassPattern.test(c));
        if (matches.length) attrs.push(`class="${matches.join(' ')}"`);
      }
    }

    // 自闭合/无子元素
    const childNodes = Array.from(el.childNodes).filter(n => n.nodeType === 1);
    const hasText = text(el).length > 0 && childNodes.length === 0;

    // 特殊处理：option 直接输出文本
    if (tag === 'option') {
      const t = text(el);
      const v = el.getAttribute('value') || '';
      out.push(`${'  '.repeat(depth)}<option value="${v}">${t}</option>`);
      return;
    }

    // 短标签：直接单行输出
    if (['input', 'br', 'hr', 'img'].includes(tag)) {
      out.push(`${'  '.repeat(depth)}<${tag}${attrs.length ? ' ' + attrs.join(' ') : ''}>`);
      return;
    }

    // 短文本：单行
    if (hasText && tag !== 'div' && tag !== 'span' && tag !== 'p') {
      const t = text(el).slice(0, 80);
      out.push(`${'  '.repeat(depth)}<${tag}${attrs.length ? ' ' + attrs.join(' ') : ''}>${t}</${tag}>`);
      return;
    }

    // div/section 等容器：如果只有纯文本（无子元素），单行
    if (childNodes.length === 0) {
      const t = text(el).slice(0, 80);
      if (t) {
        out.push(`${'  '.repeat(depth)}<${tag}${attrs.length ? ' ' + attrs.join(' ') : ''}>${t}</${tag}>`);
      } else {
        out.push(`${'  '.repeat(depth)}<${tag}${attrs.length ? ' ' + attrs.join(' ') : ''}></${tag}>`);
      }
      return;
    }

    // 有子元素：递归
    out.push(`${'  '.repeat(depth)}<${tag}${attrs.length ? ' ' + attrs.join(' ') : ''}>`);
    for (const child of childNodes) walk(child, depth + 1);
    out.push(`${'  '.repeat(depth)}</${tag}>`);
  };

  walk(root, 0);
  return out.join('\n');
}
"""


# ─────────────────────────────────────────────────────────
# JS: 补扫漏掉的 ant-select / ant-picker / ant-cascader 容器
# （SCAN_FIELDS_V2_JS 去重逻辑有时会把 .ant-select/.ant-picker 容器过滤掉）
# ─────────────────────────────────────────────────────────

_SCAN_MISSING_CONTAINERS_JS = r"""
() => {
  const text = (node) =>
    (node && (node.innerText || node.textContent || '') || '')
      .replace(/\s+/g, ' ').trim();

  const isVisible = (el) => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none'
      && r.width > 0 && r.height > 0;
  };

  const labelForEl = (el) => {
    if (el.id) {
      const lb = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lb) return text(lb).replace(/请输入|请选择/g, '').trim();
    }
    const formItem = el.closest('.ant-form-item, .el-form-item, .form-item');
    if (formItem) {
      const ll = formItem.querySelector('.ant-form-item-label, .el-form-item__label');
      if (ll) return text(ll).replace(/请输入|请选择/g, '').trim();
    }
    return '';
  };

  const containers = document.querySelectorAll(
    '.ant-select, .ant-picker, .ant-cascader, '
    + '.el-select, .el-date-editor, .el-cascader'
  );
  const results = [];

  for (const el of containers) {
    if (!isVisible(el)) continue;
    const id = el.id || '';
    if (!id) continue;  // 没有稳定 id 的跳过

    // 内部有 ant-select-selection-search-input 说明是 ant-select
    const hasInnerInput = el.querySelector(
      'input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"])'
    );

    let fieldType = 'custom-dropdown';
    if (el.classList.contains('ant-picker') || el.classList.contains('el-date-editor')) {
      fieldType = 'date-picker';
    } else if (el.classList.contains('ant-cascader') || el.classList.contains('el-cascader')) {
      fieldType = 'cascader';
    }

    const label = labelForEl(el);
    const selectedText = text(
      el.querySelector(
        '.ant-select-selection-item, .ant-select-selection-selected-value, '
        + '.ant-picker-input > input, .ant-picker-view input, '
        + '.ant-cascader-picker-label, .ant-select-selection__choice__content'
      ) || ''
    );
    const placeholder = text(
      el.querySelector(
        '.ant-select-selection-placeholder, .ant-picker-placeholder, '
        + '.ant-cascader-picker-placeholder'
      ) || ''
    );

    results.push({
      tag: el.tagName.toLowerCase(),
      type: fieldType,
      input_type: 'text',
      name: el.getAttribute('name') || '',
      id: id,
      placeholder: placeholder,
      selected_text: selectedText,
      primary_label: label,
      value: selectedText,
    });
  }

  return results;
}
"""


# ─────────────────────────────────────────────────────────
# JS: 抓取 radio group 内的所有可见选项
# ─────────────────────────────────────────────────────────

EXTRACT_RADIO_OPTIONS_JS = r"""
({ uid }) => {
  const container = document.querySelector(`[data-resume-autofill-id="${uid}"]`);
  if (!container) return [];
  const text = (n) => (n && (n.innerText || n.textContent || '') || '').replace(/\s+/g, ' ').trim();
  const opts = [];
  const seen = new Set();
  // ant-radio-wrapper / 原生 label
  const labels = container.querySelectorAll('label, .ant-radio-wrapper, .ant-radio-button-wrapper');
  for (const lb of labels) {
    const t = text(lb).replace(/^[*＊\s]+/, '').trim();
    if (!t || seen.has(t)) continue;
    seen.add(t);
    const input = lb.querySelector('input[type="radio"]');
    const v = input ? (input.getAttribute('value') || '') : '';
    opts.push({ text: t, value: v });
  }
  // ant-radio-button 用 button 模式时，label 是兄弟
  if (opts.length === 0) {
    const btns = container.querySelectorAll('.ant-radio-button, .ant-radio-button-input');
    for (const b of btns) {
      const lbl = b.closest('label');
      const t = lbl ? text(lbl) : text(b);
      if (!t || seen.has(t)) continue;
      seen.add(t);
      const v = b.getAttribute('value') || '';
      opts.push({ text: t, value: v });
    }
  }
  return opts;
}
"""


# ─────────────────────────────────────────────────────────
# Widget：抓取按钮（覆盖 v2 默认按钮文案）
# ─────────────────────────────────────────────────────────

INSTALL_DUMP_WIDGET_JS = r"""
() => {
  if (window.top !== window.self) return false;
  document.getElementById('resume-autofill-widget')?.remove();

  const box = document.createElement('div');
  box.id = 'resume-autofill-widget';
  box.style.cssText = [
    'position:fixed', 'left:12px', 'top:12px',
    'z-index:2147483647',
    'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif',
    'pointer-events:auto'
  ].join(';');

  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = '抓取表单源码';
  button.style.cssText = [
    'height:48px', 'padding:0 18px', 'border:0', 'border-radius:10px',
    'background:#7c3aed', 'color:#fff', 'font-size:16px', 'font-weight:600',
    'box-shadow:0 10px 30px rgba(0,0,0,.28)', 'cursor:pointer',
    'outline:3px solid rgba(255,255,255,.92)'
  ].join(';');

  const status = document.createElement('div');
  status.id = 'resume-autofill-status';
  status.textContent = '进入表单页后点击此按钮';
  status.style.cssText = [
    'margin-top:8px', 'max-width:260px', 'padding:7px 10px',
    'border-radius:8px', 'background:rgba(17,24,39,.88)',
    'color:#fff', 'font-size:12px', 'line-height:1.35',
    'text-align:center', 'box-shadow:0 8px 24px rgba(0,0,0,.18)'
  ].join(';');

  button.addEventListener('click', () => {
    if (window.__resumeDumpRequested) return;
    window.__resumeDumpRequested = true;
    button.disabled = true;
    button.textContent = '抓取中...';
    button.style.background = '#64748b';
    button.style.cursor = 'wait';
    status.textContent = '正在扫描页面字段';
  });

  document.addEventListener('keydown', (event) => {
    if (event.ctrlKey && event.shiftKey && String(event.key || '').toLowerCase() === 'd') {
      event.preventDefault();
      if (window.__resumeDumpRequested) return;
      window.__resumeDumpRequested = true;
      button.disabled = true;
      button.textContent = '抓取中...';
      button.style.background = '#64748b';
      button.style.cursor = 'wait';
      status.textContent = '正在扫描页面字段';
    }
  }, true);

  box.appendChild(button);
  box.appendChild(status);
  document.documentElement.appendChild(box);
  return true;
}
"""


# ─────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────

def install_dump_widget(page):
    try:
        page.evaluate(INSTALL_DUMP_WIDGET_JS)
    except Exception:
        pass


def wait_for_dump_click(page):
    while True:
        install_dump_widget(page)
        try:
            requested = page.evaluate("() => Boolean(window.__resumeDumpRequested)")
        except Exception:
            requested = False
        if requested:
            return
        time.sleep(0.5)


def set_widget_status(page, status, *, enabled=True, button_text="", tone="working"):
    try:
        page.evaluate(
            SET_WIDGET_STATUS_JS,
            {"status": status, "enabled": enabled, "buttonText": button_text, "tone": tone},
        )
    except Exception:
        pass


def _build_anchor(field: dict) -> dict:
    """从扫描结果构造稳定锚点。优先 id，其次 name，最后 label+section+placeholder。"""
    anchor = {
        "id": str(field.get("id") or ""),
        "name": str(field.get("name") or ""),
        "label": normalize_primary_label(field) or normalize_label(field),
        "section": str(field.get("section_header") or ""),
        "placeholder": str(field.get("placeholder") or ""),
    }
    # 选一个最稳定的"主锚点"标识，方便用户/LLM 一眼看清
    anchor["key"] = anchor["id"] or anchor["name"] or anchor["label"]
    return anchor


def _classify_kind(field: dict) -> str:
    """映射到执行器认识的 kind：text/textarea/select/dropdown/cascader/radio/checkbox/date"""
    ft = field.get("type", "")
    if ft == "cascader":
        return "cascader"
    if ft == "select":
        return "select"
    if ft == "custom-dropdown":
        return "dropdown"
    if ft == "date-picker":
        return "date"
    if ft == "radio":
        return "radio"
    if ft == "checkbox":
        return "checkbox"
    if field.get("tag") == "textarea":
        return "textarea"
    it = field.get("input_type", "")
    if it == "date":
        return "date"
    return "text"


def _extract_radio_options(page, field: dict) -> list[dict]:
    uid = field.get("uid", "")
    if not uid:
        return []
    try:
        opts = page.evaluate(EXTRACT_RADIO_OPTIONS_JS, {"uid": uid})
        if isinstance(opts, list):
            return opts
    except Exception as exc:
        print(f"  radio options extract failed: {exc}")
    return []


# ─────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────

def dump_schema(page, out_dir: Path) -> dict:
    """抓取完整 HTML + 精简 outline → 扫描字段锚点 → 输出 html + outline + schema + prompt"""
    print("\n[dump] 第 1 步：抓取页面 HTML（完整 + 精简）...")
    set_widget_status(page, "正在抓取页面源码...", button_text="抓取中", tone="working")

    page_html = ""
    try:
        page_html = page.evaluate(DUMP_PAGE_HTML_JS)
        print(f"[dump] 完整 HTML：{len(page_html)} 字符")
    except Exception as exc:
        print(f"[dump] 完整 HTML 抓取失败: {exc}")

    page_outline = ""
    try:
        page_outline = page.evaluate(DUMP_FORM_OUTLINE_JS)
        print(f"[dump] 精简 outline：{len(page_outline)} 字符（压缩比 {len(page_html) // max(1, len(page_outline))}x）")
    except Exception as exc:
        print(f"[dump] 精简 outline 抓取失败: {exc}")

    out_dir.mkdir(parents=True, exist_ok=True)
    # 完整 HTML：保留作为附件给支持文件上传的 LLM（豆包可能读不动，但 DeepSeek/Kimi 可以）
    html_path = out_dir / "form_page.html"
    html_path.write_text(page_html or "<!-- HTML dump failed -->", encoding="utf-8")
    print(f"[dump] 完整 HTML 已写入：{html_path}")

    # 精简 outline：用 .txt 扩展名，方便豆包直接读
    outline_path = out_dir / "form_outline.txt"
    outline_path.write_text(page_outline or "<!-- outline dump failed -->", encoding="utf-8")
    print(f"[dump] 精简 outline 已写入：{outline_path}")

    print("\n[dump] 第 2 步：扫描字段锚点（用于执行器定位）...")
    set_widget_status(page, "正在扫描字段...", tone="working")

    all_fields: list[dict] = []
    for frame_index, frame in enumerate(page.frames):
        try:
            frame_fields = frame.evaluate(SCAN_FIELDS_V2_JS)
            for f in frame_fields:
                f["frame_index"] = frame_index
            all_fields.extend(frame_fields)
        except Exception as exc:
            if frame_index == 0:
                print(f"  Main frame scan failed: {exc}")
            continue

    print(f"[dump] 扫到 {len(all_fields)} 个字段")

    # 补扫：SCAN_FIELDS_V2_JS 有时会漏掉 .ant-select / .ant-picker 容器
    # （去重逻辑里 input 和 ant-select 的 closest 检查互相干扰）
    # 这里单独遍历一遍，补进 all_fields
    print("[dump] 补扫 ant-select / ant-picker / ant-cascader 容器...")
    try:
        extra = page.evaluate(_SCAN_MISSING_CONTAINERS_JS)
        existing_ids = {f.get("id", "") for f in all_fields if f.get("id")}
        added = 0
        for f in extra:
            if f.get("id") and f["id"] not in existing_ids:
                all_fields.append(f)
                added += 1
        print(f"[dump] 补扫到 {added} 个漏扫字段（ant-select/ant-picker）")
    except Exception as exc:
        print(f"[dump] 补扫失败（不致命）：{exc}")

    # radio 选项从 DOM 直接提取（不需要点开下拉，radio 选项是静态渲染的）
    for field in all_fields:
        kind = _classify_kind(field)
        if kind == "radio" and not field.get("options"):
            field["options"] = _extract_radio_options(page, field)

    # 构造 schema（不带下拉选项——选项在 HTML/outline 里，LLM 自己看）
    print("[dump] 第 3 步：构造 schema + prompt...")
    set_widget_status(page, "生成输出文件...", tone="working")

    schema_fields: list[dict] = []
    for field in all_fields:
        kind = _classify_kind(field)
        anchor = _build_anchor(field)
        if not anchor["key"]:
            continue
        item = {
            "anchor": anchor,
            "kind": kind,
            "tag": field.get("tag", ""),
            "label": anchor["label"],
            "section": anchor["section"],
            "required": "ant-form-item-required" in str(field.get("label", "") or ""),
            "options": field.get("options") or [],
            "placeholder": anchor["placeholder"],
            "current_value": str(field.get("value") or field.get("selected_text") or field.get("text") or ""),
        }
        schema_fields.append(item)

    schema = {
        "page_url": page.url,
        "dumped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "field_count": len(schema_fields),
        "fields": schema_fields,
    }

    schema_path = out_dir / "form_schema.json"
    schema_path.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[dump] schema 已写入：{schema_path}")

    # prompt 直接嵌入精简 outline，不再依赖附件上传
    prompt = build_prompt(schema_fields, len(page_html), page_outline)
    prompt_path = out_dir / "form_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    print(f"[dump] prompt 已写入：{prompt_path}")

    set_widget_status(
        page,
        f"完成！{len(schema_fields)} 字段 + {len(page_outline)} 字符 outline",
        enabled=True,
        button_text="抓取完成",
        tone="done",
    )

    print()
    print("=" * 60)
    print("抓取完成。下一步：")
    print(f"1. 打开 {prompt_path.name}")
    print("2. 把简历 JSON 粘贴到末尾指定位置")
    print("3. 整段复制发给豆包/DeepSeek/Kimi（精简表单结构已内嵌在 prompt 里）")
    print(f"   备用：如果客户端支持文件上传，可上传 {outline_path.name}")
    print("4. 拿到模型返回的 JSON，保存为 form_plan.json（和本脚本同目录）")
    print("5. 重新运行 RUN.cmd → v3 → [2] 按计划填表")
    print("=" * 60)

    return schema


def build_prompt(fields: list[dict], html_size: int = 0, outline: str = "") -> str:
    """拼装发送给 LLM 的提示词。精简 outline 直接嵌入，不依赖附件上传。"""
    # 读取候选人简历 JSON（和本脚本同目录的 resume_data.json）
    resume_path = SCRIPT_DIR / "resume_data.json"
    resume_json = ""
    if resume_path.exists():
        try:
            resume_json = resume_path.read_text(encoding="utf-8").strip()
            print(f"[dump] 已自动加载简历：{resume_path.name}（{len(resume_json)} 字符）")
        except Exception as exc:
            resume_json = f"（读取 resume_data.json 失败: {exc}）"
    else:
        resume_json = "（未找到 resume_data.json，请把简历 JSON 粘贴到这里）"

    # 简化字段清单：每个字段一行
    field_lines: list[str] = []
    for i, f in enumerate(fields, 1):
        anchor = f["anchor"]
        kind = f["kind"]
        label = f["label"] or "(无标签)"
        section = f["section"] or "(无分区)"
        options = f.get("options") or []
        opt_text = ""
        if options:
            opt_text = " | ".join(
                f'"{o.get("text", "")}"' for o in options[:30]
            )
        cur = f.get("current_value", "")
        cur_text = f' [当前值: {cur}]' if cur else ""

        anchor_id = anchor.get("id") or anchor.get("name") or anchor.get("label")
        field_lines.append(
            f"{i}. anchor_id={anchor_id!r} kind={kind} label={label!r} "
            f"section={section!r}{cur_text}"
            + (f" options=[{opt_text}]" if opt_text else "")
        )

    fields_block = "\n".join(field_lines)
    html_kb = (html_size + 1023) // 1024
    outline_kb = (len(outline) + 1023) // 1024

    # 精简 outline 内嵌到 prompt
    outline_section = f"""
# 表单精简结构（{outline_kb} KB，从完整 HTML 抽取字段相关元素，去掉样式/脚本）
以下是表单的精简 DOM 结构，包含所有 label / input / select / textarea / radio / 选项文本
和它们的 id、name、placeholder、class（只保留有语义的）。
注意：ant-select 的下拉选项是异步加载的，不在此结构里——请根据字段标签推断应该选什么值。

```html
{outline}
```
""" if outline else f"\n（精简 outline 抓取失败，请用户上传 form_page.html 附件，约 {html_kb} KB）\n"

    return f"""你是一个招聘表单填写辅助助手。
{outline_section}
# 程序提取的字段锚点清单（schema，共 {len(fields)} 个字段）
下面是工具自动扫到的字段锚点，含稳定的 anchor_id（DOM id）、kind、label、section、当前值。
这份清单用于校验上面的 outline 并提供准确的 anchor_id（你的 fills 必须用这里的 anchor_id）。

{fields_block}

# 你的任务
结合上方的"表单精简结构"和"字段锚点清单"，为每个能从简历找到依据的字段输出一条填写计划。
简历里没有明确依据的字段直接跳过，不要出现在 fills 中。

# 输出格式（严格 JSON，不要 Markdown 围栏，不要解释，不要思考过程）
{{
  "fills": [
    {{
      "anchor_id": "字段的 anchor.id（必须来自上面 schema）",
      "kind": "text | textarea | select | dropdown | cascader | radio | date | checkbox",
      "value": "用于 text/textarea/cascader/date/checkbox 的值",
      "pick_label": "用于 select/dropdown/radio：要选中的选项可见文本",
      "note": "简短中文说明（≤30 字），解释为什么填这个值"
    }}
  ]
}}

# 各 kind 的填写规则
- text / textarea：value 写要填的字符串。textarea 支持换行用 \\n。
- select / dropdown：用 pick_label 指定要选中的选项文本。
  - 如果精简结构里有 <option> 标签，pick_label 从 option 文本里挑。
  - 如果是 ant-select（自定义下拉，没有 <option>），pick_label 写你认为应该选的值的中文文本
    （如"身份证"、"汉族"、"中共党员"）。执行器会实时打开下拉按文本模糊匹配。
  - 如果不确定有哪些选项，pick_label 写简历里的原值即可，执行器会尝试匹配。
- radio：必须用 pick_label，从 radio 字段的 options 里挑一个 text。
  - 如果 schema 里 options 为空，看精简结构里 radio 旁边的中文文本（如"男""女""是""否"），pick_label 写那个中文。
- cascader：value 写 "省 市"（用空格分隔），例如 "陕西 西安"。不要写完整地址。
- date：value 写 YYYY-MM-DD（如 2003-06-12）。
- checkbox：value 写 "是" 表示勾选，"否" 表示不勾（不勾选时直接不出现在 fills 中）。

# 严格规则
1. 宁可填错，不可留空——对必填字段（schema 标了 required），即使简历里没有明确值也要尽可能填：
   - 能推断就推断（如性别可从姓名推断，政治面貌团员/党员看简历是否提到）
   - 不能推断就从现有信息里找最接近的值（如英语水平从 certificates/cet6 推断）
   - 实在没有就填空值 ""（执行器会跳过），但不要静默跳过必填字段
2. 敏感信息（身份证号、手机号、邮箱）只在简历有现成值时填，不要编造。
3. 不要处理"上传证件照/生活照/简历解析"这类文件上传字段。
4. 不要处理"添加"按钮、提交按钮。
5. 时间类字段如果简历只给到年月，日填 "01"。
6. 同一字段只输出一条 fill。
7. 家庭成员有多条时，按精简结构里 resume_familyList_0_*、resume_familyList_1_* 的下标对应简历 family_members 数组。
8. radio 字段（性别、是否参加过正式工作、是否有近亲属在集团任职、家庭在集团任职）必须输出 fill：
   - 性别：从姓名推断男女
   - 是否参加过正式工作：看简历是否有 work_experience（非实习），没有填"否"
   - 是否有近亲属在集团任职：简历 family_members 为空时填"否"
   - 家庭在集团任职：简历 family_members 为空时填"否"
9. 证件类型/政治面貌/民族/婚姻状况/生源地/出生地/招聘信息来源/面试期望城市 等必填下拉：
   - 简历有值就填，没有就尽量从相关字段推断
   - 证件号：简历 id_number 为空时不填（留空）
10. 身高/体重：简历 height_cm/weight_kg 为 0 时填空值 ""（执行器会跳过）
11. 自我评价/爱好特长：从 resume.self_evaluation / resume.hobbies / resume.certificates 综合生成

# 候选人简历 JSON
{resume_json}
"""


# ─────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Dump form schema + prompt for client-side LLM planning")
    parser.add_argument("--url", type=str, default="", help="Target form URL (skip to log in manually)")
    parser.add_argument("--profile-dir", type=str, default=str(DEFAULT_PROFILE_DIR))
    parser.add_argument("--out-dir", type=str, default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--fresh-profile", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out_dir).resolve()

    class Args:
        pass
    launch_args = Args()
    launch_args.profile_dir = args.profile_dir
    launch_args.headless = args.headless
    launch_args.fresh_profile = args.fresh_profile

    print("Dump Form Schema — 客户端排单模式第 1 步")
    print("保持此窗口打开。浏览器打开后请登录并进入表单页。")
    print()

    with sync_playwright() as p:
        context, _ = launch_browser_context(p, launch_args)
        page = context.pages[0] if context.pages else context.new_page()

        if args.url:
            page.goto(args.url)

        install_dump_widget(page)
        print()
        print("==== 重要：抓取前请手动准备页面 ====")
        print("1. 登录并进入目标表单页")
        print("2. 手动点击所有需要填写的'添加'按钮，")
        print("   把实践经历/个人荣誉/社团活动/教育经历 等模块")
        print("   展开到你需要的条数（每条简历对应一个表单项）")
        print("3. 确认页面显示了所有要填的字段后，")
        print("   点击页面左上角紫色按钮：抓取表单源码")
        print("   （或按 Ctrl+Shift+D 触发）")
        print("=====================================")
        print()
        print("等待点击 抓取表单源码 按钮...")
        wait_for_dump_click(page)

        try:
            dump_schema(page, out_dir)
        except Exception as exc:
            print(f"\n抓取失败：{exc}")
            import traceback
            traceback.print_exc()
            input("按回车退出...")
            return 1

        print()
        input("抓取完成。可以关闭浏览器。按回车退出...")
        # 退出前自动保存 cookies（登录态）
        from form_filler_v2 import save_browser_cookies
        save_browser_cookies(context)
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
