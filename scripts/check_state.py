#!/usr/bin/env python3
"""done-done 狀態檢查（唯讀）。

檢查專案 .done-done/ 的結構、依賴、資源鎖定、證據、問題文件、給人看的文字、
建立物清單、經驗檔、可並行的工作單與停止條件。通過只代表格式與規則一致，不代表程式正確、證據真實或動作已獲授權。

用法：
  check_state.py <專案目錄或 .done-done 目錄> [--stop] [--json]
                 [--today YYYY-MM-DD | --now "YYYY-MM-DD HH:MM"]

離開碼：0 通過（--stop 時代表可以停）；1 有錯誤或不能停；2 無法讀取狀態。
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path, PurePosixPath

STATUSES = ('ready', 'doing', 'review', 'done', 'blocked', 'dropped')
FINISHED = ('done', 'dropped')
LEVELS = {'build': 1, 'unit': 2, 'api': 3, 'ui': 4, 'user': 5, 'prod': 6}
RISKS = ('normal', 'data', 'access')
SECTIONS = {
    '驗收標準': 'acceptance', 'acceptance': 'acceptance',
    '證據': 'evidence', 'evidence': 'evidence',
    '里程碑': 'milestones', 'milestones': 'milestones',
}
OPEN_WORDS = {'open'}
CLOSED_WORDS = {'closed', 'answered', 'resolved'}
USER_WORDS = ('使用者', 'user')
STATE_DIRS = ('.done-done', '.delivery-loop')  # 第二個是舊的資料夾名稱，照樣讀得到
CREATED_STATES = ('使用中', '隔離中', '已清理', '保留')
LEARNINGS_MAX = 60
# ui 以上的證據要是看得到畫面的東西
VISUAL_EXT = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.mp4', '.webm', '.mov',
              '.zip', '.har', '.html', '.pdf')
# X 的恢復條件出現這些字，代表使用者就能解除，不是外部阻礙
NEEDS_USER_RE = re.compile(r'使用者|授權|同意|允許|核准|批准|放行|approv|authori[sz]', re.I)
# 「你的回答」裡只有這些佔位字，視為還沒回答
PLACEHOLDER_RE = re.compile(r'^[（(\[]?\s*(待填|請填|未回答|尚未回答|空白)?\s*[)）\]]?$')

# 給人看的文字不該出現的東西：版本號、檔案路徑、常見行話。擋不完，只是最低限度。
HASH_RE = re.compile(r'(?<![0-9A-Za-z])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![0-9A-Za-z])')
PATH_RE = re.compile(r'(?:^|[\s（(「])([~/][^\s，。、）)」]+)'
                     r'|\S+\.(?:py|js|ts|tsx|jsx|json|md|sh|ya?ml|toml|css|sql)(?![A-Za-z])')
JARGON_RE = re.compile(r'(?<![A-Za-z])(gate|payload|schema|runtime|fixture|controller|manifest|lease|'
                       r'DOM|IAB|PID|SHA|hash|diff|stdout|stderr)(?![A-Za-z])', re.I)
JARGON_ZH = ('原生', '同版', '候選', '冒稱', '投影', '閘門')

AC_RE = re.compile(r'^\s*-\s*(AC\d+)\s*\[(\w+)\]\s*\S')
EV_RE = re.compile(r'^\s*-\s*(AC\d+)\s+(PASS|FAIL|PARTIAL|SKIP)\b(.*)$')
MS_RE = re.compile(r'^\s*-\s*(M\d+)\s*\[(\w+)([^\]]*)\]\s*(.*)$')
QX_RE = re.compile(r'^##\s+([QX]\d+)\b')
CR_RE = re.compile(r'^##\s+(C\d+)\b')
KV_RE = re.compile(r'(\w+)=(\S+)')
WHEN_RE = re.compile(r'(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}):(\d{2}))?')


class StateError(Exception):
    """狀態無法讀取。"""


def split_field(line):
    """把「鍵: 值」或「鍵：值」切開；值裡的冒號保留。"""
    hits = [i for i in (line.find(':'), line.find('：')) if i >= 0]
    if not hits:
        return None
    i = min(hits)
    return line[:i].strip(), line[i + 1:].strip()


def split_list(value):
    return [x.strip() for x in (value or '').split(',') if x.strip()]


def parse_date(value):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def parse_when(text, anywhere=False):
    """「2026-10-30」或「2026-10-27 07:30」→ (datetime, 顯示文字)。只有日期時算到當天結束。"""
    m = (WHEN_RE.search if anywhere else WHEN_RE.match)((text or '').strip())
    if not m:
        return None, None
    day = parse_date(m.group(1))
    if not day:
        return None, None
    if m.group(2):
        hour, minute = int(m.group(2)), int(m.group(3))
        if hour > 23 or minute > 59:
            return None, None
        return datetime.datetime.combine(day, datetime.time(hour, minute)), m.group(0)
    return datetime.datetime.combine(day, datetime.time(23, 59, 59)), m.group(1)


def jargon(text):
    """給人看的文字裡找到的版本號、路徑、行話（最多三個）。"""
    hits = []
    for m in HASH_RE.finditer(text):
        hits.append(m.group(0))
    for m in PATH_RE.finditer(text):
        hits.append((m.group(1) or m.group(0)).strip())
    hits.extend(m.group(1) for m in JARGON_RE.finditer(text))
    hits.extend(w for w in JARGON_ZH if w in text)
    out = []
    for h in hits:
        if h not in out:
            out.append(h)
    return out[:3]


def lint_human(label, text, errors):
    hits = jargon(text or '')
    if hits:
        errors.append(f"{label}：給人看的文字出現版本號、路徑或行話（{'、'.join(hits)}），請改成白話")


def parse_front(text, name):
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        raise StateError(f'{name}：第一行必須是 ---')
    for end in range(1, len(lines)):
        if lines[end].strip() == '---':
            break
    else:
        raise StateError(f'{name}：標頭缺少結尾的 ---')
    fields = {}
    for raw in lines[1:end]:
        line = re.sub(r'\s+#.*$', '', raw)
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        kv = split_field(line)
        if not kv or not kv[0]:
            raise StateError(f'{name}：標頭這行看不懂：{raw.strip()}')
        fields[kv[0]] = kv[1]
    return fields, lines[end + 1:]


def sections(lines):
    out, current = {}, None
    for line in lines:
        m = re.match(r'^##\s+(.+?)\s*$', line)
        if m:
            current = SECTIONS.get(m.group(1).strip(), m.group(1).strip())
            out.setdefault(current, [])
        elif current:
            out[current].append(line)
    return out


def bullet_lines(lines):
    return [ln for ln in lines if ln.lstrip().startswith('-')]


def plain_text(lines):
    """去掉 HTML 註解後的段落文字。"""
    text = re.sub(r'<!--.*?-->', '', '\n'.join(lines), flags=re.S)
    return text.strip()


def load_goal(dl):
    path = dl / 'GOAL.md'
    if not path.is_file():
        raise StateError(f'找不到 {path}；先用 templates/GOAL.md 建立')
    fields, body = parse_front(path.read_text(encoding='utf-8'), 'GOAL.md')
    secs = sections(body)
    milestones, bad = {}, []
    for line in bullet_lines(secs.get('milestones', [])):
        m = MS_RE.match(line)
        if not m:
            bad.append(f'GOAL.md：里程碑格式應為「- M1 [open] 描述 (due 2026-10-30)」：{line.strip()}')
            continue
        due_m = re.search(r'due\s+(' + WHEN_RE.pattern + ')', m.group(4))
        due, due_text = parse_when(due_m.group(1)) if due_m else (None, None)
        milestones[m.group(1)] = {
            'id': m.group(1), 'status': m.group(2), 'desc': m.group(4).strip(),
            'due': due, 'due_text': due_text,
            **dict(KV_RE.findall(m.group(3))),
        }
    return fields, secs, milestones, bad


def load_blocks(path, head_re):
    """讀「## 編號 標題」加「- 鍵: 值」組成的清單（QUESTIONS.md、CREATED.md）。"""
    if not path.is_file():
        return {}
    items, current = {}, None
    for line in path.read_text(encoding='utf-8').splitlines():
        m = head_re.match(line)
        if m:
            current = {'id': m.group(1), 'kind': m.group(1)[0], 'title': line[2:].strip()}
            items[current['id']] = current
            continue
        if line.startswith('#'):
            current = None
            continue
        if current is not None:
            kv = split_field(re.sub(r'^\s*-\s*', '', line))
            if kv and kv[0]:
                current[kv[0]] = kv[1]
    return items


