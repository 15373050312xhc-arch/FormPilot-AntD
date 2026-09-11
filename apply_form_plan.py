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
    const parentPicker = el.closest('.ant-picker, .el-date-editor');
    if (parentPicker) {
      parentPicker.setAttribute('data-resume-autofill-id', uid);
      return {
        ok: true,
        tag: parentPicker.tagName.toLowerCase(),
        className: String(parentPicker.className || '').slice(0, 80),
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
    '.ant-form-item, .ant-select, .ant-picker, .ant-radio-group, .el-select, .el-date-editor, .el-radio-group, .ant-cascader, .el-cascader'
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

  // 遍历所有可能的 input/textarea/select/容器
  const selectors = [
    'input:not([type=hidden]):not([type=button]):not([type=submit]):not([type=reset])',
    'textarea',
    'select',
    '.ant-select',
    '.ant-picker',
    '.ant-radio-group',
    '.ant-cascader',
    '.el-select',
    '.el-date-editor',
    '.el-radio-group',
    '.el-cascader',
    '[contenteditable="true"]',
  ];

  const all = Array.from(document.querySelectorAll(selectors.join(',')));
  // 排除嵌套（同一容器内的子 input 跳过，只保留容器）
  const visible = all.filter(el => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
  }).filter(el => {
    // 跳过被容器包裹的子 input（如果存在父容器也在候选里）
    return !el.closest('.ant-select, .ant-picker, .ant-radio-group, .ant-cascader, .el-select, .el-date-editor, .el-radio-group, .el-cascader')
                    || ['TEXTAREA', 'SELECT', 'INPUT'].indexOf(el.tagName) === -1;
  });

  for (const el of visible) {
    // 在祖先链里找 label 文本匹配
    let probe = el;
    for (let i = 0; probe && i < 6; i++, probe = probe.parentElement) {
      // label[for=] 模式
      if (el.id) {
        const lb = probe.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (lb && norm(text(lb)).includes(targetLabel)) {
          el.setAttribute('data-resume-autofill-id', uid);
          return { ok: true, tag: el.tagName.toLowerCase(), className: String(el.className || '').slice(0, 80) };
        }
      }
      // .ant-form-item-label 等
      const labelEl = probe.querySelector('.ant-form-item-label, .el-form-item__label, .layui-form-label, label');
      if (labelEl) {
        const lt = norm(text(labelEl));
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
          el.setAttribute('data-resume-autofill-id', uid);
          return { ok: true, tag: el.tagName.toLowerCase(), className: String(el.className || '').slice(0, 80) };
        }
      }
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
                page.locator("body").click()  # 关掉可能开着的下拉
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
        # 关掉残留的弹层
        try:
            page.locator("body").click()
            page.wait_for_timeout(80)
        except Exception:
            pass

    # 统计
    n_ok = len(results["filled"])
    n_skip = len(results["skipped"])
    n_fail = len(results["failed"])
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
