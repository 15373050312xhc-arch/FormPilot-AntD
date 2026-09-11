"""
form_filler_v2.py — 新版简历自动填表引擎

架构：
- 程序负责"怎么填"：扫描字段、识别类型、执行填写
- API 只负责"填什么"：接收干净的问答对，返回答案
- API 不看 DOM，消除噪音和幻觉
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import tempfile
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
DEFAULT_PROJECT_ROOT = (
    ROOT_DIR
    if (ROOT_DIR / "doubao" / "client.py").exists()
    else Path(r"C:\Users\lenovo\Desktop\Zhihuishu-Autonomous-Agent")
)
DEFAULT_RESUME_JSON = SCRIPT_DIR / "resume_data_wang_mengxuan.json"
DEFAULT_PROFILE_DIR = ROOT_DIR / "work" / "browser-resume-autofill-profile"


# ─────────────────────────────────────────────────────────
# JavaScript: 增强版字段扫描器（支持自定义下拉框选项提取）
# ─────────────────────────────────────────────────────────

SCAN_FIELDS_V2_JS = r"""
async () => {
  const text = (node) =>
    (node && (node.innerText || node.textContent || '') || '')
      .replace(/\s+/g, ' ').trim();

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  const isVisible = (el) => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none'
      && r.width > 0 && r.height > 0;
  };

  const primaryLabelFor = (el) => {
    const clean = (s) => String(s || '')
      .replace(/\s+/g, ' ')
      .replace(/^[*＊\s]+/, '')
      .replace(/[：:]\s*$/, '')
      .trim();
    const cands = [];
    if (el.id) {
      const lb = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lb) cands.push(clean(text(lb)));
    }
    const wrapped = el.closest('label');
    if (wrapped) cands.push(clean(text(wrapped).replace(text(el), '')));
    let probe = el.parentElement;
    for (let i = 0; probe && i < 5; i++, probe = probe.parentElement) {
      const ll = probe.querySelector(
        '.ant-form-item-label, .el-form-item__label, .layui-form-label, label'
      );
      if (ll) cands.push(clean(text(ll)));
      const prev = probe.previousElementSibling;
      if (prev) cands.push(clean(text(prev)));
    }
    const usable = cands
      .map(s => s.replace(/请输入|请选择|关键字搜索/g, '').trim())
      .filter(s => s && s.length <= 30 && !/\|/.test(s));
    return usable[0] || clean(el.getAttribute('aria-label') || el.getAttribute('placeholder') || '');
  };

  const labelFor = (el) => {
    const parts = [];
    if (el.id) {
      const lb = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lb) parts.push(text(lb));
    }
    const wrapped = el.closest('label');
    if (wrapped) parts.push(text(wrapped));
    const aria = el.getAttribute('aria-label');
    if (aria) parts.push(aria);
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      labelledBy.split(/\s+/).forEach(id => {
        const n = document.getElementById(id);
        if (n) parts.push(text(n));
      });
    }
    const ph = el.getAttribute('placeholder');
    if (ph) parts.push(ph);
    const title = el.getAttribute('title');
    if (title) parts.push(title);
    const formItem = el.closest(
      '.ant-form-item, .el-form-item, .form-item, .form-group, .layui-form-item, tr, li'
    );
    if (formItem) parts.push(text(formItem).slice(0, 260));
    let probe = el.parentElement;
    for (let i = 0; probe && i < 4; i++, probe = probe.parentElement) {
      const prev = probe.previousElementSibling;
      if (prev) parts.push(text(prev).slice(0, 120));
      const ll = probe.querySelector(
        '.ant-form-item-label, .el-form-item__label, .layui-form-label, label'
      );
      if (ll) parts.push(text(ll).slice(0, 120));
    }
    return Array.from(new Set(parts.filter(Boolean))).join(' | ');
  };

  const isCustomDropdown = (el) => {
    if (el.classList.contains('ant-select')) return true;
    if (el.classList.contains('el-select')) return true;
    if (el.getAttribute('role') === 'combobox'
        && el.tagName.toLowerCase() !== 'input') return true;
    if (el.querySelector('.ant-select-selector, .el-select__placeholder'))
      return true;
    return false;
  };

  const isDatePicker = (el) => {
    if (el.classList.contains('ant-calendar-picker')) return true;
    if (el.classList.contains('ant-picker')) return true;
    if (el.classList.contains('el-date-editor')) return true;
    if (el.classList.contains('el-date-picker')) return true;
    return false;
  };

  const isRadioGroup = (el) => {
    if (el.classList.contains('ant-radio-group')) return true;
    if (el.classList.contains('el-radio-group')) return true;
    if (el.getAttribute('role') === 'radiogroup') return true;
    if (el.querySelector('.ant-radio-wrapper, .el-radio')) return true;
    return false;
  };

  const extractVisibleOptions = () => {
    const sels = [
      '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content',
      '.ant-select-dropdown:not([style*="display: none"]) .ant-select-item-option-content',
      '.ant-select-item-option-content',
      '.ant-select-item',
      '.el-select-dropdown .el-select-dropdown__item',
      '.el-select-dropdown__item',
      '[role="listbox"] [role="option"]',
      '[role="option"]',
      '[role="menuitem"]',
      '.rc-virtual-list-holder-inner > div',
      '.ant-select-dropdown li',
      '.el-select-dropdown li',
    ];
    const seen = new Set();
    const opts = [];
    for (const sel of sels) {
      for (const el of document.querySelectorAll(sel)) {
        if (!isVisible(el)) continue;
        const t = text(el);
        if (!t || seen.has(t)) continue;
        seen.add(t);
        opts.push({
          text: t,
          value: el.getAttribute('data-value')
            || el.getAttribute('title')
            || el.getAttribute('aria-label')
            || t,
        });
      }
      if (opts.length > 0) break;
    }
    return opts.slice(0, 80);
  };

  const extractFieldContext = (el) => {
    const chain = [];
    let node = el;
    for (let i = 0; node && i < 5; i++, node = node.parentElement) {
      chain.push({
        tag: node.tagName,
        class: String(node.className || ''),
        role: node.getAttribute('role') || '',
        aria: node.getAttribute('aria-label') || '',
        text: text(node).slice(0, 260),
      });
    }
    return chain;
  };

  const findNearestSectionHeader = (el) => {
    let node = el;
    for (let i = 0; node && i < 12; i++, node = node.parentElement) {
      const prev = node.previousElementSibling;
      if (!prev) {
        const cls = String(node.className || '').toLowerCase();
        if (/item-box|section-box/.test(cls)) {
          const nameEl = node.querySelector('.item-name, .section-name');
          if (nameEl) {
            const t = text(nameEl);
            if (t && t.length <= 60) return t;
          }
        }
        continue;
      }
      const tag = prev.tagName.toLowerCase();
      if (['h1','h2','h3','h4','h5','h6'].includes(tag)) {
        const t = text(prev);
        if (t && t.length <= 60) return t;
      }
      const cls = String(prev.className || '').toLowerCase();
      if (/title|header|section|caption|legend|group-name|item-name/.test(cls)) {
        const t = text(prev);
        if (t && t.length <= 60) return t;
      }
      const role = prev.getAttribute('role') || '';
      if (['heading', 'group', 'region'].includes(role)) {
        const t = text(prev);
        if (t && t.length <= 60) return t;
      }
    }
    let probe = el.parentElement;
    for (let i = 0; probe && i < 6; i++, probe = probe.parentElement) {
      const heading = probe.querySelector(
        'h1, h2, h3, h4, h5, h6, .ant-form-item-label span, .section-title, .group-title, .item-name, legend'
      );
      if (heading) {
        const t = text(heading);
        if (t && t.length <= 60 && t.length >= 2) return t;
      }
    }
    return '';
  };

  const selector = [
    'input:not([type=hidden])',
    'textarea',
    'select',
    '.ant-select',
    '.ant-calendar-picker',
    '.ant-picker',
    '.el-select',
    '.el-date-editor',
    '.ant-radio-group',
    '.el-radio-group',
    '[role="radiogroup"]',
    '[contenteditable="true"]',
  ].join(',');

  const fields = [];
  const elements = Array.from(document.querySelectorAll(selector));

  for (let index = 0; index < elements.length; index++) {
    const el = elements[index];
    if (!isVisible(el) || el.disabled) continue;
    if (el.closest('.ant-select') && !el.classList.contains('ant-select')) continue;
    if (el.closest('.ant-calendar-picker') && !el.classList.contains('ant-calendar-picker')) continue;
    if (el.closest('.ant-picker') && !el.classList.contains('ant-picker')) continue;
    if (el.closest('.el-select') && !el.classList.contains('el-select')) continue;
    if (el.closest('.el-date-editor') && !el.classList.contains('el-date-editor')) continue;
    if (el.closest('.ant-radio-group') && !el.classList.contains('ant-radio-group')) continue;
    if (el.closest('.el-radio-group') && !el.classList.contains('el-radio-group')) continue;
    if (el.closest('[role="radiogroup"]') && !el.getAttribute('role') === 'radiogroup') continue;

    const tag = el.tagName.toLowerCase();
    const attrType = (el.getAttribute('type') || tag).toLowerCase();
    if (['button', 'submit', 'reset', 'image', 'file', 'password'].includes(attrType)) continue;
    if (el.classList.contains('ant-select-disabled')) continue;
    if (el.classList.contains('el-select') && el.classList.contains('is-disabled')) continue;

    const uid = `arf_${Date.now()}_${index}`;
    el.setAttribute('data-resume-autofill-id', uid);

    let fieldType, options = [];

    if (tag === 'select') {
      fieldType = 'select';
      options = Array.from(el.options).map(o => ({
        value: o.value,
        text: text(o) || o.label || o.value,
      }));
    } else if (el.classList.contains('ant-cascader')) {
      fieldType = 'cascader';
    } else if (isCustomDropdown(el)) {
      fieldType = 'custom-dropdown';
    } else if (isDatePicker(el)) {
      fieldType = 'date-picker';
    } else if (isRadioGroup(el)) {
      fieldType = 'radio';
    } else if (attrType === 'radio') {
      fieldType = 'radio';
    } else if (attrType === 'checkbox') {
      fieldType = 'checkbox';
    } else {
      fieldType = 'text';
    }

    const selectedText = tag === 'select' && el.selectedIndex >= 0
      ? text(el.options[el.selectedIndex])
      : el.classList.contains('ant-select')
        ? text(el.querySelector('.ant-select-selection-item, .ant-select-selection-selected-value, .ant-select-selection__choice__content') || '')
        : el.classList.contains('el-select')
          ? text(el.querySelector('.el-select__selected-item, .el-input__inner') || '')
          : '';

    const placeholderText = el.getAttribute('placeholder')
      || text(el.querySelector('.ant-select-selection-placeholder, .ant-select-selection__placeholder, .el-select__placeholder') || '');

    fields.push({
      uid,
      tag,
      type: fieldType,
      input_type: attrType,
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: placeholderText,
      readonly: Boolean(
        el.readOnly
        || el.classList.contains('ant-select')
        || el.classList.contains('ant-calendar-picker')
        || el.classList.contains('ant-picker')
        || el.classList.contains('el-select')
        || el.classList.contains('el-date-editor')
      ),
      checked: Boolean(el.checked),
      value: el.value || '',
      text: el.isContentEditable ? text(el) : '',
      selected_text: selectedText,
      primary_label: primaryLabelFor(el),
      label: labelFor(el),
      section_header: findNearestSectionHeader(el),
      dom_context: extractFieldContext(el),
      options,
    });
  }

  return fields;
}
"""


# ─────────────────────────────────────────────────────────
# JavaScript: React-safe 文本填充
# ─────────────────────────────────────────────────────────

APPLY_TEXT_JS = r"""
({ uid, value }) => {
  let el = document.querySelector(`[data-resume-autofill-id="${uid}"]`);
  if (!el) return { ok: false, error: 'not_found' };
  const textValue = value == null ? '' : String(value);

  if (el.isContentEditable) {
    el.focus();
    el.innerText = textValue;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    return { ok: true };
  }

  // 如果 stamp 到了容器（div/.ant-form-item），向下找真正的 input/textarea
  const tag = el.tagName.toLowerCase();
  if (tag !== 'input' && tag !== 'textarea' && tag !== 'select') {
    const inner = el.querySelector('input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]), textarea, select');
    if (inner) el = inner;
  }

  el.focus();
  const proto = el.tagName.toLowerCase() === 'textarea'
    ? HTMLTextAreaElement.prototype
    : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
  if (setter) setter.call(el, textValue);
  else el.value = textValue;
  el.dispatchEvent(new Event('focus', { bubbles: true }));
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new Event('blur', { bubbles: true }));
  return { ok: true };
}
"""


# ─────────────────────────────────────────────────────────
# JavaScript: 原生 <select> 选项匹配
# ─────────────────────────────────────────────────────────

APPLY_SELECT_JS = r"""
({ uid, value }) => {
  const el = document.querySelector(`[data-resume-autofill-id="${uid}"]`);
  if (!el) return { ok: false, error: 'not_found' };
  const norm = (s) => String(s || '').replace(/\s+/g, '').toLowerCase();
  const wanted = norm(value);
  const option = Array.from(el.options).find(o =>
    norm(o.value) === wanted || norm(o.textContent) === wanted
  ) || Array.from(el.options).find(o =>
    norm(o.value).includes(wanted) || norm(o.textContent).includes(wanted)
    || wanted.includes(norm(o.textContent))
  );
  if (!option) return { ok: false, error: 'option_not_found' };
  el.value = option.value;
  option.selected = true;
  el.selectedIndex = el.options.indexOf(option);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { ok: true, selected: option.textContent.trim() };
}
"""


# ─────────────────────────────────────────────────────────
# JavaScript: Widget（复用原有逻辑）
# ─────────────────────────────────────────────────────────

INSTALL_WIDGET_JS = r"""
() => {
  if (window.top !== window.self) return false;
  if (document.getElementById('resume-autofill-widget')) return true;

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
  button.textContent = '识别并填表 (v2)';
  button.style.cssText = [
    'height:48px', 'padding:0 18px', 'border:0', 'border-radius:10px',
    'background:#0052d9', 'color:#fff', 'font-size:16px', 'font-weight:600',
    'box-shadow:0 10px 30px rgba(0,0,0,.28)', 'cursor:pointer',
    'outline:3px solid rgba(255,255,255,.92)'
  ].join(';');

  const status = document.createElement('div');
  status.id = 'resume-autofill-status';
  status.textContent = '或按 Ctrl+Shift+F';
  status.style.cssText = [
    'margin-top:8px', 'max-width:220px', 'padding:7px 10px',
    'border-radius:8px', 'background:rgba(17,24,39,.88)',
    'color:#fff', 'font-size:12px', 'line-height:1.35',
    'text-align:center', 'box-shadow:0 8px 24px rgba(0,0,0,.18)'
  ].join(';');

  button.addEventListener('click', () => {
    window.__resumeAutofillRequested = true;
    button.disabled = true;
    button.textContent = '处理中...';
    button.style.background = '#64748b';
    button.style.cursor = 'wait';
    status.textContent = '正在识别页面字段';
  });

  document.addEventListener('keydown', (event) => {
    if (event.ctrlKey && event.shiftKey && String(event.key || '').toLowerCase() === 'f') {
      event.preventDefault();
      window.__resumeAutofillRequested = true;
      button.disabled = true;
      button.textContent = '处理中...';
      button.style.background = '#64748b';
      button.style.cursor = 'wait';
      status.textContent = '正在识别页面字段';
    }
  }, true);

  box.appendChild(button);
  box.appendChild(status);
  document.documentElement.appendChild(box);
  return true;
}
"""

SET_WIDGET_STATUS_JS = r"""
({ status, enabled, buttonText, tone }) => {
  const button = document.querySelector('#resume-autofill-widget button');
  const statusEl = document.getElementById('resume-autofill-status');
  if (statusEl && status) statusEl.textContent = status;
  if (button) {
    if (buttonText) button.textContent = buttonText;
    if (typeof enabled === 'boolean') {
      button.disabled = !enabled;
      button.style.cursor = enabled ? 'pointer' : 'wait';
    }
    if (tone === 'ready') button.style.background = '#1677ff';
    if (tone === 'working') button.style.background = '#64748b';
    if (tone === 'done') button.style.background = '#16a34a';
    if (tone === 'error') button.style.background = '#dc2626';
  }
  return true;
}
"""


# ─────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────

def normalize_label(field: dict) -> str:
    raw = " ".join(
        str(field.get(k) or "")
        for k in ("primary_label", "label", "name", "id", "placeholder")
    )
    raw = raw.replace("请输入", "").replace("请选择", "").replace("关键字搜索", "")
    raw = re.sub(r"[\s*：:()（）|_\-]+", "", raw)
    return raw


def normalize_primary_label(field: dict) -> str:
    raw = str(field.get("primary_label") or "")
    raw = raw.replace("请输入", "").replace("请选择", "").replace("关键字搜索", "")
    raw = re.sub(r"[\s*：:()（）|_\-]+", "", raw)
    return raw


def extract_output_text(response: dict) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    for item in response.get("output", []) if isinstance(response.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    text = "\n".join(parts).strip()
    if text:
        return text
    raise RuntimeError(f"Cannot extract text from response: {response}")


def parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            return json.loads(match.group())
        raise


def load_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def load_doubao_client(project_root: Path) -> Any:
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from doubao.client import DoubaoClient, DoubaoConfig
    return DoubaoClient(DoubaoConfig.from_env())


# ─────────────────────────────────────────────────────────
# 字段分类
# ─────────────────────────────────────────────────────────

def classify_field(field: dict) -> str:
    """返回: text / dropdown / cascader / radio / checkbox / date / textarea"""
    ft = field.get("type", "")
    if ft == "cascader":
        return "cascader"
    if ft == "select" or ft == "custom-dropdown":
        return "dropdown"
    if ft == "date-picker":
        return "date"
    if ft == "radio":
        return "radio"
    if ft == "checkbox":
        return "checkbox"
    if field.get("tag") == "textarea":
        return "textarea"
    input_type = field.get("input_type", "")
    if input_type == "date":
        return "date"
    return "text"


# ─────────────────────────────────────────────────────────
# 简历候选值生成
# ─────────────────────────────────────────────────────────

def get_candidates(
    field: dict,
    resume: dict,
    all_fields: list[dict] | None = None,
    field_index: int | None = None,
) -> list[str]:
    """根据字段标签从简历中提取候选值"""
    label = normalize_label(field)
    if not label:
        return []

    cands: list[str] = []

    # 检查是否在家庭成员区域 — 通过 section_header 和 DOM 属性双重检测
    section = field.get("section_header", "")
    is_family_section = any(
        kw in section for kw in
        ["家庭", "家庭成员", "家庭主要成员", "家属", "主要家庭成员"]
    ) or _is_family_field_by_dom(field)

    # 家庭成员区域内有特定含义的字段关键词（不应使用本人信息）
    family_specific_keywords = [
        "姓名", "名字", "手机", "电话", "联系方式", "联系电话",
        "单位", "工作", "就业", "职务", "职位", "岗位", "部门",
        "出生日期", "出生年月", "生日", "年龄",
    ]

    # 个人信息
    personal_map = [
        (["姓名", "名字"], lambda r: [r.get("name", "")]),
        (["性别"], lambda r: [r.get("gender", "")]),
        (["手机", "电话", "联系方式", "联系电话"], lambda r: [r.get("phone", "")]),
        (["微信"], lambda r: [r.get("wechat", "")]),
        (["邮箱", "电子邮件"], lambda r: [r.get("email", "")]),
        (["出生日期", "出生年月", "生日"], lambda r: [r.get("birth_date", "")]),
        (["证件类型", "身份证件类型"], lambda r: ["居民身份证"]),
        (["证件号", "证件号码", "身份证号", "身份证号码"], lambda r: [r.get("id_number", "")]),
        (["国籍", "国家或地区", "国家"], lambda r: ["中国"]),
        (["民族"], lambda r: [r.get("ethnicity") or "汉族"]),
        (["婚姻", "婚否"], lambda r: [r.get("marital_status", "")]),
        (["政治面貌"], lambda r: [r.get("political_status", "")]),
        (["地址", "联系地址", "通讯地址", "现住址"],
         lambda r: [r.get("address") or r.get("current_location") or ""]),
        (["邮编", "邮政编码"], lambda r: [r.get("postal_code") or ""]),
        (["户口所在", "户籍"], lambda r: [r.get("hukou_location") or r.get("birthplace") or ""]),
        (["生源地"], lambda r: [r.get("birthplace") or r.get("hukou_location") or ""]),
        (["户口类型", "户籍类型"], lambda r: [r.get("hukou_type") or ""]),
        (["工作地点", "首选工作地点", "期望工作地点"],
         lambda r: [r.get("preferred_work_location") or r.get("current_location") or ""]),
        (["身高"], lambda r: [str(r["height_cm"]) if r.get("height_cm") else ""]),
        (["体重"], lambda r: [str(r["weight_kg"]) if r.get("weight_kg") else ""]),
    ]
    # 如果在家庭成员区域且字段有家庭成员特定含义，跳过 personal_map
    skip_personal = is_family_section and any(kw in label for kw in family_specific_keywords)

    if not skip_personal:
        for keywords, getter in personal_map:
            if any(kw in label for kw in keywords):
                vals = getter(resume)
                cands.extend(v for v in vals if v)
                break

    # Field dependency logic: 证件类型 → 证件号
    # If this is a 证件号 field and there's a nearby 证件类型 field, use id_number
    # (Assuming 证件类型 will be "身份证" since that's the default)
    if any(kw in label for kw in ["证件号", "证件号码", "身份证号", "身份证号码"]) and all_fields is not None:
        # Look for a nearby 证件类型 field in the same form section
        for j in range(max(0, field_index - 5), min(len(all_fields), field_index + 6)):
            if j == field_index:
                continue
            other_field = all_fields[j]
            other_label = normalize_label(other_field)
            # Check if this is a 证件类型 field
            if any(kw in other_label for kw in ["证件类型", "身份证件类型"]):
                # Assume it will be filled with "身份证", so use id_number
                id_num = resume.get("id_number", "")
                if id_num and id_num not in cands:
                    cands.insert(0, id_num)  # Prioritize id_number
                break

    # 政治面貌特殊处理
    if "政治面貌" in label:
        cands.append(resume.get("political_status", ""))

    # 学历相关
    if any(kw in label for kw in ["学历", "学位", "最高学历"]):
        for e in resume.get("education", []):
            v = e.get("degree", "")
            if v and v not in cands:
                cands.append(v)

    # 学校
    if any(kw in label for kw in ["学校", "院校", "毕业院校", "毕业学校"]):
        # Check if we're in an education section
        section = field.get("section_header", "")
        is_edu_section = any(kw in section for kw in ["教育", "学历", "学习经历"])
        
        if is_edu_section and all_fields is not None and field_index is not None:
            # Determine which education entry this row belongs to
            edu_idx = _determine_education_entry_index(field, all_fields, field_index, resume)
            if edu_idx is not None:
                educations = resume.get("education", [])
                if 0 <= edu_idx < len(educations):
                    v = educations[edu_idx].get("school", "")
                    if v and v not in cands:
                        cands.append(v)
        else:
            # Fallback: add all schools
            for e in resume.get("education", []):
                v = e.get("school", "")
                if v and v not in cands:
                    cands.append(v)

    # 专业
    if "专业" in label and "专" in label:
        section = field.get("section_header", "")
        is_edu_section = any(kw in section for kw in ["教育", "学历", "学习经历"])
        
        if is_edu_section and all_fields is not None and field_index is not None:
            edu_idx = _determine_education_entry_index(field, all_fields, field_index, resume)
            if edu_idx is not None:
                educations = resume.get("education", [])
                if 0 <= edu_idx < len(educations):
                    v = educations[edu_idx].get("major", "")
                    if v and v not in cands:
                        cands.append(v)
        else:
            for e in resume.get("education", []):
                v = e.get("major", "")
                if v and v not in cands:
                    cands.append(v)

    # 院系/学院
    if any(kw in label for kw in ["院系", "学院", "系所"]):
        section = field.get("section_header", "")
        is_edu_section = any(kw in section for kw in ["教育", "学历", "学习经历"])
        
        if is_edu_section and all_fields is not None and field_index is not None:
            edu_idx = _determine_education_entry_index(field, all_fields, field_index, resume)
            if edu_idx is not None:
                educations = resume.get("education", [])
                if 0 <= edu_idx < len(educations):
                    # Try to get department from major or school name
                    v = educations[edu_idx].get("department", "") or ""
                    if not v:
                        # Fallback: use first word of major as department hint
                        major = educations[edu_idx].get("major", "")
                        if major:
                            v = major.split("(")[0].strip()[:10]  # Simplified
                    if v and v not in cands:
                        cands.append(v)

    # 学习形式
    if any(kw in label for kw in ["学习形式", "学习方式", "就读形式", "全日制"]):
        section = field.get("section_header", "")
        is_edu_section = any(kw in section for kw in ["教育", "学历", "学习经历"])
        
        if is_edu_section and all_fields is not None and field_index is not None:
            edu_idx = _determine_education_entry_index(field, all_fields, field_index, resume)
            if edu_idx is not None:
                educations = resume.get("education", [])
                if 0 <= edu_idx < len(educations):
                    v = educations[edu_idx].get("study_type", "")
                    if v and v not in cands:
                        cands.append(v)
        else:
            for e in resume.get("education", []):
                v = e.get("study_type", "")
                if v and v not in cands:
                    cands.append(v)

    # 是否出国
    if "是否出国" in label or "海外学习" in label:
        section = field.get("section_header", "")
        is_edu_section = any(kw in section for kw in ["教育", "学历", "学习经历"])
        
        if is_edu_section and all_fields is not None and field_index is not None:
            edu_idx = _determine_education_entry_index(field, all_fields, field_index, resume)
            if edu_idx is not None:
                educations = resume.get("education", [])
                if 0 <= edu_idx < len(educations):
                    v = educations[edu_idx].get("is_abroad", "")
                    if v and v not in cands:
                        cands.append(v)
        else:
            for e in resume.get("education", []):
                v = e.get("is_abroad", "")
                if v and v not in cands:
                    cands.append(v)

    # 专业排名/成绩排名
    if any(kw in label for kw in ["排名", "成绩排名", "专业排名"]):
        section = field.get("section_header", "")
        is_edu_section = any(kw in section for kw in ["教育", "学历", "学习经历"])
        
        if is_edu_section and all_fields is not None and field_index is not None:
            edu_idx = _determine_education_entry_index(field, all_fields, field_index, resume)
            if edu_idx is not None:
                educations = resume.get("education", [])
                if 0 <= edu_idx < len(educations):
                    v = educations[edu_idx].get("ranking", "")
                    if v and v not in cands:
                        cands.append(v)

    # 教育经历时间 (入学时间/毕业时间)
    if any(kw in label for kw in ["时间", "入学", "毕业", "起止时间", "就读时间"]):
        section = field.get("section_header", "")
        is_edu_section = any(kw in section for kw in ["教育", "学历", "学习经历"])
        
        if is_edu_section and all_fields is not None and field_index is not None:
            edu_idx = _determine_education_entry_index(field, all_fields, field_index, resume)
            if edu_idx is not None:
                educations = resume.get("education", [])
                if 0 <= edu_idx < len(educations):
                    # Determine if this is start or end date based on label
                    if any(kw in label for kw in ["开始", "入学", "起始", "起"]):
                        v = educations[edu_idx].get("start_date", "")
                    elif any(kw in label for kw in ["结束", "毕业", "终止", "止"]):
                        v = educations[edu_idx].get("end_date", "")
                    else:
                        # Generic "时间" - try both
                        v = educations[edu_idx].get("start_date", "") or educations[edu_idx].get("end_date", "")
                    
                    if v and v not in cands:
                        cands.append(v)

    # 长文本字段
    long_text_map = [
        (["爱好", "兴趣", "特长"], lambda r: [r.get("hobbies", "")]),
        (["自我评价", "自我描述", "个人评价", "个人总结"], lambda r: [r.get("self_evaluation", "")]),
        (["应聘理由", "求职理由", "申请理由"], lambda r: [r.get("application_reason", "")]),
        (["计算机", "电脑水平"], lambda r: [r.get("computer_skill", "")]),
        (["技能", "其他技能", "其它技能"],
         lambda r: [r.get("other_skills") or r.get("computer_skill") or ""]),
    ]
    for keywords, getter in long_text_map:
        if any(kw in label for kw in keywords):
            vals = getter(resume)
            cands.extend(v for v in vals if v)
            break

    # 是/否类问题
    yn_map = {
        "是否接受调剂": "accept_work_location_adjustment",
        "调剂": "work_location_adjustment",
        "亲属": "relatives_in_company",
        "处分": "bad_record",
        "违纪": "bad_record",
        "健康": "health_issue_affecting_work",
        "如实": "truthfulness_guarantee",
        "背景调查": "agree_background_check",
        "境外永居": "mainland_outside_permanent_residency",
    }
    for keyword, qkey in yn_map.items():
        if keyword in label:
            v = resume.get("questions", {}).get(qkey, "")
            if v:
                cands.append(v)
            break

    # 家庭成员 — 优先从 DOM name/id 属性提取索引，其次用 label 和 section 推断
    family_idx = _extract_family_index_from_name(field)

    if family_idx is None:
        family_idx = _extract_family_index(label)

    if family_idx is None and is_family_section and all_fields is not None:
        family_idx = _determine_family_member_index(
            field, all_fields, field_index, resume
        )

    if family_idx is not None:
        members = resume.get("family_members", [])
        if 0 <= family_idx < len(members):
            m = members[family_idx]
            if "姓名" in label or "名字" in label:
                cands.append(m.get("name", ""))
            elif "关系" in label:
                cands.append(m.get("relationship", ""))
            elif "电话" in label or "手机" in label or "联系" in label:
                cands.append(m.get("phone", ""))
            elif "单位" in label or "工作" in label or "就业" in label:
                cands.append(m.get("employer", ""))
            elif "职务" in label or "职位" in label or "岗位" in label:
                cands.append(m.get("position", ""))
            elif "部门" in label:
                cands.append(m.get("department", ""))

    return [c for c in cands if c]


def _is_family_field_by_dom(field: dict) -> bool:
    """Check if a field belongs to the family member section via DOM attributes."""
    name = field.get("name", "").lower()
    fid = field.get("id", "").lower()
    for attr in (name, fid):
        if not attr:
            continue
        if "family" in attr or "familylist" in attr or "family_list" in attr:
            return True
    dom_context = field.get("dom_context", [])
    if isinstance(dom_context, list):
        for ctx in dom_context:
            if not isinstance(ctx, dict):
                continue
            cls = str(ctx.get("class", "")).lower()
            if "family" in cls:
                return True
    return False


def _extract_family_index_from_name(field: dict) -> int | None:
    """Extract family member index from DOM name pattern like familyList_0_name."""
    name = field.get("name", "")
    fid = field.get("id", "")
    for attr in (name, fid):
        if not attr:
            continue
        m = re.search(r"family[_]?list[_]?(\d+)", attr, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def _extract_family_index(label: str) -> int | None:
    for i, kw in enumerate(["父亲", "母亲", "配偶"]):
        if kw in label:
            return i
    if "家庭成" in label or "家庭成员" in label:
        m = re.search(r"(\d+)", label)
        if m:
            return int(m.group(1)) - 1
        return 0
    return None


def _determine_family_member_index(
    field: dict,
    all_fields: list[dict],
    field_index: int | None,
    resume: dict | None = None,
) -> int | None:
    """Determine which family member row this field belongs to.
    
    Strategy 1: If there's a preceding "关系类型" field in the same section, 
                extract its filled value and match it to find the correct member.
    Strategy 2: Count how many "关系类型" or "姓名" fields came before to determine row index.
    """
    if field_index is None or all_fields is None:
        return None

    label = normalize_label(field)
    section = field.get("section_header", "")

    preceding_same_section = []
    for i in range(field_index):
        f = all_fields[i]
        if f.get("section_header", "") == section and section:
            preceding_same_section.append(f)

    # Strategy 1: Look for a "关系类型" field that was already filled
    # Match the relationship value to find the correct family member
    for f in reversed(preceding_same_section):
        f_label = normalize_label(f)
        if any(kw in f_label for kw in ["关系", "亲属关系"]):
            filled_value = f.get("_filled_value", "")
            if filled_value and resume:
                # Find which family member has this relationship
                members = resume.get("family_members", [])
                for idx, m in enumerate(members):
                    if m.get("relationship", "") == filled_value:
                        return idx
                # Fallback: try partial match
                for idx, m in enumerate(members):
                    rel = m.get("relationship", "")
                    if filled_value in rel or rel in filled_value:
                        return idx
            break

    # Strategy 2: Count how many "关系类型" fields came before to determine row index
    # This handles the case where we're filling the first field of a new row
    relation_count = 0
    for f in preceding_same_section:
        f_label = normalize_label(f)
        if any(kw in f_label for kw in ["关系", "亲属关系"]):
            relation_count += 1

    # If this is a relationship field itself, use the count
    if any(kw in label for kw in ["关系", "亲属关系"]):
        return relation_count

    # For other fields (姓名, 电话, 单位, etc.), check if they belong to the current row
    # by counting how many of their type came before
    field_type_key = ""
    if "姓名" in label or "名字" in label:
        field_type_key = "姓名"
    elif "电话" in label or "手机" in label:
        field_type_key = "电话"
    elif "单位" in label or "工作" in label:
        field_type_key = "单位"
    elif "职务" in label or "职位" in label:
        field_type_key = "职务"

    if not field_type_key:
        return None

    nth = 0
    for f in preceding_same_section:
        f_label = normalize_label(f)
        if field_type_key in f_label:
            nth += 1

    # The nth occurrence of this field type should correspond to the nth relationship
    # But we need to check if there's a relationship field at position nth
    if relation_count >= nth:
        return nth

    return None


def _determine_education_entry_index(
    field: dict,
    all_fields: list[dict],
    field_index: int | None,
    resume: dict | None = None,
) -> int | None:
    """Determine which education entry this field belongs to by looking at preceding fields."""
    if field_index is None or all_fields is None:
        return None

    label = normalize_label(field)
    section = field.get("section_header", "")

    # Find all preceding fields in the same section
    preceding_same_section = []
    for i in range(field_index):
        f = all_fields[i]
        if f.get("section_header", "") == section and section:
            preceding_same_section.append(f)

    # Look for a "学历" (degree) field that was already filled - this indicates which education row we're on
    for f in reversed(preceding_same_section):
        f_label = normalize_label(f)
        if any(kw in f_label for kw in ["学历", "学位"]):
            filled_value = f.get("_filled_value", "")
            if filled_value and resume:
                # Match the degree to find which education entry it corresponds to
                educations = resume.get("education", [])
                for idx, edu in enumerate(educations):
                    if edu.get("degree", "") == filled_value:
                        return idx
                break

    # Fallback: count how many "学校" fields came before to determine row index
    school_count = 0
    for f in preceding_same_section:
        f_label = normalize_label(f)
        if any(kw in f_label for kw in ["学校", "院校"]):
            school_count += 1

    # If this is a school/department/major field, use the count
    if any(kw in label for kw in ["学校", "院系", "专业", "排名", "全日制", "是否出国", "时间"]):
        return school_count

    return None


# ─────────────────────────────────────────────────────────
# API 答疑（仅当规则匹配不确定时调用）
# ──────────────────────────────────────────────────────────

def ask_api_for_answer(
    client: Any,
    field: dict,
    candidates: list[str],
    resume: dict,
    timeout_sec: float = 120,
) -> str:
    """
    给 API 发送干净的问答对，让 API 选择正确答案。
    API 看不到 DOM，只看到结构化的问题和候选答案。
    """
    label = normalize_primary_label(field) or normalize_label(field)
    options_text = [o.get("text", "") for o in field.get("options", [])[:50]]

    compact_resume = {
        "name": resume.get("name"),
        "gender": resume.get("gender"),
        "phone": resume.get("phone"),
        "email": resume.get("email"),
        "birth_date": resume.get("birth_date"),
        "ethnicity": resume.get("ethnicity"),
        "political_status": resume.get("political_status"),
        "marital_status": resume.get("marital_status"),
        "hukou_location": resume.get("hukou_location"),
        "birthplace": resume.get("birthplace"),
        "hukou_type": resume.get("hukou_type"),
        "id_number": resume.get("id_number"),
        "address": resume.get("address"),
        "current_location": resume.get("current_location"),
        "preferred_work_location": resume.get("preferred_work_location"),
        "education": [
            {"school": e.get("school"), "degree": e.get("degree"),
             "major": e.get("major"), "study_type": e.get("study_type")}
            for e in resume.get("education", [])
        ],
        "family_members": [
            {"name": m.get("name"), "relationship": m.get("relationship"),
             "phone": m.get("phone"), "employer": m.get("employer"),
             "position": m.get("position"), "department": m.get("department")}
            for m in resume.get("family_members", [])
        ],
    }

    payload = {
        "section_header": field.get("section_header", ""),
        "question": label,
        "candidates": candidates[:20],
        "page_options": options_text,
        "resume": compact_resume,
    }

    system_prompt = (
        "你是简历填表助手。根据简历数据，从候选答案中选择最适合的值。"
        "注意 section_header 上下文：如果在家庭成员区域，选择对应家庭成员的数据而非本人数据。"
        "只返回精确的答案原文（必须是候选答案或页面选项之一），不要解释。"
        "如果没有合适的选项，返回空字符串。"
    )

    try:
        response = client.create_response(
            user_text=json.dumps(payload, ensure_ascii=False),
            system_prompt=system_prompt,
            use_web_search=False,
            extra_payload={"temperature": 0, "max_output_tokens": 16000},
            timeout_sec=timeout_sec,
        )
        answer = extract_output_text(response).strip()
        valid = set(candidates) | set(options_text)
        valid = {v for v in valid if v}
        if not valid:
            return ""
        if answer in valid:
            return answer
        answer_norm = answer.replace(" ", "").lower()
        for v in valid:
            v_norm = v.replace(" ", "").lower()
            if answer_norm == v_norm or answer_norm in v_norm or v_norm in answer_norm:
                return v
        print(f"  API answer rejected (not in valid set): '{answer}'")
        return ""
    except Exception as exc:
        print(f"  API answer failed: {exc}")
        return ""


def ask_api_for_option_pick(
    client: Any,
    field: dict,
    candidates: list[str],
    timeout_sec: float = 120,
) -> str:
    """让 API 从页面选项中选择一个"""
    label = normalize_primary_label(field) or normalize_label(field)
    options = field.get("options", [])[:80]
    if not options:
        return ""

    payload = {
        "field": {
            "primary_label": field.get("primary_label", ""),
            "label": field.get("label", ""),
            "placeholder": field.get("placeholder", ""),
        },
        "preferred_candidates": candidates[:20],
        "options": [o.get("text", "") for o in options],
    }

    system_prompt = (
        "你是简历填表助手。根据字段含义和候选答案，从页面选项中选出最匹配的一个。"
        "只返回选项原文，不要解释。如果没有合适的，返回空字符串。"
    )

    try:
        response = client.create_response(
            user_text=json.dumps(payload, ensure_ascii=False),
            system_prompt=system_prompt,
            use_web_search=False,
            extra_payload={"temperature": 0, "max_output_tokens": 16000},
            timeout_sec=timeout_sec,
        )
        data = extract_output_text(response).strip()
        valid = {str(o.get("text", "")).strip() for o in options}
        if data in valid:
            return data
        for v in valid:
            if v and (v in data or data in v):
                return v
        return ""
    except Exception as exc:
        print(f"  API option pick failed: {exc}")
        return ""


# ─────────────────────────────────────────────────────────
# 字段填写执行
# ─────────────────────────────────────────────────────────

def _ensure_company_manual_fill(page: Any, field: dict) -> None:
    """Check the '自行填写' checkbox for family company fields if present."""
    name = field.get("name", "").lower()
    fid = field.get("id", "").lower()
    is_company = "company" in name or "company" in fid
    if not is_company:
        return
    try:
        page.evaluate(f"""(() => {{
            const el = document.querySelector('[data-resume-autofill-id="{field["uid"]}"]');
            if (!el) return;
            const formItem = el.closest('.ant-form-item');
            if (!formItem) return;
            const cb = formItem.querySelector('.ant-checkbox-input');
            if (cb && !cb.checked) cb.click();
        }})()""")
    except Exception:
        pass


def fill_text_field(page: Any, field: dict, value: str) -> bool:
    _ensure_company_manual_fill(page, field)
    try:
        result = page.evaluate(APPLY_TEXT_JS, {"uid": field["uid"], "value": value})
        return result.get("ok", False)
    except Exception as exc:
        print(f"  text fill failed: {exc}")
        return False


def fill_select_field(page: Any, field: dict, value: str) -> bool:
    # Try native <select> first
    try:
        result = page.evaluate(APPLY_SELECT_JS, {"uid": field["uid"], "value": value})
        if result.get("ok"):
            return True
    except Exception:
        pass

    locator = page.locator(f'[data-resume-autofill-id="{field["uid"]}"]')
    try:
        locator.select_option(label=value)
        return True
    except Exception:
        pass

    norm = value.replace(" ", "").lower()
    for opt in field.get("options", []):
        opt_text = str(opt.get("text", "")).replace(" ", "").lower()
        opt_val = str(opt.get("value", "")).replace(" ", "").lower()
        if norm == opt_text or norm == opt_val or norm in opt_text or opt_text in norm:
            try:
                locator.select_option(value=str(opt.get("value", "")))
                return True
            except Exception:
                pass

    # Fall through to custom dropdown (Ant Design / Element UI)
    return fill_custom_dropdown(page, field, value)


EXTRACT_DROPDOWN_OPTIONS_JS = r"""
({ uid }) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const text = (node) =>
    (node && (node.innerText || node.textContent || '') || '')
      .replace(/\s+/g, ' ').trim();
  const isVisible = (el) => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none'
      && r.width > 0 && r.height > 0;
  };

  const container = document.querySelector(`[data-resume-autofill-id="${uid}"]`);
  if (!container) return [];

  const clickTarget = container.querySelector('.ant-select-selector')
    || container.querySelector('.el-select__placeholder')
    || container.querySelector('.ant-select-selection-search-input')
    || container;
  clickTarget.click();
  clickTarget.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));

  const extractVisibleOptions = () => {
    const sels = [
      '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content',
      '.ant-select-dropdown:not([style*="display: none"]) .ant-select-item-option-content',
      '.ant-select-item-option-content',
      '.ant-select-item',
      '.el-select-dropdown .el-select-dropdown__item',
      '.el-select-dropdown__item',
      '[role="listbox"] [role="option"]',
      '[role="option"]',
      '[role="menuitem"]',
      '.rc-virtual-list-holder-inner > div',
      '.ant-select-dropdown li',
      '.el-select-dropdown li',
    ];
    const seen = new Set();
    const opts = [];
    for (const sel of sels) {
      for (const el of document.querySelectorAll(sel)) {
        if (!isVisible(el)) continue;
        const t = text(el);
        if (!t || seen.has(t)) continue;
        seen.add(t);
        opts.push({
          text: t,
          value: el.getAttribute('data-value')
            || el.getAttribute('title')
            || el.getAttribute('aria-label')
            || t,
        });
      }
      if (opts.length > 0) break;
    }
    return opts.slice(0, 80);
  };

  return (async () => {
    await sleep(300);
    const opts = extractVisibleOptions();
    document.body.click();
    await sleep(30);
    return opts;
  })();
}
"""


def extract_dropdown_options(page: Any, field: dict) -> list[dict]:
    """Open a single dropdown on-demand, extract its options, then close it."""
    uid = field["uid"]
    try:
        options = page.evaluate(EXTRACT_DROPDOWN_OPTIONS_JS, {"uid": uid})
        if isinstance(options, list):
            return options
    except Exception as exc:
        print(f"  dropdown option extraction failed: {exc}")
        try:
            page.locator("body").click()
        except Exception:
            pass
    return []


def fill_cascader_field(page: Any, field: dict, value: str) -> bool:
    """Fill a cascader (province→city) field by selecting two levels."""
    uid = field["uid"]
    locator = page.locator(f'[data-resume-autofill-id="{uid}"]')

    parts = _split_cascader_value(value)
    if not parts or len(parts) < 2:
        return fill_custom_dropdown(page, field, value)

    province, city = parts[0], parts[1]

    try:
        locator.click(force=True)
        page.wait_for_timeout(400)
    except Exception as exc:
        print(f"  cascader click failed: {exc}")
        return False

    if not _click_cascader_level(page, province):
        _close_cascader(page)
        return False

    page.wait_for_timeout(400)

    if not _click_cascader_level(page, city):
        _close_cascader(page)
        return False

    page.wait_for_timeout(200)
    _close_cascader(page)
    return True


def _split_cascader_value(value: str) -> list[str]:
    """Split '广东省 深圳市' or '广东深圳' into [province, city]."""
    for sep in [" ", " ", ",", "，", "-"]:
        if sep in value:
            parts = [p.strip() for p in value.split(sep, 1) if p.strip()]
            if len(parts) >= 2:
                return parts

    for suffix_pair in [("省", "市"), ("省", "区"), ("省", "县")]:
        p_suffix, c_suffix = suffix_pair
        if p_suffix in value:
            idx = value.index(p_suffix) + len(p_suffix)
            province = value[:idx]
            city_rest = value[idx:]
            if city_rest:
                return [province, city_rest]

    if len(value) >= 4:
        mid = len(value) // 2
        for i in range(mid - 1, mid + 2):
            if 0 < i < len(value):
                return [value[:i], value[i:]]

    return []


def _click_cascader_level(page: Any, target: str) -> bool:
    """Click a matching item in the currently visible cascader menu level."""
    target_norm = target.replace(" ", "").lower()

    menu_item_sels = [
        '.ant-cascader-menu-item-content',
        '.ant-cascader-menu-item',
    ]

    for sel in menu_item_sels:
        try:
            items = page.locator(f'{sel}:visible')
            count = items.count()
            for i in range(count):
                item_text = items.nth(i).inner_text()
                item_norm = item_text.replace(" ", "").lower()
                if target_norm == item_norm or target_norm in item_norm or item_norm in target_norm:
                    items.nth(i).click()
                    return True
        except Exception:
            continue

    try:
        input_el = page.locator('.ant-cascader-dropdown:not(.ant-cascader-dropdown-hidden) input, .ant-cascader input')
        if input_el.count() > 0:
            input_el.first.fill("")
            input_el.first.type(target, delay=50)
            page.wait_for_timeout(500)
            for sel in menu_item_sels:
                try:
                    filtered = page.locator(f'{sel}:visible')
                    if filtered.count() > 0:
                        filtered.first.click()
                        return True
                except Exception:
                    continue
    except Exception:
        pass

    return False


def _close_cascader(page: Any) -> None:
    try:
        page.locator("body").click()
    except Exception:
        pass


def fill_custom_dropdown(page: Any, field: dict, value: str) -> bool:
    uid = field["uid"]
    locator = page.locator(f'[data-resume-autofill-id="{uid}"]')

    try:
        locator.click(force=True)
        page.wait_for_timeout(350)
    except Exception as exc:
        print(f"  dropdown click failed: {exc}")
        return False

    norm = value.replace(" ", "").lower()

    option_selectors = [
        '.ant-select-item-option-content',
        '.ant-select-item',
        '.ant-cascader-menu-item-content',
        '.ant-cascader-menu-item',
        '.el-select-dropdown__item',
        '[role="option"]',
        '[role="menuitem"]',
    ]

    for sel in option_selectors:
        try:
            options = page.locator(f'{sel}:visible')
            count = options.count()
            for i in range(count):
                opt_text = options.nth(i).inner_text()
                opt_norm = opt_text.replace(" ", "").lower()
                if norm == opt_norm or norm in opt_norm or opt_norm in norm:
                    options.nth(i).click()
                    page.wait_for_timeout(200)
                    return True
        except Exception:
            continue

    try:
        input_el = locator.locator("input")
        if input_el.count() > 0:
            input_el.first.click()
            input_el.first.press("Control+a")
            input_el.first.press("Backspace")
            page.wait_for_timeout(100)
            input_el.first.type(value, delay=50)
            page.wait_for_timeout(500)

            for sel in option_selectors:
                try:
                    filtered = page.locator(f'{sel}:visible')
                    if filtered.count() > 0:
                        filtered.first.click()
                        page.wait_for_timeout(200)
                        return True
                except Exception:
                    continue
    except Exception:
        pass

    try:
        document_body = page.locator("body")
        document_body.click()
    except Exception:
        pass
    return False


def fill_radio_field(page: Any, field: dict, value: str) -> bool:
    uid = field["uid"]
    norm = value.replace(" ", "").lower()

    group_locator = page.locator(f'[data-resume-autofill-id="{uid}"]')

    # Strategy 1: Try clicking label elements (native HTML radio)
    for scope in [group_locator, group_locator.locator(".."), group_locator.locator("../..")]:
        try:
            labels = scope.locator("label")
            count = labels.count()
            for i in range(count):
                label_text = labels.nth(i).inner_text()
                label_norm = label_text.replace(" ", "").lower()
                if norm == label_norm or norm in label_norm or label_norm in norm:
                    try:
                        labels.nth(i).click(force=True, timeout=3000)
                        page.wait_for_timeout(200)
                        return True
                    except Exception as click_exc:
                        print(f"    click label failed: {click_exc}")
                        continue
        except Exception:
            continue

        # Strategy 2: Try native input[type="radio"]
        try:
            radios = scope.locator('input[type="radio"]')
            count = radios.count()
            for i in range(count):
                rv = radios.nth(i).get_attribute("value") or ""
                if norm == rv.replace(" ", "").lower():
                    try:
                        radios.nth(i).click(force=True, timeout=3000)
                        page.wait_for_timeout(200)
                        return True
                    except Exception as click_exc:
                        print(f"    click radio input failed: {click_exc}")
                        continue
        except Exception:
            continue

        # Strategy 3: Try ant-design radio buttons (.ant-radio-wrapper)
        try:
            wrappers = scope.locator(".ant-radio-wrapper, .ant-radio-wrapper-in-form-item")
            count = wrappers.count()
            for i in range(count):
                wrapper_text = wrappers.nth(i).inner_text()
                wrapper_norm = wrapper_text.replace(" ", "").lower()
                if norm == wrapper_norm or norm in wrapper_norm or wrapper_norm in norm:
                    try:
                        wrappers.nth(i).click(force=True, timeout=3000)
                        page.wait_for_timeout(200)
                        return True
                    except Exception as click_exc:
                        print(f"    click wrapper failed: {click_exc}")
                        continue
        except Exception:
            continue

        # Strategy 4: Try element-ui radio buttons (.el-radio)
        try:
            el_radios = scope.locator(".el-radio")
            count = el_radios.count()
            for i in range(count):
                radio_text = el_radios.nth(i).inner_text()
                radio_norm = radio_text.replace(" ", "").lower()
                if norm == radio_norm or norm in radio_norm or radio_norm in norm:
                    el_radios.nth(i).click()
                    page.wait_for_timeout(200)
                    return True
        except Exception:
            continue

    # Strategy 5: JS 直接设 input checked 属性（终极兜底）
    try:
        js_code = """
        ({ uid, targetNorm }) => {
          const container = document.querySelector(`[data-resume-autofill-id="${uid}"]`);
          if (!container) return false;
          const radios = container.querySelectorAll('input[type="radio"]');
          for (const radio of radios) {
            let labelText = '';
            const wrapper = radio.closest('label, .ant-radio-wrapper, .ant-radio-wrapper-in-form-item, .el-radio');
            if (wrapper) labelText = (wrapper.innerText || '').replace(/\s+/g, '').toLowerCase();
            const valNorm = (radio.value || '').replace(/\s+/g, '').toLowerCase();
            const truthyMap = { 'true': '是', 'yes': '是', 'y': '是', '1': '是' };
            const falsyMap = { 'false': '否', 'no': '否', 'n': '否', '0': '否' };
            const valAsText = truthyMap[valNorm] || falsyMap[valNorm] || valNorm;
            const valAsTextNorm = valAsText.replace(/\s+/g, '').toLowerCase();
            const matchLabel = labelText && (targetNorm === labelText || targetNorm.includes(labelText) || labelText.includes(targetNorm));
            const matchValue = valAsTextNorm && (targetNorm === valAsTextNorm || targetNorm.includes(valAsTextNorm) || valAsTextNorm.includes(targetNorm));
            if (matchLabel || matchValue) {
              radios.forEach(r => { r.checked = false; });
              radio.checked = true;
              radio.dispatchEvent(new Event('change', { bubbles: true }));
              radio.dispatchEvent(new Event('input', { bubbles: true }));
              return true;
            }
          }
          return false;
        }
        """
        js_result = page.evaluate(js_code, {"uid": uid, "targetNorm": norm})
        if js_result:
            page.wait_for_timeout(200)
            return True
        print(f"    Strategy 5 JS set checked also failed")
    except Exception as e5:
        print(f"    Strategy 5 JS error: {e5}")

    return False


def fill_checkbox_field(page: Any, field: dict, value: str) -> bool:
    truthy = {"1", "true", "yes", "y", "是", "同意", "有", "true"}
    uid = field["uid"]
    locator = page.locator(f'[data-resume-autofill-id="{uid}"]')
    norm = value.replace(" ", "").lower()

    should_check = norm in truthy or any(t in norm for t in truthy)
    if should_check:
        try:
            locator.click(force=True)
            page.wait_for_timeout(200)
            return True
        except Exception:
            return False
    return True


def fill_date_field(page: Any, field: dict, value: str) -> bool:
    uid = field["uid"]
    locator = page.locator(f'[data-resume-autofill-id="{uid}"]')

    normalized = _normalize_date(value)
    candidates = [normalized] if normalized else [value]
    if normalized and normalized != value:
        candidates.append(value)

    # Generate multiple format variations
    date_formats = []
    for date_val in candidates:
        date_formats.append(date_val)
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date_val)
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3)
            date_formats.append(f"{y}/{mo}/{d}")
            date_formats.append(f"{y}年{int(mo)}月{int(d)}日")
            date_formats.append(f"{y}{mo}{d}")

    for date_val in date_formats:
        # Strategy 1: Click + type with format detection
        if _try_fill_date_click_type(page, locator, uid, date_val):
            return True
        # Strategy 2: JavaScript dayjs-aware setter
        if _try_fill_date_js_direct(page, uid, date_val):
            return True
        # Strategy 3: React-safe native setter
        if _try_fill_date_react_setter(page, locator, uid, date_val):
            return True

    # Strategy 4: Calendar navigation (slowest, try with normalized value)
    for date_val in candidates:
        if _try_fill_date_calendar_nav(page, locator, date_val):
            return True

    return fill_text_field(page, field, value)


def _normalize_date(value: str) -> str:
    value = value.strip()
    m = re.match(r"(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})", value)
    if m:
        y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        return f"{y}-{mo}-{d}"
    return value


def _try_fill_date_click_type(page: Any, locator: Any, uid: str, value: str) -> bool:
    """Strategy: Click picker input, remove readonly, clear, type date value, confirm"""
    try:
        input_el = locator.locator("input")
        if input_el.count() == 0:
            return False
        target = input_el.first

        try:
            page.evaluate(f"""(() => {{
                const el = document.querySelector('[data-resume-autofill-id="{uid}"] input');
                if (el) {{
                    el.readOnly = false;
                    el.removeAttribute('readonly');
                }}
            }})()""")
        except Exception:
            pass

        target.click()
        page.wait_for_timeout(300)

        clear_btn = locator.locator('.ant-picker-clear, .ant-calendar-picker-clear')
        if clear_btn.count() > 0:
            try:
                clear_btn.first.click()
                page.wait_for_timeout(200)
            except Exception:
                pass

        target.press("Control+a")
        page.wait_for_timeout(100)
        target.press("Backspace")
        page.wait_for_timeout(100)

        target.type(value, delay=50)
        page.wait_for_timeout(400)

        # Check if value was accepted
        current_val = target.input_value()
        if current_val and current_val.strip():
            # Press Tab to confirm and close picker
            target.press("Tab")
            page.wait_for_timeout(200)
            return True

        # Try pressing Enter instead
        try:
            target.press("Enter")
            page.wait_for_timeout(300)
            current_val = target.input_value()
            if current_val and current_val.strip():
                return True
        except Exception:
            pass

        # Click body to close picker and trigger blur
        try:
            page.locator("body").click(position={"x": 1, "y": 1})
            page.wait_for_timeout(200)
            current_val = target.input_value()
            if current_val and current_val.strip():
                return True
        except Exception:
            pass

        return False
    except Exception:
        return False


def _try_fill_date_js_direct(page: Any, uid: str, value: str) -> bool:
    """Strategy: Use JavaScript to set date via React/dayjs internals"""
    try:
        result = page.evaluate(r"""
            ({ uid, value }) => {
              const container = document.querySelector(`[data-resume-autofill-id="${uid}"]`);
              if (!container) return false;
              const input = container.querySelector('input');
              if (!input) return false;

              // Parse the date value
              const dateMatch = value.match(/(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日]?/);
              if (!dateMatch) return false;

              const year = parseInt(dateMatch[1]);
              const month = parseInt(dateMatch[2]);
              const day = parseInt(dateMatch[3]);

              // Try to find dayjs from global scope or React internals
              const dayjs = window.dayjs || window.__dayjs;

              // Find React props on the picker wrapper
              const pickerWrapper = container.closest('.ant-picker') || container;
              const reactPropsKey = Object.keys(pickerWrapper).find(k => k.startsWith('__reactProps$'));
              const reactFiberKey = Object.keys(pickerWrapper).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));

              // Strategy A: Try to find onChange through React fiber tree
              if (reactFiberKey) {
                let fiber = pickerWrapper[reactFiberKey];
                for (let i = 0; i < 20 && fiber; i++, fiber = fiber.return) {
                  const props = fiber.memoizedProps || fiber.pendingProps;
                  if (props && typeof props.onChange === 'function') {
                    try {
                      let dateObj;
                      if (dayjs) {
                        dateObj = dayjs(new Date(year, month - 1, day));
                      } else {
                        dateObj = new Date(year, month - 1, day);
                      }
                      props.onChange(dateObj, null);
                      return true;
                    } catch(e) {}
                  }
                }
              }

              // Strategy B: Try direct React props onChange
              if (reactPropsKey) {
                const props = pickerWrapper[reactPropsKey];
                if (props && typeof props.onChange === 'function') {
                  try {
                    let dateObj;
                    if (dayjs) {
                      dateObj = dayjs(new Date(year, month - 1, day));
                    } else {
                      dateObj = new Date(year, month - 1, day);
                    }
                    props.onChange(dateObj, null);
                    return true;
                  } catch(e) {}
                }
              }

              // Strategy C: Remove readonly and set value via native setter + events
              input.readOnly = false;
              input.removeAttribute('readonly');

              const formattedValue = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
              const proto = HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              if (setter) setter.call(input, formattedValue);
              else input.value = formattedValue;

              input.dispatchEvent(new Event('focus', { bubbles: true }));
              input.dispatchEvent(new Event('input', { bubbles: true }));
              input.dispatchEvent(new Event('change', { bubbles: true }));
              input.dispatchEvent(new Event('blur', { bubbles: true }));

              return true;
            }
        """, {"uid": uid, "value": value})
        return bool(result)
    except Exception:
        return False



def _try_fill_date_react_setter(page: Any, locator: Any, uid: str, value: str) -> bool:
    """Strategy: React-safe native setter with readOnly removal"""
    try:
        result = page.evaluate(r"""
            ({ uid, value }) => {
              const container = document.querySelector(`[data-resume-autofill-id="${uid}"]`);
              if (!container) return false;
              const input = container.querySelector('input');
              if (!input) return false;
              input.readOnly = false;
              input.removeAttribute('readonly');
              input.focus();
              const proto = HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              if (setter) setter.call(input, value);
              else input.value = value;
              input.dispatchEvent(new Event('focus', { bubbles: true }));
              input.dispatchEvent(new Event('input', { bubbles: true }));
              input.dispatchEvent(new Event('change', { bubbles: true }));
              input.dispatchEvent(new Event('blur', { bubbles: true }));
              input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
              input.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', bubbles: true}));
              return true;
            }
        """, {"uid": uid, "value": value})
        return bool(result)
    except Exception:
        return False


def _try_fill_date_calendar_nav(page: Any, locator: Any, value: str) -> bool:
    """Strategy: Open calendar popup, navigate to correct year/month, click day"""
    try:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
        if not m:
            m = re.match(r"(\d{4})/(\d{2})/(\d{2})", value)
        if not m:
            return False

        target_year = int(m.group(1))
        target_month = int(m.group(2))
        target_day = int(m.group(3))

        locator.click(force=True)
        page.wait_for_timeout(500)

        year_month_display = page.locator(
            '.ant-picker-header-view:visible, '
            '.ant-calendar-header:visible, '
            '.ant-picker-panel:visible .ant-picker-header-view'
        )

        for _ in range(200):
            header_text = ""
            if year_month_display.count() > 0:
                header_text = year_month_display.first.inner_text()

            year_match = re.search(r"(\d{4})", header_text)
            month_match = re.search(r"(\d{1,2})[月]", header_text)

            current_year = int(year_match.group(1)) if year_match else None
            current_month = int(month_match.group(1)) if month_match else None

            if current_year == target_year and current_month == target_month:
                break

            if current_year is None or current_year > target_year:
                prev_btn = page.locator(
                    '.ant-picker-header-super-prev-btn:visible, '
                    '.ant-calendar-prev-year-btn:visible, '
                    'button[aria-label="Previous year"]:visible'
                )
                if prev_btn.count() > 0:
                    prev_btn.first.click()
                    page.wait_for_timeout(100)
                else:
                    break
            elif current_year < target_year:
                next_btn = page.locator(
                    '.ant-picker-header-super-next-btn:visible, '
                    '.ant-calendar-next-year-btn:visible, '
                    'button[aria-label="Next year"]:visible'
                )
                if next_btn.count() > 0:
                    next_btn.first.click()
                    page.wait_for_timeout(100)
                else:
                    break

            if current_year == target_year:
                if current_month is not None and current_month > target_month:
                    prev_btn = page.locator(
                        '.ant-picker-header-prev-btn:visible, '
                        '.ant-calendar-prev-month-btn:visible, '
                        'button[aria-label="Previous month"]:visible'
                    )
                    if prev_btn.count() > 0:
                        prev_btn.first.click()
                        page.wait_for_timeout(100)
                elif current_month is not None and current_month < target_month:
                    next_btn = page.locator(
                        '.ant-picker-header-next-btn:visible, '
                        '.ant-calendar-next-month-btn:visible, '
                        'button[aria-label="Next month"]:visible'
                    )
                    if next_btn.count() > 0:
                        next_btn.first.click()
                        page.wait_for_timeout(100)

        day_cells = page.locator(
            'td.ant-picker-cell:visible, '
            'td.ant-calendar-cell:visible, '
            'td[title]:visible'
        )

        day_str = str(target_day)
        clicked = False
        count = day_cells.count()
        for i in range(count):
            cell = day_cells.nth(i)
            cell_text = cell.inner_text().strip()
            title = cell.get_attribute("title") or ""
            if cell_text == day_str or day_str in title:
                aria_disabled = cell.get_attribute("aria-disabled")
                if aria_disabled == "true":
                    continue
                cell.click()
                clicked = True
                page.wait_for_timeout(300)
                break

        if not clicked:
            try:
                page.locator("body").click()
            except Exception:
                pass
            return False

        return True
    except Exception:
        return False


def fill_field(page: Any, field: dict, value: str) -> bool:
    """根据字段类型选择填写方式"""
    kind = classify_field(field)
    if kind == "text" or kind == "textarea":
        return fill_text_field(page, field, value)
    elif kind == "dropdown":
        if field.get("type") == "select":
            return fill_select_field(page, field, value)
        else:
            return fill_custom_dropdown(page, field, value)
    elif kind == "cascader":
        return fill_cascader_field(page, field, value)
    elif kind == "radio":
        return fill_radio_field(page, field, value)
    elif kind == "checkbox":
        return fill_checkbox_field(page, field, value)
    elif kind == "date":
        return fill_date_field(page, field, value)
    return False


# ─────────────────────────────────────────────────────────
# 浏览器和 Widget 管理
# ─────────────────────────────────────────────────────────

def _kill_stray_edge_processes(profile_dir: str):
    """Kill any msedge.exe processes that might be locking the profile dir."""
    import subprocess
    try:
        # Only kill msedge processes whose command line mentions our profile dir
        cmd = (
            'wmic process where "name=\'msedge.exe\'" get ProcessId,CommandLine '
            '/format:csv'
        )
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, shell=False
        ).stdout
        for line in out.splitlines():
            if profile_dir.replace("\\", "\\\\") in line or profile_dir in line:
                parts = line.split(",")
                if parts:
                    pid = parts[-1].strip()
                    if pid.isdigit():
                        try:
                            subprocess.run(
                                ["taskkill", "/F", "/PID", pid],
                                capture_output=True, timeout=5,
                            )
                            print(f"  killed stale Edge pid={pid}")
                        except Exception:
                            pass
    except Exception:
        pass


def launch_browser_context(p, args):
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
        "--window-size=1280,860",
    ]
    profile_dir = Path(args.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_str = str(profile_dir)

    # 先清理可能锁住 profile 的残留 Edge 进程
    _kill_stray_edge_processes(profile_str)

    print(f"Launching Edge with profile: {profile_dir}")
    try:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_str,
            channel="msedge",
            headless=args.headless,
            no_viewport=True,
            args=launch_args,
        )
        _inject_saved_cookies(context)
        return context, None
    except Exception as edge_exc:
        print(f"[warn] Edge 启动失败: {edge_exc}")
        print("[info] 尝试降级到 Playwright 自带 Chromium...")
        # 再次清理残留进程
        _kill_stray_edge_processes(profile_str)
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=profile_str,
                headless=args.headless,
                no_viewport=True,
                args=launch_args,
            )
            print("[info] 已降级到自带 Chromium（登录态可能需要重新登录）")
            _inject_saved_cookies(context)
            return context, None
        except Exception as chrom_exc:
            print(f"[error] Chromium 也启动失败: {chrom_exc}")
            raise


def _inject_saved_cookies(context) -> None:
    """启动后尝试注入之前保存的 cookies（作为 profile 持久化的备份）"""
    cookie_path = Path(__file__).parent / "saved_cookies.json"
    if not cookie_path.exists():
        return
    try:
        cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
        context.add_cookies(cookies)
        print(f"  [cookie] 已注入 {len(cookies)} 条保存的 cookies")
    except Exception as exc:
        print(f"  [cookie] 注入失败: {exc}")


def save_browser_cookies(context, out_dir: Path | None = None) -> int:
    """从 browser context 提取所有 cookies 保存到 JSON 文件。返回保存条数。"""
    out_dir = out_dir or Path(__file__).parent
    cookie_path = out_dir / "saved_cookies.json"
    try:
        all_cookies: list[dict] = []
        for page in context.pages:
            try:
                all_cookies.extend(context.cookies())
                break  # context.cookies() 已经是全局的，不用每个 page 单独取
            except Exception:
                continue
        # 去重（按 name+domain+path）
        seen = set()
        unique = []
        for c in all_cookies:
            key = (c.get("name"), c.get("domain"), c.get("path"))
            if key not in seen:
                seen.add(key)
                unique.append(c)
        cookie_path.write_text(
            json.dumps(unique, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[cookie] 已保存 {len(unique)} 条 cookies → {cookie_path}")
        return len(unique)
    except Exception as exc:
        print(f"[cookie] 保存失败: {exc}")
        return 0


def install_widget(page):
    try:
        page.evaluate(INSTALL_WIDGET_JS)
    except Exception:
        pass


def wait_for_widget_click(page):
    while True:
        install_widget(page)
        try:
            requested = page.evaluate("() => Boolean(window.__resumeAutofillRequested)")
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


def reset_widget_request(page):
    try:
        page.evaluate("() => { window.__resumeAutofillRequested = false; return true; }")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────
# 字段过滤
# ─────────────────────────────────────────────────────────

def is_field_filled(field: dict) -> bool:
    ft = field.get("type", "")
    val = str(field.get("value") or "").strip()
    text_val = str(field.get("text") or "").strip()
    selected = str(field.get("selected_text") or "").strip()

    if ft == "select" and selected:
        return True
    if ft in ("custom-dropdown",) and selected:
        return True
    if ft == "radio" and field.get("checked"):
        return True
    if ft == "checkbox" and field.get("checked"):
        return True
    if field.get("tag") == "textarea" and text_val:
        return True
    if val and val not in ("", " "):
        return True
    return False


def filter_empty_fields(fields: list[dict]) -> list[dict]:
    return [f for f in fields if not is_field_filled(f)]


# ─────────────────────────────────────────────────────────
# 展开隐藏区域（点击"添加"按钮）
# ─────────────────────────────────────────────────────────

def click_add_buttons_for_hidden_sections(page: Any) -> None:
    """Click '添加' buttons for sections that are display:none by default
    (实践经历, 个人荣誉, 社团活动). Must run BEFORE scanning so their fields exist in DOM."""
    try:
        clicked = page.evaluate("""() => {
            const sections = ['实践经历', '个人荣誉', '社团活动'];
            let count = 0;
            for (const name of sections) {
                const itemNames = document.querySelectorAll('.item-name');
                for (const el of itemNames) {
                    const directText = Array.from(el.childNodes)
                        .filter(n => n.nodeType === 3)
                        .map(n => n.textContent.trim())
                        .join('');
                    if (!directText.includes(name)) continue;

                    const container = el.closest('.item-box');
                    if (!container) continue;

                    const hiddenDiv = container.querySelector('div[style*="display: none"], div[style*="display:none"]');
                    if (!hiddenDiv) continue;

                    const addBtn = el.querySelector('.btn-add-zero, .btn-add')
                        || container.querySelector('.add-item .btn-add, .btn-add-zero');
                    if (addBtn) {
                        addBtn.click();
                        count++;
                    }
                    break;
                }
            }
            return count;
        }""")
        if clicked:
            print(f"[v2] Clicked {clicked} '添加' button(s) for hidden sections")
            page.wait_for_timeout(1000)
    except Exception as exc:
        print(f"[v2] Warning: click_add_buttons failed: {exc}")


# ─────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────

def run_autofill_v2(page, resume: dict, client, timeout_sec: float = 120) -> dict:
    """
    v2 填表主流程：
    1. 扫描页面字段（含下拉选项）
    2. 过滤已填字段
    3. 对每个空字段：分类 → 生成候选 → 匹配/答疑 → 填写
    """
    click_add_buttons_for_hidden_sections(page)

    print("\n[v2] Scanning form fields...")
    set_widget_status(page, "正在扫描页面字段...", button_text="扫描中...", tone="working")

    all_fields = []
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

    print(f"[v2] Found {len(all_fields)} total fields")

    empty_fields = filter_empty_fields(all_fields)
    print(f"[v2] {len(empty_fields)} empty fields to fill")
    set_widget_status(
        page, f"发现 {len(empty_fields)} 个空字段", button_text="开始填写", tone="ready"
    )
    page.wait_for_timeout(500)

    results = {"filled": [], "skipped": [], "failed": []}

    for i, field in enumerate(empty_fields):
        label = normalize_primary_label(field) or normalize_label(field)
        kind = classify_field(field)
        uid = field.get("uid", "")
        n_options = len(field.get("options", []))
        opt_info = f"options={n_options}" if n_options > 0 else ("options=deferred" if kind == "dropdown" else "")

        print(f"\n[{i+1}/{len(empty_fields)}] {label or '(unknown)'} "
              f"type={kind} {opt_info}".rstrip())

        candidates = get_candidates(field, resume, all_fields=empty_fields, field_index=i)
        print(f"  candidates: {candidates[:5]}")

        value = ""

        if kind in ("text", "textarea"):
            if len(candidates) == 1:
                value = candidates[0]
            elif len(candidates) > 1:
                value = ask_api_for_answer(client, field, candidates, resume, timeout_sec)
            if not value:
                print(f"  SKIP: no candidate")
                results["skipped"].append({"uid": uid, "label": label, "reason": "no_candidate"})
                continue

        elif kind == "dropdown":
            options = extract_dropdown_options(page, field)
            field["options"] = options
            option_texts = [o.get("text", "") for o in options]
            print(f"  extracted {len(options)} options on-demand")

            if not candidates and not options:
                print(f"  SKIP: no candidates and no options")
                results["skipped"].append({"uid": uid, "label": label, "reason": "no_data"})
                continue

            matched = ""
            if candidates:
                for c in candidates:
                    c_norm = c.replace(" ", "").lower()
                    for ot in option_texts:
                        ot_norm = ot.replace(" ", "").lower()
                        if c_norm == ot_norm or c_norm in ot_norm or ot_norm in c_norm:
                            matched = ot
                            break
                    if matched:
                        break

            if matched:
                value = matched
                print(f"  matched option: {value}")
            elif candidates and options:
                value = ask_api_for_option_pick(client, field, candidates, timeout_sec)
                if not value:
                    value = ask_api_for_answer(client, field, candidates, resume, timeout_sec)
            elif candidates:
                value = ask_api_for_answer(client, field, candidates, resume, timeout_sec)
            else:
                # No candidates but options exist - special handling for family/education sections
                section = field.get("section_header", "")
                is_family_section = any(kw in section for kw in ["家庭", "家庭成员", "家庭主要成员"]) or _is_family_field_by_dom(field)
                is_relationship_field = any(kw in label for kw in ["关系", "亲属关系"])
                
                if is_family_section and is_relationship_field and options:
                    # For family relationship fields, determine which row this is and pick from options
                    family_idx = _extract_family_index_from_name(field)
                    if family_idx is None:
                        family_idx = _determine_family_member_index(field, empty_fields, i, resume)
                    if family_idx is not None and resume:
                        members = resume.get("family_members", [])
                        if 0 <= family_idx < len(members):
                            target_rel = members[family_idx].get("relationship", "")
                            # Find matching option
                            for ot in option_texts:
                                if target_rel == ot or target_rel in ot or ot in target_rel:
                                    value = ot
                                    print(f"  matched by row index {family_idx}: {value}")
                                    break
                    
                    # If still no match, try API with context
                    if not value:
                        # Pass the field with section info to help API understand context
                        value = ask_api_for_answer(client, field, [], resume, timeout_sec)
                else:
                    value = ask_api_for_answer(client, field, [], resume, timeout_sec)

            if not value:
                print(f"  SKIP: no answer for dropdown")
                results["skipped"].append({"uid": uid, "label": label, "reason": "no_answer"})
                continue

        elif kind == "radio":
            if candidates:
                value = candidates[0]
            else:
                value = ask_api_for_answer(client, field, [], resume, timeout_sec)
            if not value:
                print(f"  SKIP: no answer for radio")
                results["skipped"].append({"uid": uid, "label": label, "reason": "no_answer"})
                continue

        elif kind == "checkbox":
            if candidates:
                value = candidates[0]
            else:
                value = "是"
            if not value:
                results["skipped"].append({"uid": uid, "label": label, "reason": "no_answer"})
                continue

        elif kind == "date":
            if candidates:
                value = candidates[0]
            else:
                print(f"  SKIP: no date value")
                results["skipped"].append({"uid": uid, "label": label, "reason": "no_date"})
                continue

        if not value:
            results["skipped"].append({"uid": uid, "label": label, "reason": "empty_value"})
            continue

        print(f"  → filling: {value[:60]}")
        set_widget_status(
            page, f"填写 {i+1}/{len(empty_fields)}: {label[:15]}", tone="working"
        )

        ok = fill_field(page, field, value)
        if ok:
            print(f"  OK")
            results["filled"].append({"uid": uid, "label": label, "value": value})
            # Store filled value in field dict for dependency tracking
            field["_filled_value"] = value
        else:
            print(f"  FAILED")
            results["failed"].append({"uid": uid, "label": label, "value": value})

        page.wait_for_timeout(300)

    print(f"\n{'='*50}")
    print(f"[v2] Done: {len(results['filled'])} filled, "
          f"{len(results['skipped'])} skipped, {len(results['failed'])} failed")
    set_widget_status(
        page,
        f"完成！已填 {len(results['filled'])} 项",
        enabled=True,
        button_text="填写完成",
        tone="done",
    )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume autofill v2 engine")
    parser.add_argument("--resume-json", type=str, default=str(DEFAULT_RESUME_JSON))
    parser.add_argument("--url", type=str, default="")
    parser.add_argument("--profile-dir", type=str, default=str(DEFAULT_PROFILE_DIR))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--fresh-profile", action="store_true")
    parser.add_argument("--doubao-timeout", type=float, default=120)
    parser.add_argument("--project-root", type=str, default=str(DEFAULT_PROJECT_ROOT))
    parser.add_argument("--yes", action="store_true", help="Auto-confirm (unused in v2)")
    parser.add_argument("--one-by-one", action="store_true", help="Unused in v2")
    parser.add_argument("--site-adapter", type=str, default="auto", help="Unused in v2")
    parser.add_argument("--max-fields-per-click", type=int, default=60, help="Unused in v2")
    parser.add_argument("--chunk-size", type=int, default=5, help="Unused in v2")
    parser.add_argument("--confidence", type=float, default=0.45, help="Unused in v2")
    args = parser.parse_args()

    resume_path = Path(args.resume_json)
    if not resume_path.is_file():
        print(f"Resume JSON not found: {resume_path}")
        return 1
    resume = load_json(resume_path)
    print(f"Loaded resume: {resume.get('name', '?')}")

    project_root = Path(args.project_root)
    client = load_doubao_client(project_root)
    print(f"Doubao client ready (model: {client.config.model})")

    with sync_playwright() as p:
        context, browser = launch_browser_context(p, args)
        try:
            page = context.new_page()
            if args.url:
                page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
                print(f"Navigated to: {args.url}")
            else:
                page.goto("about:blank")
                print("Opened blank page. Navigate to the form page in the browser.")

            print("\nWaiting for widget click...")
            print("Click '识别并填表 (v2)' button or press Ctrl+Shift+F to start.\n")
            wait_for_widget_click(page)
            reset_widget_request(page)

            run_autofill_v2(page, resume, client, timeout_sec=args.doubao_timeout)

            print("\nAutofill complete. Close the browser window to exit.")
            try:
                page.wait_for_event("close", timeout=0)
            except Exception:
                pass
        finally:
            try:
                context.close()
            except Exception:
                pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