def load_ticket(path):
    fields, body = parse_front(path.read_text(encoding='utf-8'), path.name)
    secs = sections(body)
    ticket = dict(fields)
    ticket.update(_file=path.name, _acs=[], _ev=[], _bad=[], _ev_elsewhere={})
    for line in bullet_lines(secs.get('acceptance', [])):
        m = AC_RE.match(line)
        if m:
            ticket['_acs'].append((m.group(1), m.group(2)))
        else:
            ticket['_bad'].append(f'驗收標準格式應為「- AC1 [ui] 描述」：{line.strip()}')
    for line in bullet_lines(secs.get('evidence', [])):
        m = EV_RE.match(line)
        if m:
            ticket['_ev'].append({'ac': m.group(1), 'result': m.group(2), **dict(KV_RE.findall(m.group(3)))})
        else:
            ticket['_bad'].append(
                f'證據格式應為「- AC1 PASS level=ui rev=… env=… artifact=…」：{line.strip()}')
    for name, lines in secs.items():
        if name in ('acceptance', 'evidence'):
            continue
        for line in bullet_lines(lines):
            m = EV_RE.match(line)
            if m:
                ticket['_ev_elsewhere'].setdefault(m.group(1), name)
    return ticket


def is_user_question(q):
    return q['kind'] == 'Q' and any(w in q.get('對象', '').lower() for w in USER_WORDS)


