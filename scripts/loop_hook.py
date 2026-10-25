#!/usr/bin/env python3
"""done-done 的 hook，Codex 和 Claude Code 共用。

- Stop（停止閘門）：GOAL.md 的 autopilot 是 on、而檢查程式說還不能停時，擋下停止，
  讓 agent 回到迴圈繼續做。狀態連續兩次沒變化就放行，避免空轉燒額度。
- SessionStart：新 session、恢復或壓縮 context 後，提醒 agent 這個專案用 done-done，
  目前該做什麼、該讀哪些檔。

不在 done-done 專案裡時什麼都不做，所以裝在使用者層級也安全。
安裝方式見 references/automation.md。
"""
import datetime
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_state import STATE_DIRS, StateError, assess, stop_verdict  # noqa: E402

MAX_SAME = 2  # 狀態沒變化時，最多連續擋幾次


def find_loop(start):
    path = Path(start or '.').resolve()
    for d in (path, *path.parents):
        for name in STATE_DIRS:
            if (d / name / 'GOAL.md').is_file():
                return d / name
    return None


def fingerprint(dl):
    """狀態檔內容的指紋；有進展（工作單、問題、紀錄有改）指紋就會變。"""
    h = hashlib.sha256()
    for f in sorted(dl.rglob('*.md')):
        h.update(str(f.relative_to(dl)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def load_assessment(dl, now=None):
    try:
        return assess(dl, now)
    except (OSError, UnicodeDecodeError, StateError):
        return None


def stop_decision(dl, now=None):
    """回傳 (要不要擋, 理由)。"""
    r = load_assessment(dl, now)
    if r is None or not r['autopilot']:
        return False, ''
    gate = dl / '.stop_gate.json'
    ok, reasons = stop_verdict(r)
    if ok:
        gate.unlink(missing_ok=True)
        return False, ''
    fp = fingerprint(dl)
    try:
        prev = json.loads(gate.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        prev = {}
    count = prev.get('count', 0) + 1 if prev.get('fingerprint') == fp else 1
    if count > MAX_SAME:
        gate.unlink(missing_ok=True)
        with (dl / 'LOG.md').open('a', encoding='utf-8') as log:
            log.write(f"{datetime.date.today()} 停止閘門 放行：狀態連續 {MAX_SAME} 次沒有變化，交給使用者判斷\n")
        return False, ''
    gate.write_text(json.dumps({'fingerprint': fp, 'count': count}), encoding='utf-8')
    lines = ['done-done 自動模式：現在還不能停。'] + [f'- {x}' for x in reasons[:5]]
    lines.append('回到迴圈繼續做。真的需要使用者，先把問題寫進 QUESTIONS.md（問使用者的要附「請回覆」），'
                 '再跑一次檢查；使用者說要停，就把 GOAL.md 的 autopilot 改成 off。')
    return True, '\n'.join(lines)


def session_context(dl, now=None):
    r = load_assessment(dl, now)
    if r is None:
        return f'這個專案用 done-done（{dl}），但狀態檔讀不到；先跑 check_state.py 修好。'
    goal = r['goal']
    lines = [f"這個專案用 done-done（{dl}）。整合者：{goal.get('integrator') or '未填'}；"
             f"自動模式：{'開' if r['autopilot'] else '關'}。",
             '接手前先讀 GOAL.md、QUESTIONS.md、LEARNINGS.md、LOG.md 最後 30 行，並跑 check_state.py。']
    now_parts = []
    if r['batch']:
        now_parts.append(f"可以並行做 {'、'.join(r['batch'])}")
    if r['ask_user'] or r['forward']:
        now_parts.append(f"等使用者 {len(r['ask_user'])} 題、要轉問 {len(r['forward'])} 題")
    if r['errors']:
        now_parts.append(f"狀態有 {len(r['errors'])} 個錯誤要先修")
    if now_parts:
        lines.append('目前：' + '；'.join(now_parts) + '。')
    return '\n'.join(lines)


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    dl = find_loop(payload.get('cwd'))
    if dl is None:
        return 0
    event = payload.get('hook_event_name', '')
    if event == 'Stop':
        block, reason = stop_decision(dl)
        if block:
            print(json.dumps({'decision': 'block', 'reason': reason}, ensure_ascii=False))
    elif event == 'SessionStart':
        print(session_context(dl))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
