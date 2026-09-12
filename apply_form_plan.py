"""
apply_form_plan.py — 客户端排单模式 第 2 步：按计划填表

流程：
1. 读取 form_plan.json（用户从 LLM 客户端拿回来保存的答案）
2. 启动浏览器（复用 v2 的 profile 保留登录态）
3. 用户登录并进入表单页（或保持上次没关的页面）
4. 点击右下角"按计划填表"按钮
5. 工具逐条执行：
   a. 根据 anchor_id 找到目标元素
   b. 给元素打上临时 data-resume-autofill-id
   c. 按 kind 分发到 v2 的 fill_text_field / fill_select_field / fill_custom_dropdown /
      fill_radio_field / fill_cascader_field / fill_date_field / fill_checkbox_field
   d. 记录成功/失败/跳过
6. 打印统计，等用户人工复核后提交

关键设计：
- anchor_id 优先用 DOM id（绝大多数 ant-form-item 的 id 都很规整）
- 没有 id 时回退到 name/label/section 组合
- radio/select 用 pick_label 按显示文本匹配点击，不靠业务 value
- 不调用任何 LLM API
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
DEFAULT_PLAN_PATH = SCRIPT_DIR / "form_plan.json"

# 复用 v2 的 fill_* 函数族和工具
sys.path.insert(0, str(SCRIPT_DIR))
from form_filler_v2 import (  # noqa: E402
    SET_WIDGET_STATUS_JS,
    launch_browser_context,
    fill_text_field,
    fill_select_field,
    fill_custom_dropdown,
    fill_radio_field,
    fill_cascader_field,
    fill_date_field,
    fill_checkbox_field,
)


# ─────────────────────────────────────────────────────────
# Widget：执行填表按钮
# ─────────────────────────────────────────────────────────

INSTALL_APPLY_WIDGET_JS = r"""
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
  button.textContent = '按计划填表';
  button.style.cssText = [
    'height:48px', 'padding:0 18px', 'border:0', 'border-radius:10px',
    'background:#059669', 'color:#fff', 'font-size:16px', 'font-weight:600',
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
    if (window.__resumeApplyRequested) return;
    window.__resumeApplyRequested = true;
    button.disabled = true;
    button.textContent = '执行中...';
    button.style.background = '#64748b';
    button.style.cursor = 'wait';
    status.textContent = '正在按计划填写';
  });

  document.addEventListener('keydown', (event) => {
    if (event.ctrlKey && event.shiftKey && String(event.key || '').toLowerCase() === 'p') {
      event.preventDefault();
      if (window.__resumeApplyRequested) return;
      window.__resumeApplyRequested = true;
      button.disabled = true;
      button.textContent = '执行中...';
      button.style.background = '#64748b';
      button.style.cursor = 'wait';
      status.textContent = '正在按计划填写';
    }
  }, true);

  box.appendChild(button);
  box.appendChild(status);
  document.documentElement.appendChild(box);
  return true;
}
"""


# ─────────────────────────────────────────────────────────
# JS：通过 anchor_id 给目标元素打临时 uid
# ─────────────────────────────────────────────────────────

STAMP_UID_BY_ID_JS = r"""
({ anchorId, uid }) => {
  if (!anchorId) return { ok: false, error: 'empty_anchor' };

  // 1. 直接按 id 查
  let el = document.getElementById(anchorId);
  // 2. 按 name 查
  if (!el) {
    el = document.querySelector(`[name="${CSS.escape(anchorId)}"]`);
  }
  // 3. 按 data-resume-autofill-id 查（dump 阶段留下的）
  if (!el) {
    el = document.querySelector(`[data-resume-autofill-id="${anchorId}"]`);
  }

  if (!el) return { ok: false, error: 'not_found' };

  const tag = el.tagName.toLowerCase();

  // 1. 如果本身就是 ant-select/ant-picker/ant-radio-group/ant-cascader 容器 → stamp 自己
  const selfIsContainer = el.classList && (
    el.classList.contains('ant-select') ||
    el.classList.contains('ant-picker') ||
    el.classList.contains('ant-radio-group') ||
    el.classList.contains('ant-cascader') ||
    el.classList.contains('ant-cascader-picker') ||
    el.classList.contains('ant-calendar-picker') ||
    el.classList.contains('el-select') ||
    el.classList.contains('el-date-editor') ||
    el.classList.contains('el-radio-group') ||
    el.classList.contains('el-cascader')
  );
  if (selfIsContainer) {
    el.setAttribute('data-resume-autofill-id', uid);
    return {
      ok: true,
      tag: tag,
      className: String(el.className || '').slice(0, 80),
      isContainer: true,
    };
  }

  // 2. 如果是 ant-select 内部的 input（.ant-select-selection-search-input 等）→ 向上找 .ant-select 容器
  if (tag === 'input') {
    const parentSelect = el.closest('.ant-select, .el-select');
    if (parentSelect) {
      parentSelect.setAttribute('data-resume-autofill-id', uid);
      return {
        ok: true,
        tag: parentSelect.tagName.toLowerCase(),
        className: String(parentSelect.className || '').slice(0, 80),
        isContainer: true,
      };
    }
    // ant-picker 内部 input → 向上找 .ant-picker
    const parentPicker = el.closest('.ant-picker, .ant-calendar-picker, .el-date-editor');
    if (parentPicker) {
      parentPicker.setAttribute('data-resume-autofill-id', uid);
      return {
        ok: true,
        tag: parentPicker.tagName.toLowerCase(),
        className: String(parentPicker.className || '').slice(0, 80),
        isContainer: true,
      };
    }
    // ant-cascader / el-cascader 内部 input → 向上找 .ant-cascader / .el-cascader ⚠️ 关键修复
    const parentCascader = el.closest('.ant-cascader, .ant-cascader-picker, .el-cascader');
    if (parentCascader) {
      parentCascader.setAttribute('data-resume-autofill-id', uid);
      return {
        ok: true,
        tag: parentCascader.tagName.toLowerCase(),
        className: String(parentCascader.className || '').slice(0, 80),
        isContainer: true,
      };
    }
    // 普通 input → stamp 自己（text 字段）
    el.setAttribute('data-resume-autofill-id', uid);
    return {
      ok: true,
      tag: tag,
      className: String(el.className || '').slice(0, 80),
      isContainer: false,
    };
  }

  // 3. 如果是 textarea/select → 直接 stamp 自己
  if (tag === 'textarea' || tag === 'select') {
    el.setAttribute('data-resume-autofill-id', uid);
    return {
      ok: true,
      tag: tag,
      className: String(el.className || '').slice(0, 80),
      isContainer: false,
    };
  }

  // 4. 其他：向上找容器 stamp
  const container = el.closest(
    '.ant-form-item, .ant-select, .ant-picker, .ant-calendar-picker, .ant-radio-group, .el-select, .el-date-editor, .el-radio-group, .ant-cascader, .ant-cascader-picker, .el-cascader'
  );
  const finalTarget = container || el;
  finalTarget.setAttribute('data-resume-autofill-id', uid);

  return {
    ok: true,
    tag: finalTarget.tagName.toLowerCase(),
    className: String(finalTarget.className || '').slice(0, 80),
    isContainer: finalTarget !== el,
  };
}
"""


STAMP_UID_BY_LABEL_JS = r"""
({ label, section, uid }) => {
  if (!label) return { ok: false, error: 'empty_label' };

  const text = (n) => (n && (n.innerText || n.textContent || '') || '').replace(/\s+/g, ' ').trim();
  const norm = (s) => s.replace(/[*＊：:\s（）()|_\-]/g, '');

  const targetLabel = norm(label);
  if (!targetLabel) return { ok: false, error: 'empty_label' };

  // ── 通用 label 文本提取（结构启发式，不依赖具体类名）──
  const getLabelText = (el) => {
    // 1. label[for=id] — HTML 标准
    if (el.id) {
      const lb = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lb) return text(lb);
    }
    // 2. aria-label — 无障碍标准
    const aria = el.getAttribute('aria-label');
    if (aria) return aria;
    // 3. aria-labelledby
    const lbBy = el.getAttribute('aria-labelledby');
    if (lbBy) {
      const n = document.getElementById(lbBy.split(/\s+/)[0]);
      if (n) return text(n);
    }
    // 4. 被 <label> 包裹
    const wrapped = el.closest('label');
    if (wrapped) return text(wrapped);
    // 5. 结构启发式：前一个兄弟节点（label 常在 input 左侧/上方，作为兄弟元素）
    let probe = el;
    for (let i = 0; probe && i < 6; i++, probe = probe.parentElement) {
      const prev = probe.previousElementSibling;
      if (prev) {
        const t = text(prev);
        const clean = t.replace(/[*＊：:\s]/g, '');
        // 只取短文本（1-20字），长文本多半是描述不是标签
        if (clean && clean.length >= 1 && clean.length <= 20) return t;
      }
    }
    // 6. 结构启发式：字段容器内第一个短文本节点（不依赖类名）
    const container = el.closest(
      'li, tr, .form-item, .form-group, .ant-form-item, .el-form-item, '
      + '[class*="field"], [class*="item"], [class*="form"]'
    );
    if (container) {
      const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
        acceptNode: (node) => {
          if (el.contains(node)) return NodeFilter.FILTER_REJECT;
          const t = node.textContent.replace(/\s+/g, ' ').trim();
          if (!t) return NodeFilter.FILTER_REJECT;
          if (t.length > 30) return NodeFilter.FILTER_REJECT; // 跳过描述性长文本
          return NodeFilter.FILTER_ACCEPT;
        }
      });
      const first = walker.nextNode();
      if (first) return first.textContent;
    }
    // 7. 类名兜底（Ant Design / Element UI 等已知框架）
    let p = el.parentElement;
    for (let i = 0; p && i < 6; i++, p = p.parentElement) {
      const ll = p.querySelector(
        '.ant-form-item-label, .el-form-item__label, .layui-form-label, '
        + '.aply-field-label, label'
      );
      if (ll) {
        const t = text(ll);
        if (t) return t;
      }
    }
    return '';
  };

  // 遍历所有可能的 input/textarea/select/容器
  const selectors = [
    'input:not([type=hidden]):not([type=button]):not([type=submit]):not([type=reset])',
    'textarea',
    'select',
    '.ant-select',
    '.ant-picker',
    '.ant-radio-group',
    '.ant-cascader',
    '.ant-cascader-picker',
    '.ant-calendar-picker',
    '.el-select',
    '.el-date-editor',
    '.el-radio-group',
    '.el-cascader',
    '[contenteditable="true"]',
  ];

  const all = Array.from(document.querySelectorAll(selectors.join(',')));
  const visible = all.filter(el => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
  }).filter(el => {
    const isControlContainer = (() => {
      try {
        return el.matches(
          '.ant-select, .ant-picker, .ant-calendar-picker, .ant-radio-group, .ant-cascader, .ant-cascader-picker, '
          + '.el-select, .el-date-editor, .el-radio-group, .el-cascader'
        );
      } catch(e) { return false; }
    })();
    if (isControlContainer) return true;

    if (['TEXTAREA', 'SELECT', 'INPUT'].indexOf(el.tagName) !== -1) {
      return !el.closest('.ant-select, .ant-picker, .ant-calendar-picker, .ant-radio-group, .ant-cascader, .ant-cascader-picker, .el-select, .el-date-editor, .el-radio-group, .el-cascader');
    }

    const hasInnerControl = el.querySelector(
      '.ant-select, .ant-picker, .ant-calendar-picker, .ant-radio-group, .ant-cascader, .ant-cascader-picker, '
      + '.el-select, .el-date-editor, .el-radio-group, .el-cascader'
    );
    if (hasInnerControl) return false;

    return true;
  });

  for (const el of visible) {
    const lt = norm(getLabelText(el));
    if (lt && lt.includes(targetLabel)) {
      // 校验 section（如果给了）
      if (section) {
        const targetSection = norm(section);
        let sectionProbe = el;
        let sectionMatch = false;
        for (let j = 0; sectionProbe && j < 12; j++, sectionProbe = sectionProbe.parentElement) {
          const itemName = sectionProbe.querySelector('.item-name, .section-name, h1, h2, h3, h4, h5, h6');
          if (itemName && norm(text(itemName)).includes(targetSection)) {
            sectionMatch = true;
            break;
          }
        }
        if (!sectionMatch) continue;
      }
      // 如果 el 是控件容器内部的 input，stamp 容器本身（cascader/date/select）
      const wrap = el.closest('.ant-select, .ant-picker, .ant-calendar-picker, .ant-cascader, .ant-cascader-picker, .el-select, .el-date-editor, .el-cascader');
      const target = wrap || el;
      target.setAttribute('data-resume-autofill-id', uid);
      return { ok: true, tag: target.tagName.toLowerCase(), className: String(target.className || '').slice(0, 80) };
    }
  }
  return { ok: false, error: 'label_not_found' };
}
"""


# ─────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────

def install_apply_widget(page):
    try:
        page.evaluate(INSTALL_APPLY_WIDGET_JS)
    except Exception:
        pass


def wait_for_apply_click(page):
    while True:
        install_apply_widget(page)
        try:
            requested = page.evaluate("() => Boolean(window.__resumeApplyRequested)")
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


def stamp_uid_by_anchor(page, fill: dict, uid: str) -> tuple[bool, str]:
    """根据 anchor 信息给目标元素打临时 uid。先按 id，失败再按 label+section。"""
    anchor = fill.get("anchor") or {}
    anchor_id = fill.get("anchor_id") or anchor.get("id") or anchor.get("name") or anchor.get("key")
    label = anchor.get("label") or fill.get("label") or ""
    section = anchor.get("section") or fill.get("section") or ""

    # 1. 按 id/name
    if anchor_id:
        try:
            result = page.evaluate(
                STAMP_UID_BY_ID_JS,
                {"anchorId": anchor_id, "uid": uid},
            )
            if isinstance(result, dict) and result.get("ok"):
                return True, f"id={anchor_id} tag={result.get('tag')} cls={result.get('className')}"
        except Exception as exc:
            print(f"  stamp by id failed: {exc}")

    # 2. 按 label + section 兜底
    # form_plan.json 可能只给了中文 anchor_id（如"证件类型"）没给 label，用 anchor_id 兜底
    if not label and anchor_id:
        label = anchor_id
    if label:
        try:
            result = page.evaluate(
                STAMP_UID_BY_LABEL_JS,
                {"label": label, "section": section, "uid": uid},
            )
            if isinstance(result, dict) and result.get("ok"):
                return True, f"label={label!r} section={section!r} tag={result.get('tag')}"
        except Exception as exc:
            print(f"  stamp by label failed: {exc}")

    return False, "anchor_not_found"


# ─────────────────────────────────────────────────────────
# JS：禁用文件上传组件（防止填表误触弹出选择框）
# ─────────────────────────────────────────────────────────

DISABLE_FILE_UPLOAD_JS = r"""
() => {
  const saved = [];
  // 1. 隐藏所有 input[type="file"]
  document.querySelectorAll('input[type="file"]').forEach((el, i) => {
    const key = `arf_file_disabled_${i}`;
    saved.push({ el, display: el.style.display, visibility: el.style.visibility, pe: el.style.pointerEvents });
    el.style.display = 'none';
    el.setAttribute('data-arf-hidden-by', 'resume-autofill');
  });
  // 2. 给常见上传容器加 pointer-events:none（整个上传区域也禁用点击）
  const uploadSelectors = [
    '.ant-upload', '.el-upload', '.upload', '.uploader',
    '[class*="upload"]', '[class*="Upload"]',
    '.file-upload', '.fileupload',
    'a[href*="upload"]',
  ];
  document.querySelectorAll(uploadSelectors.join(',')).forEach((el, i) => {
    if (el.getAttribute('data-arf-pe-disabled')) return;
    el.setAttribute('data-arf-pe-disabled', '1');
    const key = `arf_upload_pe_${i}`;
    if (!window.__arf_upload_saved) window.__arf_upload_saved = [];
    window.__arf_upload_saved.push({ el, pe: el.style.pointerEvents });
    el.style.pointerEvents = 'none';
  });
  return true;
}
"""

RESTORE_FILE_UPLOAD_JS = r"""
() => {
  document.querySelectorAll('input[type="file"][data-arf-hidden-by="resume-autofill"]').forEach(el => {
    el.removeAttribute('data-arf-hidden-by');
  });
  document.querySelectorAll('[data-arf-pe-disabled="1"]').forEach(el => {
    el.removeAttribute('data-arf-pe-disabled');
    el.style.pointerEvents = '';
  });
  return true;
}
"""

SAFE_BODY_CLICK_JS = r"""
() => {
  // 用 JS 原生 click 事件关掉下拉/弹层，不触发浏览器原生文件选择框
  document.body.click();
  // 同时按 Esc 也能关掉很多弹层
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  document.dispatchEvent(new KeyboardEvent('keyup', { key: 'Escape', bubbles: true }));
  return true;
}
"""


def _dispatch_fill(page: Any, kind: str, field: dict, value: str) -> bool:
    """按 kind 分发到对应的 fill_* 函数。"""
    if kind in ("text", "textarea"):
        return fill_text_field(page, field, value)
    if kind == "select":
        return fill_select_field(page, field, value)
    if kind == "dropdown":
        return fill_custom_dropdown(page, field, value)
    if kind == "radio":
        return fill_radio_field(page, field, value)
    if kind == "cascader":
        return fill_cascader_field(page, field, value)
    if kind == "date":
        return fill_date_field(page, field, value)
    if kind == "checkbox":
        return fill_checkbox_field(page, field, value)
    return False


def load_plan(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Plan file not found: {path}")
    raw = path.read_text(encoding="utf-8").strip()
    # 容错：去掉模型可能加的 ```json 围栏
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Plan root must be object, got {type(data).__name__}")
    fills = data.get("fills") or data.get("plan") or data.get("actions")
    if not isinstance(fills, list):
        raise ValueError("Plan must contain 'fills' array")
    data["fills"] = fills
    return data


# ─────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────

def apply_plan(page, plan: dict) -> dict:
    fills = plan.get("fills") or []
    print(f"\n[apply] 共 {len(fills)} 条 fill 计划")

    # 给所有 Playwright 操作设 8 秒超时，防止 fill 卡死不返回
    # （React 重渲染时元素可能暂时不可见，click 默认 30 秒会卡很久）
    try:
        page.set_default_timeout(8000)
    except Exception:
        pass

    # ⚠️ 关键防护：填表前禁用所有文件上传组件，防止 Playwright 真实 click 误触弹出选择框
    try:
        page.evaluate(DISABLE_FILE_UPLOAD_JS)
        print("[safety] 已禁用页面文件上传组件")
    except Exception as exc:
        print(f"[safety] 禁用上传组件失败（不影响主要流程）: {exc}")

    results = {"filled": [], "skipped": [], "failed": []}
    set_widget_status(
        page,
        f"准备执行 {len(fills)} 条填表",
        button_text="执行中",
        tone="working",
    )

    for i, fill in enumerate(fills, 1):
        anchor = fill.get("anchor") or {}
        anchor_id = fill.get("anchor_id") or anchor.get("id") or anchor.get("key") or "(unknown)"
        kind = fill.get("kind") or ""
        value = str(fill.get("value") or "")
        pick_label = str(fill.get("pick_label") or "")
        note = fill.get("note") or ""
        label_hint = anchor.get("label") or fill.get("label") or anchor_id

        # 决定要写入的最终 value
        if kind in ("select", "dropdown", "radio"):
            effective_value = pick_label or value
        elif kind == "checkbox":
            effective_value = value or "是"
        else:
            effective_value = value

        if not effective_value and kind != "checkbox":
            print(f"\n[{i}/{len(fills)}] {label_hint} kind={kind} → SKIP: empty value")
            results["skipped"].append({"anchor_id": anchor_id, "reason": "empty_value", "note": note})
            continue

        print(f"\n[{i}/{len(fills)}] {label_hint} kind={kind}")
        print(f"  value={effective_value[:80]!r}" + (f" note={note}" if note else ""))

        # 给目标元素打 uid
        uid = f"plan_{int(time.time()*1000)}_{i}"
        ok, where = stamp_uid_by_anchor(page, fill, uid)
        if not ok:
            print(f"  SKIP: anchor not found ({anchor_id})")
            results["skipped"].append({
                "anchor_id": anchor_id, "reason": "anchor_not_found", "note": note,
            })
            continue
        print(f"  stamp: {where}")

        # 构造 field dict 传给 v2 的 fill_* 函数
        field = {
            "uid": uid,
            "type": kind if kind != "dropdown" else "custom-dropdown",
            "tag": "select" if kind == "select" else "input",
            "options": [],  # 执行器不再用预存的 options，靠 fill_custom_dropdown 实时匹配
        }

        set_widget_status(
            page,
            f"填写 {i}/{len(fills)}: {label_hint[:15]}",
            tone="working",
        )

        # 分发
        if kind not in ("text", "textarea", "select", "dropdown", "radio", "cascader", "date", "checkbox"):
            print(f"  SKIP: unknown kind={kind!r}")
            results["skipped"].append({
                "anchor_id": anchor_id, "reason": f"unknown_kind:{kind}", "note": note,
            })
            continue

        try:
            success = _dispatch_fill(page, kind, field, effective_value)
        except Exception as exc:
            print(f"  FAILED with exception: {exc}")
            results["failed"].append({
                "anchor_id": anchor_id, "value": effective_value, "error": str(exc), "note": note,
            })
            try:
                page.evaluate(SAFE_BODY_CLICK_JS)
                page.wait_for_timeout(150)
            except Exception:
                pass
            continue

        # React 重新渲染可能导致 data-resume-autofill-id 丢失，重新 stamp + 重试一次
        if not success:
            uid_retry = f"plan_r_{int(time.time()*1000)}_{i}"
            ok_retry, where_retry = stamp_uid_by_anchor(page, fill, uid_retry)
            if ok_retry:
                field["uid"] = uid_retry
                print(f"  retry stamp: {where_retry}")
                try:
                    success = _dispatch_fill(page, kind, field, effective_value)
                except Exception:
                    pass

        if success:
            print(f"  OK")
            results["filled"].append({
                "anchor_id": anchor_id, "kind": kind, "value": effective_value, "note": note,
            })
        else:
            print(f"  FAILED")
            results["failed"].append({
                "anchor_id": anchor_id, "kind": kind, "value": effective_value, "note": note,
            })

        page.wait_for_timeout(250)
        # 关掉残留的弹层（用 JS 版本，避免误触文件上传）
        try:
            page.evaluate(SAFE_BODY_CLICK_JS)
            page.wait_for_timeout(80)
        except Exception:
            pass

    # 统计
    n_ok = len(results["filled"])
    n_skip = len(results["skipped"])
    n_fail = len(results["failed"])

    # 恢复文件上传组件（填表完了）
    try:
        page.evaluate(RESTORE_FILE_UPLOAD_JS)
        print("[safety] 已恢复页面文件上传组件")
    except Exception:
        pass
    print()
    print("=" * 60)
    print(f"[apply] 完成：{n_ok} 成功 / {n_skip} 跳过 / {n_fail} 失败")
    set_widget_status(
        page,
        f"完成！{n_ok} 成功 / {n_skip} 跳过 / {n_fail} 失败",
        enabled=True,
        button_text="可关闭",
        tone="done" if n_fail == 0 else ("working" if n_fail > 0 else "done"),
    )
    return results


# ─────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a client-side LLM-generated form plan")
    parser.add_argument("--plan", type=str, default=str(DEFAULT_PLAN_PATH),
                        help="Path to form_plan.json (from LLM)")
    parser.add_argument("--url", type=str, default="",
                        help="Target form URL (skip to use the page already open)")
    parser.add_argument("--profile-dir", type=str, default=str(DEFAULT_PROFILE_DIR))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--fresh-profile", action="store_true")
    args = parser.parse_args()

    plan_path = Path(args.plan).resolve()
    try:
        plan = load_plan(plan_path)
    except Exception as exc:
        print(f"无法读取计划文件：{exc}")
        print(f"路径：{plan_path}")
        input("按回车退出...")
        return 1

    class Args:
        pass
    launch_args = Args()
    launch_args.profile_dir = args.profile_dir
    launch_args.headless = args.headless
    launch_args.fresh_profile = args.fresh_profile

    print("Apply Form Plan — 客户端排单模式第 2 步")
    print(f"计划文件：{plan_path}")
    print(f"共 {len(plan.get('fills', []))} 条 fill")
    print()
    print("保持此窗口打开。浏览器打开后：")
    print("  1. 登录并进入表单页（与 dump 时同一页面）")
    print("  2. 点击页面左上角绿色按钮：按计划填表")
    print("     （或按 Ctrl+Shift+P 触发）")
    print()

    with sync_playwright() as p:
        context, _ = launch_browser_context(p, launch_args)
        page = context.pages[0] if context.pages else context.new_page()

        if args.url:
            page.goto(args.url)

        install_apply_widget(page)
        print("等待点击 按计划填表 按钮...")
        wait_for_apply_click(page)

        try:
            apply_plan(page, plan)
        except Exception as exc:
            print(f"\n执行失败：{exc}")
            import traceback
            traceback.print_exc()
            input("按回车退出...")
            return 1

        print()
        print("提示：填写完成后请人工复核，再手动提交表单。")
        input("按回车退出...")
        # 退出前自动保存 cookies（登录态）
        from form_filler_v2 import save_browser_cookies
        save_browser_cookies(context)
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