def answered(q):
    return not PLACEHOLDER_RE.match(q.get('你的回答', '').strip())


def parse_resource(spec):
    """「db:site@write」→ (id, mode)。"""
    if '@' not in spec:
        return None
    rid, mode = spec.rsplit('@', 1)
    return rid.strip(), mode.strip()


def resources_overlap(a, b):
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return long_.startswith(short) and long_[len(short)] in '/:'


def check_goal(goal, secs, milestones, errors):
    if not milestones:
        errors.append('GOAL.md：至少要有一個里程碑')
    if goal.get('boundary') not in ('local', 'deploy-ready', 'prod'):
        errors.append('GOAL.md：boundary 必須是 local、deploy-ready 或 prod')
    if goal.get('autopilot', 'off') not in ('on', 'off', ''):
        errors.append('GOAL.md：autopilot 只能是 on 或 off')
    if goal.get('confirmed') and not parse_date(goal['confirmed']):
        errors.append('GOAL.md：confirmed 要寫使用者確認的日期（YYYY-MM-DD）')
    lint_human('GOAL.md 目標', goal.get('objective', ''), errors)
    for name in ('完成的定義', '我理解你要的'):
        lint_human(f'GOAL.md「{name}」', plain_text(secs.get(name, [])), errors)
    for ms in milestones.values():
        if ms['status'] not in ('open', 'released'):
            errors.append(f"{ms['id']}：里程碑狀態只能是 open 或 released")
        lint_human(f"{ms['id']} 里程碑名稱", re.sub(r'\(due[^)]*\)', '', ms['desc']), errors)


def check_questions(questions, errors):
    for q in questions.values():
        qid = f"QUESTIONS.md {q['id']}"
        status = q.get('狀態', '').lower()
        if status not in OPEN_WORDS | CLOSED_WORDS:
            errors.append(f'{qid}：狀態要寫 open 或 closed')
        q['open'] = status in OPEN_WORDS
        if q['kind'] == 'Q' and not q.get('對象'):
            errors.append(f'{qid}：缺少「對象」（要問誰）')
        if q['kind'] == 'X':
            if not q.get('證據') or not q.get('恢復條件'):
                errors.append(f'{qid}：外部阻礙要有「證據」和「恢復條件」')
            elif q['open'] and NEEDS_USER_RE.search(q['恢復條件']):
                errors.append(f'{qid}：恢復條件要靠使用者（授權、決定或親自操作），'
                              '這不是外部阻礙；改成 Q（對象: 使用者）並寫「請回覆」')
        if not q['open'] or q['kind'] != 'Q':
            continue
        if answered(q):
            errors.append(f'{qid}：使用者已經回答了；把答案寫進 GOAL.md 的「已確認的決定」，'
                          '改成 closed，被它擋住的工作單改回 ready')
        if not q.get('背景'):
            errors.append(f'{qid}：缺少「背景」：用白話寫為什麼要問、不問會怎樣')
        if is_user_question(q) and not q.get('請回覆'):
            errors.append(f'{qid}：問使用者的 Q 要寫「請回覆」：'
                          '使用者只要照抄回覆就能解除的那一句授權、決定或要做的事')
        if not is_user_question(q) and not q.get('轉傳內容'):
            errors.append(f'{qid}：要轉問別人的 Q 要寫「轉傳內容」：使用者可以直接複製傳給對方的完整訊息')
        lint_human(f'{qid} 標題', q['title'], errors)
        for key in ('背景', '問題', '建議', '請回覆', '轉傳內容'):
            lint_human(f'{qid}「{key}」', q.get(key, ''), errors)


def check_created(created, by_id, errors, warnings):
    for c in created.values():
        cid = f"CREATED.md {c['id']}"
        missing = [k for k in ('種類', '位置', '狀態') if not c.get(k)]
        if missing:
            errors.append(f"{cid}：缺少「{'、'.join(missing)}」")
        state = c.get('狀態', '')
        if state and state not in CREATED_STATES:
            errors.append(f"{cid}：狀態只能是 {'、'.join(CREATED_STATES)}")
        if state == '保留' and not c.get('理由'):
            errors.append(f'{cid}：狀態是保留，要寫「理由」')
        refs = split_list(c.get('工作單'))
        unknown = [r for r in refs if r not in by_id]
        if unknown:
            errors.append(f"{cid}：工作單 {'、'.join(unknown)} 不存在")
        if state == '使用中' and refs and not unknown and \
                all(by_id[r].get('status') in FINISHED for r in refs):
            warnings.append(f"{cid} 相關工作單都完成了：照 references/cleanup.md 評估清理")


def assess(target, today=None):
    """today 可以是日期（測試用，視為當天 00:00）或時間；省略時用現在時間。"""
    target = Path(target)
    dl = next((target / name for name in STATE_DIRS if (target / name).is_dir()), target)
    project_root = dl.parent
    if today is None:
        now = datetime.datetime.now()
    elif isinstance(today, datetime.datetime):
        now = today
    else:
        now = datetime.datetime.combine(today, datetime.time(0, 0))
    goal, goal_secs, milestones, errors = load_goal(dl)
    warnings = []
    questions = load_blocks(dl / 'QUESTIONS.md', QX_RE)
    created = load_blocks(dl / 'CREATED.md', CR_RE)

    deadline, deadline_text = parse_when(goal.get('deadline'))
    if not deadline:
        errors.append('GOAL.md：缺少 deadline（格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM）')
    check_goal(goal, goal_secs, milestones, errors)
    check_questions(questions, errors)

    tickets = []
    tickets_dir = dl / 'tickets'
    for path in sorted(tickets_dir.glob('*.md')) if tickets_dir.is_dir() else []:
        try:
            ticket = load_ticket(path)
        except StateError as exc:
            errors.append(str(exc))
            continue
        if not ticket.get('id'):
            errors.append(f'{path.name}：缺少 id')
            continue
        tickets.append(ticket)

    by_id = {}
    for t in tickets:
        if t['id'] in by_id:
            errors.append(f"{t['id']}：編號重複（{by_id[t['id']]['_file']}、{t['_file']}）")
        by_id[t['id']] = t

    for t in tickets:
        tid, status, owner = t['id'], t.get('status'), t.get('owner')
        errors.extend(f'{tid}：{msg}' for msg in t['_bad'])
        if status not in STATUSES:
            errors.append(f"{tid}：status 必須是 {'、'.join(STATUSES)} 之一")
        if t.get('milestone') not in milestones:
            errors.append(f'{tid}：milestone 不存在於 GOAL.md')
        risk = t.get('risk', 'normal') or 'normal'
        if risk not in RISKS:
            errors.append(f'{tid}：risk 必須是 normal、data 或 access')
        if t.get('qa') not in (None, '', 'blind'):
            errors.append(f'{tid}：qa 只能空白或寫 blind')
        lint_human(f'{tid} 標題', t.get('title', ''), errors)

        t['_deps'] = split_list(t.get('depends_on'))
        for dep in t['_deps']:
            if dep not in by_id:
                errors.append(f'{tid}：依賴的 {dep} 不存在')
            elif status in ('doing', 'review', 'done') and by_id[dep].get('status') != 'done':
                errors.append(f'{tid}：依賴的 {dep} 還沒 done，不能是 {status}')
            elif by_id[dep].get('status') == 'dropped' and status not in FINISHED:
                errors.append(f'{tid}：依賴的 {dep} 已放棄，需重新規劃')

        t['_blockers'] = split_list(t.get('blocked_by'))
        unknown = [b for b in t['_blockers'] if b not in questions]
        if unknown:
            errors.append(f"{tid}：blocked_by 只能放 QUESTIONS.md 的 Q／X 編號，找不到 {'、'.join(unknown)}"
                          '（工作單之間的依賴請用 depends_on）')
        open_blockers = [b for b in t['_blockers'] if questions.get(b, {}).get('open')]
        t['_open_blockers'] = open_blockers
        if status == 'blocked':
            if not t['_blockers']:
                errors.append(f'{tid}：blocked 必須在 blocked_by 寫明被哪個 Q／X 擋住')
            elif not open_blockers and not unknown:
                errors.append(f'{tid}：擋住它的 Q／X 都已關閉，請改回 ready')
        elif status in ('ready', 'doing', 'review') and open_blockers:
            errors.append(f"{tid}：仍被 {'、'.join(open_blockers)} 擋住，狀態應為 blocked")

        if status in ('doing', 'review') and not owner:
            errors.append(f'{tid}：{status} 必須填 owner')

        t['_res'] = []
        for spec in split_list(t.get('resources')):
            parsed = parse_resource(spec)
            if not parsed or parsed[1] not in ('read', 'write') or not parsed[0]:
                errors.append(f'{tid}：資源格式應為「名稱@read」或「名稱@write」：{spec}')
                continue
            if parsed[0].startswith('file:'):
                p = PurePosixPath(parsed[0][5:])
                if not p.is_absolute() or '..' in p.parts:
                    errors.append(f'{tid}：檔案資源要用絕對路徑：{spec}')
            t['_res'].append(parsed)
        lease = t.get('lease', 'released') or 'released'
        if lease not in ('held', 'released'):
            errors.append(f'{tid}：lease 只能是 held 或 released')
        t['_held'] = lease == 'held'
        if t['_held'] and status in ('ready', 'done', 'dropped'):
            errors.append(f'{tid}：狀態是 {status} 卻仍鎖定資源，交接時要釋放（lease: released）')
        if t['_held'] and not t['_res']:
            errors.append(f'{tid}：lease 是 held 但沒有列出資源')

        known_acs = {ac for ac, _ in t['_acs']}
        for ev in t['_ev']:
            if ev['ac'] not in known_acs:
                errors.append(f"{tid}：證據對應的 {ev['ac']} 不在驗收標準裡")
        for ac, level in t['_acs']:
            if level not in LEVELS:
                errors.append(f"{tid} {ac}：證據等級 [{level}] 不存在，可用 {'、'.join(LEVELS)}")

        if status == 'done':
            check_done(t, risk, project_root, errors)
        if status == 'dropped' and (not t.get('drop_reason') or not t.get('dropped_by')):
            errors.append(f'{tid}：dropped 要寫 drop_reason 和 dropped_by（必須是使用者）')

    check_cycles(by_id, errors)
    check_created(created, by_id, errors, warnings)
    learnings = dl / 'LEARNINGS.md'
    if learnings.is_file():
        count = len(bullet_lines(learnings.read_text(encoding='utf-8').splitlines()))
        if count > LEARNINGS_MAX:
            warnings.append(f'LEARNINGS.md 有 {count} 條：合併整理到 {LEARNINGS_MAX} 條以內，只留之後還用得到的')

    held = [t for t in tickets if t['_held']]
    for i, a in enumerate(held):
        for b in held[i + 1:]:
            if any_conflict(a, b):
                errors.append(f"資源衝突：{a['id']} 與 {b['id']} 同時鎖定重疊資源且至少一方寫入")

    for q in questions.values():
        if q['open'] and q['kind'] == 'X' and not any(q['id'] in t['_blockers'] for t in tickets):
            warnings.append(f"QUESTIONS.md {q['id']} 是 open，但沒有工作單被它擋住")

    for ms in milestones.values():
        ms_tickets = [t for t in tickets if t.get('milestone') == ms['id']]
        ms['_tickets'] = ms_tickets
        if ms['status'] == 'released':
            check_release(ms, ms_tickets, errors)
        elif not ms_tickets:
            warnings.append(f"{ms['id']} 還沒有任何工作單")
        elif ms['due'] and now > ms['due']:
            warnings.append(f"{ms['id']} 已到預定時間 {ms['due_text']}：先交這個里程碑的現況報告，再繼續做")

    actionable = [t['id'] for t in tickets if t.get('status') == 'ready'
                  and all(by_id.get(d, {}).get('status') == 'done' for d in t['_deps'])
                  and not t['_open_blockers']
                  and not any(any_conflict(t, h) for h in held if h is not t)]
    batch, queued = parallel_batch(actionable, by_id)
    ask_user = [q for q in questions.values() if q['open'] and is_user_question(q)]
    if not goal.get('confirmed'):
        ask_user.insert(0, {'id': '確認', 'title': '確認目標：讀 GOAL.md 的「我理解你要的」',
                            '請回覆': '對（或寫下要修改的地方）'})
    forward = [q for q in questions.values() if q['open'] and q['kind'] == 'Q' and not is_user_question(q)]
    return {
        'dir': str(dl), 'now': now, 'deadline': deadline, 'deadline_text': deadline_text, 'goal': goal,
        'milestones': milestones, 'questions': questions, 'tickets': tickets, 'by_id': by_id,
        'created': created, 'errors': errors, 'warnings': warnings, 'actionable': actionable,
        'ask_user': ask_user, 'forward': forward, 'batch': batch, 'queued': queued,
        'autopilot': goal.get('autopilot') == 'on',
        'review': [t['id'] for t in tickets if t.get('status') == 'review'],
        'doing': [t['id'] for t in tickets if t.get('status') == 'doing'],
    }


def any_conflict(a, b):
    """兩張工作單宣告的資源是否重疊，且至少一方寫入。"""
    for ra, ma in a['_res']:
        for rb, mb in b['_res']:
            if (ma == 'write' or mb == 'write') and resources_overlap(ra, rb):
                return True
    return False


def parallel_batch(actionable, by_id):
    """可以做的工作單裡，挑出彼此資源不衝突、能同時開工的一批；風險高的優先。"""
    order = sorted(actionable, key=lambda tid: by_id[tid].get('risk', 'normal') in ('', 'normal'))
    batch, queued = [], []
    for tid in order:
        clash = [b for b in batch if any_conflict(by_id[tid], by_id[b])]
        (queued if clash else batch).append(tid)
    return batch, queued


def check_done(t, risk, project_root, errors):
    tid, owner, reviewer = t['id'], t.get('owner'), t.get('reviewer')
    blind = risk in ('data', 'access') or t.get('qa') == 'blind'
    if not t['_acs']:
        errors.append(f'{tid}：done 但沒有驗收標準')
    for ac, need in t['_acs']:
        if need not in LEVELS:
            continue
        good = [ev for ev in t['_ev'] if ev['ac'] == ac and ev['result'] == 'PASS'
                and LEVELS.get(ev.get('level'), 0) >= LEVELS[need]
                and ev.get('rev') and ev.get('env') and ev.get('artifact')]
        if not good:
            where = t['_ev_elsewhere'].get(ac)
            hint = f'；在「{where}」段找到 {ac} 的證據行，請移到「## 證據」' if where else ''
            errors.append(f'{tid} {ac}：缺少 {need} 等級以上的 PASS 證據（要有 level、rev、env、artifact）{hint}')
            continue
        if LEVELS[need] >= LEVELS['ui']:
            visual = [ev for ev in good if is_visual(ev['artifact'], project_root)]
            if not visual:
                errors.append(f"{tid} {ac}：{need} 等級的證據要是截圖、錄影或瀏覽器紀錄，"
                              f"文字報告不算：{good[0]['artifact']}")
                continue
            good = visual
        if not any(artifact_exists(ev['artifact'], project_root) for ev in good):
            errors.append(f"{tid} {ac}：證據檔不存在：{good[0]['artifact']}")
            continue
        if blind and not any(ev.get('by') and ev['by'] == reviewer and ev['by'] != owner for ev in good):
            errors.append(f"{tid} {ac}：要有驗收者自己重做的證據（證據行加 by={reviewer or '驗收者名稱'}）")
    independent = bool(reviewer) and reviewer != owner
    if risk in ('data', 'access'):
        code_reviewer = t.get('code_reviewer')
        if not code_reviewer or code_reviewer == owner:
            errors.append(f'{tid}：risk={risk} 要有程式碼審查（code_reviewer 填看過改動的人，不能是作者）')
    if risk in ('data', 'access') and not independent:
        errors.append(f'{tid}：risk={risk} 一定要由非作者審查（reviewer 不能空白或等於 owner）')
    elif not independent and not t.get('review_exempt'):
        errors.append(f'{tid}：done 要有非作者的 reviewer；真的不需要審查就寫 review_exempt 和理由')


def resolve(artifact, project_root):
    path = Path(artifact)
    return path if path.is_absolute() else project_root / path


def artifact_exists(artifact, project_root):
    if re.match(r'^https?://', artifact):
        return True
    return resolve(artifact, project_root).exists()


def is_visual(artifact, project_root):
    """截圖、錄影、瀏覽器紀錄，或裝著這些檔案的資料夾；網址也算。"""
    if re.match(r'^https?://', artifact):
        return True
    path = resolve(artifact, project_root)
    if path.is_dir():
        return any(p.suffix.lower() in VISUAL_EXT for p in path.rglob('*'))
    return path.suffix.lower() in VISUAL_EXT


def check_release(ms, ms_tickets, errors):
    rev, env = ms.get('rev'), ms.get('env')
    if not rev:
        errors.append(f"{ms['id']}：released 必須寫 rev=（發布的版本）")
        return
    unfinished = [t['id'] for t in ms_tickets if t.get('status') not in FINISHED]
    if unfinished:
        errors.append(f"{ms['id']}：已 released 但還有未完成的工作單：{'、'.join(unfinished)}")
    proof = [ev for t in ms_tickets if t.get('status') == 'done' for ev in t['_ev']
             if ev['result'] == 'PASS' and ev.get('rev') == rev
             and (env != 'prod' or (ev.get('env') == 'prod' and ev.get('level') == 'prod'))]
    if not proof:
        need = '在正式站（env=prod level=prod）' if env == 'prod' else ''
        errors.append(f"{ms['id']}：已 released，但沒有任何工作單{need}在 rev={rev} 上有 PASS 證據；"
                      '發布的版本必須做過整合驗收')


def check_cycles(by_id, errors):
    state = {}

    def visit(tid, path):
        if state.get(tid) == 'done' or tid not in by_id:
            return
        if state.get(tid) == 'visiting':
            errors.append(f"依賴循環：{' → '.join(path + [tid])}")
            return
        state[tid] = 'visiting'
        for dep in by_id[tid]['_deps']:
            visit(dep, path + [tid])
        state[tid] = 'done'

    for tid in by_id:
        visit(tid, [])


def stop_verdict(r):
    """回傳 (能不能停, 理由清單)。只能在四種情況停：完成、到期、只剩阻礙（等使用者或外部）、使用者喊停。"""
    if r['errors']:
        return False, ['狀態有錯誤，先修正狀態再判斷']
    milestones, by_id = r['milestones'], r['by_id']
    if milestones and all(ms['status'] == 'released' for ms in milestones.values()):
        return True, ['完成：所有里程碑都已 released，交出交付報告']
    if r['deadline'] and r['now'] > r['deadline']:
        left = [t['id'] for t in r['tickets'] if t.get('status') not in FINISHED]
        return True, [f"到期：已過截止時間 {r['deadline_text']}，交出現況報告，不自行延長",
                      f"未完成：{'、'.join(left) or '無'}"]

    reasons = []
    if r['actionable']:
        reasons.append(f"還有可以做的工作單：{'、'.join(r['actionable'])}")
    if r['review']:
        reasons.append(f"有工作單等驗收：{'、'.join(r['review'])}")
    if r['doing']:
        reasons.append(f"有進行中的工作單：{'、'.join(r['doing'])}（做完或等它交接）")

    def externally_blocked(tid, seen=()):
        t = by_id.get(tid)
        if not t or tid in seen:
            return False
        if t.get('status') == 'blocked':
            return bool(t['_open_blockers'])
        if t.get('status') != 'ready':
            return False
        pending = [d for d in t['_deps'] if by_id.get(d, {}).get('status') != 'done']
        return bool(pending) and all(externally_blocked(d, seen + (tid,)) for d in pending)

    for ms in milestones.values():
        if ms['status'] == 'released':
            continue
        if not ms['_tickets']:
            reasons.append(f"{ms['id']} 還沒拆成工作單：先拆解，這不是外部阻礙")
            continue
        remaining = [t['id'] for t in ms['_tickets'] if t.get('status') not in FINISHED]
        if not remaining:
            reasons.append(f"{ms['id']} 的工作單都完成了但還沒 released：做整合驗收並發布")
            continue
        loose = [tid for tid in remaining if not externally_blocked(tid)
                 and tid not in r['actionable'] + r['review'] + r['doing']]
        if loose:
            reasons.append(f"這些工作單沒有被外部阻礙擋住：{'、'.join(loose)}")
    if reasons:
        return False, reasons

    lines = []
    if r['ask_user'] or r['forward']:
        lines.append('等使用者回覆或轉問：回報最後一行附上下面的內容')
        lines.extend(f"{q['title']}：請回覆「{q.get('請回覆', '')}」" for q in r['ask_user'])
        lines.extend(f"{q['title']}：請轉問{q.get('對象', '')}（轉傳內容在問題文件）" for q in r['forward'])
    waiting = [q for q in r['questions'].values() if q['open'] and q['kind'] == 'X'
               and any(q['id'] in t['_open_blockers'] for t in r['tickets'])]
    if waiting:
        lines.append('只能等的外部狀況：附上恢復條件')
        lines.extend(f"{q['title']}（{q.get('恢復條件', '')}）" for q in waiting)
    return True, lines or ['只剩阻礙：交出問題文件']


def render(r, verdict):
    out = [f"狀態檢查：{r['dir']}"]
    if r['deadline']:
        left = r['deadline'] - r['now']
        hours = left.total_seconds() / 3600
        if hours < 0:
            span = f'已過 {(-left).days} 天' if hours <= -48 else f'已過 {int(-hours)} 小時'
        else:
            span = f'還有 {left.days} 天' if hours >= 48 else f'還有 {int(hours)} 小時'
        out.append(f"截止時間：{r['deadline_text']}（{span}）")
    if r['goal'].get('integrator'):
        out.append(f"整合者：{r['goal']['integrator']}（新指示傳給這個 session）")
    out.append(f"自動模式：{'開（還不能停時，停止閘門會讓它繼續做）' if r['autopilot'] else '關'}")
    for ms in r['milestones'].values():
        tickets = ms.get('_tickets', [])
        done = sum(1 for t in tickets if t.get('status') == 'done')
        due = f"，預定 {ms['due_text']}" if ms['due_text'] else ''
        out.append(f"{ms['id']} [{ms['status']}] {ms['desc']}：{done}／{len(tickets)} 張完成{due}")
    blocked = [f"{t['id']}（{'、'.join(t['_open_blockers'])}）"
               for t in r['tickets'] if t.get('status') == 'blocked']
    for label, items in (('這一波可以並行', r['batch']), ('要排隊（資源衝突）', r['queued']),
                         ('驗收中', r['review']), ('進行中', r['doing']), ('卡住', blocked)):
        if items:
            out.append(f"{label}：{'、'.join(items)}")
    if r['ask_user']:
        out.append(f"需要你回覆（{len(r['ask_user'])} 題）：")
        out.extend(f"  - {q['title']}：「{q.get('請回覆', '')}」" for q in r['ask_user'])
    if r['forward']:
        out.append(f"需要你轉問（{len(r['forward'])} 題，轉傳內容在問題文件）：")
        out.extend(f"  - {q['title']}（{q.get('對象', '')}）" for q in r['forward'])
    in_use = [c['id'] for c in r['created'].values() if c.get('狀態') in ('使用中', '隔離中')]
    if in_use:
        out.append(f"建立中的測試物：{'、'.join(in_use)}（見 CREATED.md）")
    if r['errors']:
        out.append(f"錯誤（{len(r['errors'])}）：")
        out.extend(f'  - {e}' for e in r['errors'])
    if r['warnings']:
        out.append(f"提醒（{len(r['warnings'])}）：")
        out.extend(f'  - {w}' for w in r['warnings'])
    if verdict:
        ok, reasons = verdict
        out.append(f"可以停嗎：{'可以' if ok else '不行，回到迴圈'}")
        out.extend(f'  - {x}' for x in reasons)
    out.append('註：只檢查結構與格式，不代表程式正確、證據真實或已獲授權。')
    return '\n'.join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description='done-done 狀態檢查（唯讀）')
    parser.add_argument('target', type=Path, help='專案目錄或 .done-done 目錄')
    parser.add_argument('--stop', action='store_true', help='判斷現在能不能停')
    parser.add_argument('--json', action='store_true', help='輸出 JSON')
    parser.add_argument('--today', type=datetime.date.fromisoformat, help='測試用：指定今天日期')
    parser.add_argument('--now', help='測試用：指定現在時間，格式 "YYYY-MM-DD HH:MM"')
    args = parser.parse_args(argv)
    when = args.today
    if args.now:
        when, _ = parse_when(args.now)
        if not when:
            parser.error('--now 格式應為 "YYYY-MM-DD HH:MM"')
    try:
        r = assess(args.target, when)
    except (OSError, UnicodeDecodeError, StateError) as exc:
        print(f'無法讀取狀態：{exc}', file=sys.stderr)
        return 2
    verdict = stop_verdict(r) if args.stop else None
    if args.json:
        print(json.dumps({
            'errors': r['errors'], 'warnings': r['warnings'], 'actionable': r['actionable'],
            'review': r['review'], 'doing': r['doing'], 'batch': r['batch'], 'queued': r['queued'],
            'autopilot': r['autopilot'], 'integrator': r['goal'].get('integrator'),
            'confirmed': bool(r['goal'].get('confirmed')),
            'ask_user': [{'id': q['id'], 'title': q['title'], 'reply': q.get('請回覆', '')}
                         for q in r['ask_user']],
            'forward': [{'id': q['id'], 'title': q['title'], 'to': q.get('對象', '')}
                        for q in r['forward']],
            'stop': None if verdict is None else {'allowed': verdict[0], 'reasons': verdict[1]},
        }, ensure_ascii=False, indent=2))
    else:
        print(render(r, verdict))
    if r['errors'] or (verdict is not None and not verdict[0]):
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
