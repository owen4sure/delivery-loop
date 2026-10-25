#!/usr/bin/env python3
"""check_state.py 的測試。執行：python3 -B scripts/test_check_state.py"""
import datetime
import io
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import loop_hook
import run_loop
from check_state import assess, main, stop_verdict

TODAY = datetime.date(2026, 10, 25)

GOAL = """---
objective: 業務能在系統完成報價到訂單
boundary: local   # local / deploy-ready / prod
deadline: {deadline}
budget: 剩 30% 額度時收尾
confirmed: 2026-10-24
---
## 完成的定義
業務用自己的帳號走完報價到訂單。

## 里程碑
{milestones}
"""


def ticket(tid='T01', status='ready', extra='', acs='- AC1 [ui] 業務可以轉訂單', evidence='',
           title=None):
    return f"""---
id: {tid}
title: {title or '測試工作單 ' + tid}
status: {status}
milestone: M1
{extra}
---
## 要做什麼
說明

## 驗收標準
{acs}

## 證據
{evidence}
"""


PASS_UI = '- AC1 PASS level=ui rev=abc env=local artifact=evidence/a.png'
DONE = 'owner: alice\nreviewer: bob'


class Base(unittest.TestCase):
    def build(self, tickets, milestones='- M1 [open] 本機可用 (due 2026-10-28)', questions=None,
              artifacts=('evidence/a.png',), deadline='2026-10-30', goal=None, created=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        dl = root / '.done-done'
        (dl / 'tickets').mkdir(parents=True)
        text = goal if goal is not None else GOAL.format(milestones=milestones, deadline=deadline)
        (dl / 'GOAL.md').write_text(text, encoding='utf-8')
        for i, body in enumerate(tickets, 1):
            (dl / 'tickets' / f'{i:02d}.md').write_text(body, encoding='utf-8')
        if questions is not None:
            (dl / 'QUESTIONS.md').write_text(questions, encoding='utf-8')
        if created is not None:
            (dl / 'CREATED.md').write_text(created, encoding='utf-8')
        for rel in artifacts:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text('x')
        return root

    def run_assess(self, *args, today=TODAY, **kw):
        return assess(self.build(*args, **kw), today)

    def assertError(self, result, text):
        self.assertTrue(any(text in e for e in result['errors']), result['errors'])

    def assertClean(self, result):
        self.assertEqual(result['errors'], [])


class Structure(Base):
    def test_minimal_state_is_clean_and_actionable(self):
        r = self.run_assess([ticket()])
        self.assertClean(r)
        self.assertEqual(r['actionable'], ['T01'])

    def test_missing_id_is_readable_error_not_crash(self):
        r = self.run_assess([ticket().replace('id: T01\n', '')])
        self.assertError(r, '缺少 id')

    def test_full_width_colon_in_header(self):
        r = self.run_assess([ticket().replace('status: ready', 'status：ready')])
        self.assertClean(r)

    def test_missing_deadline(self):
        root = self.build([ticket()])
        goal = root / '.done-done' / 'GOAL.md'
        goal.write_text(goal.read_text(encoding='utf-8').replace('deadline: 2026-10-30\n', ''),
                        encoding='utf-8')
        self.assertError(assess(root, TODAY), 'deadline')

    def test_unknown_dependency_and_cycle(self):
        r = self.run_assess([ticket('T01', extra='depends_on: T02'),
                             ticket('T02', extra='depends_on: T01'),
                             ticket('T03', extra='depends_on: T09')])
        self.assertError(r, '依賴循環')
        self.assertError(r, 'T09 不存在')

    def test_doing_requires_owner_and_done_dependencies(self):
        r = self.run_assess([ticket('T01'), ticket('T02', 'doing', 'depends_on: T01')])
        self.assertError(r, 'T02：doing 必須填 owner')
        self.assertError(r, 'T01 還沒 done')

    def test_bad_acceptance_and_evidence_lines(self):
        r = self.run_assess([ticket(acs='- AC1 業務可以轉訂單', evidence='- AC1 通過了')])
        self.assertError(r, '驗收標準格式')
        self.assertError(r, '證據格式')

    def test_unconfirmed_goal_asks_user_first(self):
        root = self.build([ticket()])
        goal = root / '.done-done' / 'GOAL.md'
        goal.write_text(goal.read_text(encoding='utf-8').replace('confirmed: 2026-10-24\n', ''),
                        encoding='utf-8')
        r = assess(root, TODAY)
        self.assertClean(r)
        self.assertEqual(r['ask_user'][0]['id'], '確認')


class Extras(Base):
    def test_autopilot_values(self):
        root = self.build([ticket()])
        goal = root / '.done-done' / 'GOAL.md'
        goal.write_text(goal.read_text(encoding='utf-8').replace('budget:', 'autopilot: maybe\nbudget:'),
                        encoding='utf-8')
        self.assertError(assess(root, TODAY), 'autopilot')

    def test_learnings_size_warning(self):
        root = self.build([ticket()])
        (root / '.done-done' / 'LEARNINGS.md').write_text(
            '# 經驗\n' + ''.join(f'- 第 {i} 條\n' for i in range(61)), encoding='utf-8')
        r = assess(root, TODAY)
        self.assertTrue(any('LEARNINGS.md' in w for w in r['warnings']), r['warnings'])

    def test_parallel_batch_skips_conflicts_and_puts_risk_first(self):
        r = self.run_assess([ticket('T01', extra='resources: db:test@write'),
                             ticket('T02', extra='resources: db:test@write\nrisk: data'),
                             ticket('T03', extra='resources: file:/app/web@write')])
        self.assertEqual(r['batch'], ['T02', 'T03'])
        self.assertEqual(r['queued'], ['T01'])


class LegacyFolder(Base):
    def test_old_state_folder_name_still_works(self):
        root = self.build([ticket()])
        (root / '.done-done').rename(root / '.delivery-loop')
        r = assess(root, TODAY)
        self.assertClean(r)
        self.assertEqual(r['actionable'], ['T01'])
        self.assertEqual(loop_hook.find_loop(root).name, '.delivery-loop')


class Deadline(Base):
    def test_deadline_with_time(self):
        before = datetime.datetime(2026, 10, 27, 7, 0)
        after = datetime.datetime(2026, 10, 27, 7, 31)
        ok, _ = stop_verdict(self.run_assess([ticket()], deadline='2026-10-27 07:30', today=before))
        self.assertFalse(ok)
        ok, reasons = stop_verdict(self.run_assess([ticket()], deadline='2026-10-27 07:30', today=after))
        self.assertTrue(ok)
        self.assertIn('2026-10-27 07:30', reasons[0])

    def test_milestone_due_time_asks_for_report(self):
        r = self.run_assess([ticket()], milestones='- M1 [open] 本機可用 (due 2026-10-27 07:30)',
                            today=datetime.datetime(2026, 10, 27, 8, 0))
        self.assertTrue(any('現況報告' in w for w in r['warnings']), r['warnings'])


class Done(Base):
    def test_done_with_independent_review_and_evidence_is_clean(self):
        self.assertClean(self.run_assess([ticket(status='done', extra=DONE, evidence=PASS_UI)]))

    def test_author_cannot_review_own_work(self):
        r = self.run_assess([ticket(status='done', extra='owner: alice\nreviewer: alice',
                                    evidence=PASS_UI)])
        self.assertError(r, '非作者')

    def test_missing_reviewer_needs_explicit_exemption(self):
        r = self.run_assess([ticket(status='done', extra='owner: alice', evidence=PASS_UI)])
        self.assertError(r, 'review_exempt')
        r = self.run_assess([ticket(status='done', extra='owner: alice\nreview_exempt: 只改文案',
                                    evidence=PASS_UI)])
        self.assertClean(r)

    def test_data_risk_cannot_skip_review(self):
        r = self.run_assess([ticket(status='done', evidence=PASS_UI,
                                    extra='owner: alice\nrisk: data\nreview_exempt: 趕時間')])
        self.assertError(r, 'risk=data')

    def test_blind_qa_needs_reviewer_own_evidence(self):
        extra = DONE + '\nrisk: data\ncode_reviewer: carol'
        r = self.run_assess([ticket(status='done', extra=extra, evidence=PASS_UI)])
        self.assertError(r, 'by=bob')
        r = self.run_assess([ticket(status='done', extra=extra, evidence=PASS_UI + ' by=bob')])
        self.assertClean(r)
        r = self.run_assess([ticket(status='done', extra=DONE + '\nqa: blind', evidence=PASS_UI)])
        self.assertError(r, '驗收者自己重做')

    def test_data_risk_needs_code_review_by_someone_else(self):
        ev = PASS_UI + ' by=bob'
        r = self.run_assess([ticket(status='done', extra=DONE + '\nrisk: access', evidence=ev)])
        self.assertError(r, 'code_reviewer')
        r = self.run_assess([ticket(status='done', extra=DONE + '\nrisk: access\ncode_reviewer: alice',
                                    evidence=ev)])
        self.assertError(r, 'code_reviewer')

    def test_qa_field_values(self):
        self.assertError(self.run_assess([ticket(extra='qa: maybe')]), 'qa 只能')

    def test_lower_evidence_level_does_not_count(self):
        ev = '- AC1 PASS level=api rev=abc env=local artifact=evidence/a.png'
        r = self.run_assess([ticket(status='done', extra=DONE, evidence=ev)])
        self.assertError(r, '缺少 ui 等級以上')

    def test_ui_evidence_must_be_visual(self):
        ev = '- AC1 PASS level=ui rev=abc env=local artifact=evidence/review.md'
        r = self.run_assess([ticket(status='done', extra=DONE, evidence=ev)],
                            artifacts=('evidence/review.md',))
        self.assertError(r, '文字報告不算')
        ev = '- AC1 PASS level=ui rev=abc env=local artifact=evidence/shots'
        r = self.run_assess([ticket(status='done', extra=DONE, evidence=ev)],
                            artifacts=('evidence/shots/step1.png',))
        self.assertClean(r)

    def test_evidence_without_revision_does_not_count(self):
        ev = '- AC1 PASS level=ui env=local artifact=evidence/a.png'
        self.assertError(self.run_assess([ticket(status='done', extra=DONE, evidence=ev)]), 'rev')

    def test_fail_evidence_does_not_count(self):
        ev = '- AC1 FAIL level=ui rev=abc env=local artifact=evidence/a.png'
        self.assertError(self.run_assess([ticket(status='done', extra=DONE, evidence=ev)]), 'PASS')

    def test_artifact_must_exist_unless_url(self):
        ev = '- AC1 PASS level=ui rev=abc env=local artifact=evidence/missing.png'
        self.assertError(self.run_assess([ticket(status='done', extra=DONE, evidence=ev)]),
                         '證據檔不存在')
        ev = '- AC1 PASS level=ui rev=abc env=local artifact=https://ci.example/run/1'
        self.assertClean(self.run_assess([ticket(status='done', extra=DONE, evidence=ev)]))

    def test_evidence_for_unknown_criterion(self):
        ev = PASS_UI + '\n- AC9 PASS level=ui rev=abc env=local artifact=evidence/a.png'
        self.assertError(self.run_assess([ticket(status='done', extra=DONE, evidence=ev)]), 'AC9')

    def test_evidence_in_wrong_section_gets_hint(self):
        text = ticket(status='done', extra=DONE) + '\n## 本輪結論\n' + PASS_UI + '\n'
        self.assertError(self.run_assess([text]), '請移到「## 證據」')

    def test_dropped_needs_reason_and_user(self):
        self.assertError(self.run_assess([ticket(status='dropped')]), 'drop_reason')


QUESTIONS = """# 問題文件

## Q1 保證金比例
- 對象: 財務
- 狀態: {q1}
- 背景: 押標金要照比例計算，比例不對金額就會錯
- 轉傳內容: 財務你好，想確認押標金比例是否依標案金額分級？
- 你的回答:
- 影響: T01

## X1 公司主機連不到
- 狀態: open
- 證據: evidence/ssh-timeout.txt
- 恢復條件: 主機機房斷電修復
"""

USER_Q = """## Q2 開測試站
- 對象: 使用者
- 狀態: open
- 背景: 要實際存檔才能驗收，主站不能寫入
- 請回覆: 同意開本機測試站
- 你的回答: {answer}
"""


class Blockers(Base):
    def test_blocked_needs_open_question(self):
        r = self.run_assess([ticket(status='blocked')], questions=QUESTIONS.format(q1='open'))
        self.assertError(r, 'blocked_by')
        r = self.run_assess([ticket(status='blocked', extra='blocked_by: Q1')],
                            questions=QUESTIONS.format(q1='closed'))
        self.assertError(r, '改回 ready')

    def test_blocked_by_must_not_be_ticket(self):
        r = self.run_assess([ticket('T01'), ticket('T02', 'blocked', 'blocked_by: T01')],
                            questions=QUESTIONS.format(q1='open'))
        self.assertError(r, 'depends_on')

    def test_ready_ticket_with_open_blocker(self):
        r = self.run_assess([ticket(extra='blocked_by: Q1')], questions=QUESTIONS.format(q1='open'))
        self.assertError(r, '應為 blocked')

    def test_external_blocker_needs_evidence_and_resume_condition(self):
        q = '## X1 主機\n- 狀態: open\n'
        self.assertError(self.run_assess([ticket()], questions=q), '恢復條件')

    def test_user_liftable_limit_is_not_external(self):
        for cond in ('使用者另授權臨時測試網頁程序', '已授權環境實際有資源', '使用者同意後'):
            q = f'## X1 測試站沒有瀏覽入口\n- 狀態: open\n- 證據: evidence/a.png\n- 恢復條件: {cond}\n'
            r = self.run_assess([ticket(status='blocked', extra='blocked_by: X1')], questions=q)
            self.assertError(r, '不是外部阻礙')
        q = '## X1 測試站沒有瀏覽入口\n- 狀態: closed\n- 證據: evidence/a.png\n- 恢復條件: 使用者授權\n'
        self.assertClean(self.run_assess([ticket()], questions=q))

    def test_user_question_needs_reply_sentence(self):
        q = USER_Q.format(answer='').replace('- 請回覆: 同意開本機測試站\n', '')
        r = self.run_assess([ticket(status='blocked', extra='blocked_by: Q2')], questions=q)
        self.assertError(r, '請回覆')
        r = self.run_assess([ticket(status='blocked', extra='blocked_by: Q2')],
                            questions=USER_Q.format(answer=''))
        self.assertClean(r)
        self.assertEqual([x['id'] for x in r['ask_user']], ['Q2'])

    def test_question_needs_background_and_forward_text(self):
        q = '## Q1 比例\n- 對象: 財務\n- 狀態: open\n'
        r = self.run_assess([ticket()], questions=q)
        self.assertError(r, '背景')
        self.assertError(r, '轉傳內容')

    def test_answered_question_must_be_closed(self):
        r = self.run_assess([ticket()], questions=USER_Q.format(answer='同意'))
        self.assertError(r, '已經回答')
        r = self.run_assess([ticket()], questions=USER_Q.format(answer='（待填）'))
        self.assertClean(r)


class PlainLanguage(Base):
    def test_jargon_in_question_fields(self):
        q = USER_Q.format(answer='').replace('主站不能寫入', '主站 a9d34f2 的 gate 沒過')
        r = self.run_assess([ticket()], questions=q)
        self.assertError(r, 'a9d34f2')
        self.assertError(r, 'gate')

    def test_jargon_in_title_and_milestone(self):
        r = self.run_assess([ticket(title='原生甘特圖候選版驗收')],
                            milestones='- M1 [open] 修好 output/x.json 的問題 (due 2026-10-28)')
        self.assertError(r, 'T01 標題')
        self.assertError(r, 'M1 里程碑名稱')

    def test_common_words_are_allowed(self):
        r = self.run_assess([ticket(title='把報價修正 commit 後部署到測試站')])
        self.assertClean(r)


class Created(Base):
    def test_ledger_fields_and_cleanup_hint(self):
        c = '## C1 測試站\n- 種類: 測試站\n- 工作單: T09\n- 狀態: 用完了\n'
        r = self.run_assess([ticket()], created=c)
        self.assertError(r, '位置')
        self.assertError(r, '狀態只能')
        self.assertError(r, 'T09 不存在')
        c = '## C1 測試站\n- 種類: 測試站\n- 位置: test.localhost\n- 工作單: T01\n- 狀態: 使用中\n'
        r = self.run_assess([ticket(status='done', extra=DONE, evidence=PASS_UI)], created=c)
        self.assertClean(r)
        self.assertTrue(any('評估清理' in w for w in r['warnings']), r['warnings'])


class Resources(Base):
    def test_prefix_overlap_conflicts(self):
        r = self.run_assess([
            ticket('T01', 'doing', 'owner: a\nlease: held\nresources: db:site1@write'),
            ticket('T02', 'doing', 'owner: b\nlease: held\nresources: db:site1/table@write')])
        self.assertError(r, '資源衝突')

    def test_read_read_and_unrelated_names_do_not_conflict(self):
        r = self.run_assess([
            ticket('T01', 'doing', 'owner: a\nlease: held\nresources: db:site1@read'),
            ticket('T02', 'doing', 'owner: b\nlease: held\nresources: db:site1@read, db:site10@write')])
        self.assertClean(r)

    def test_held_lease_after_done(self):
        r = self.run_assess([ticket(status='done', evidence=PASS_UI,
                                    extra=DONE + '\nlease: held\nresources: db:site1@write')])
        self.assertError(r, '釋放')

    def test_relative_file_resource(self):
        r = self.run_assess([ticket(extra='resources: file:src/app@write')])
        self.assertError(r, '絕對路徑')

    def test_ready_ticket_waits_for_conflicting_lease(self):
        r = self.run_assess([
            ticket('T01', 'blocked', 'owner: a\nlease: held\nresources: db:site1@write\nblocked_by: X1'),
            ticket('T02', extra='resources: db:site1@write')], questions=QUESTIONS.format(q1='open'))
        self.assertClean(r)
        self.assertEqual(r['actionable'], [])


class Release(Base):
    def test_release_revision_must_have_integration_evidence(self):
        r = self.run_assess([ticket(status='done', extra=DONE, evidence=PASS_UI)],
                            milestones='- M1 [released rev=r999 env=local] 本機可用')
        self.assertError(r, 'rev=r999')
        r = self.run_assess([ticket(status='done', extra=DONE, evidence=PASS_UI)],
                            milestones='- M1 [released rev=abc env=local] 本機可用')
        self.assertClean(r)

    def test_production_release_needs_production_evidence(self):
        r = self.run_assess([ticket(status='done', extra=DONE, evidence=PASS_UI)],
                            milestones='- M1 [released rev=abc env=prod] 正式站')
        self.assertError(r, '正式站')

    def test_release_with_unfinished_ticket(self):
        r = self.run_assess([ticket('T01', 'done', DONE, evidence=PASS_UI), ticket('T02')],
                            milestones='- M1 [released rev=abc env=local] 本機可用')
        self.assertError(r, '未完成的工作單：T02')


class Stop(Base):
    def verdict(self, *args, **kw):
        return stop_verdict(self.run_assess(*args, **kw))

    def test_ready_work_means_continue(self):
        ok, reasons = self.verdict([ticket()])
        self.assertFalse(ok)
        self.assertIn('T01', reasons[0])

    def test_milestone_without_tickets_is_not_external_wait(self):
        ok, reasons = self.verdict([], milestones='- M1 [open] 本機可用')
        self.assertFalse(ok)
        self.assertIn('拆', reasons[0])

    def test_only_blockers_allows_stop(self):
        ok, reasons = self.verdict([ticket('T01', 'blocked', 'blocked_by: Q1'),
                                    ticket('T02', extra='depends_on: T01')],
                                   questions=QUESTIONS.format(q1='open'))
        self.assertTrue(ok, reasons)
        self.assertIn('Q1', '\n'.join(reasons))
        self.assertIn('請轉問財務', '\n'.join(reasons))

    def test_waiting_for_user_lists_reply_sentence_first(self):
        q = QUESTIONS.format(q1='open') + '\n' + USER_Q.format(answer='')
        ok, reasons = self.verdict([ticket('T01', 'blocked', 'blocked_by: Q2'),
                                    ticket('T02', 'blocked', 'blocked_by: Q1')], questions=q)
        self.assertTrue(ok, reasons)
        self.assertIn('等使用者回覆', reasons[0])
        self.assertIn('請回覆「同意開本機測試站」', reasons[1])
        self.assertIn('Q1', '\n'.join(reasons[2:]))

    def test_blocker_plus_independent_work_continues(self):
        ok, _ = self.verdict([ticket('T01', 'blocked', 'blocked_by: Q1'), ticket('T02')],
                             questions=QUESTIONS.format(q1='open'))
        self.assertFalse(ok)

    def test_review_pending_continues(self):
        ok, reasons = self.verdict([ticket(status='review', extra='owner: a')])
        self.assertFalse(ok)
        self.assertIn('驗收', reasons[0])

    def test_deadline_passed_allows_stop_with_report(self):
        ok, reasons = self.verdict([ticket()], today=datetime.date(2026, 10, 31))
        self.assertTrue(ok)
        self.assertIn('到期', reasons[0])

    def test_all_released_is_complete(self):
        ok, reasons = self.verdict([ticket(status='done', extra=DONE, evidence=PASS_UI)],
                                   milestones='- M1 [released rev=abc env=local] 本機可用')
        self.assertTrue(ok)
        self.assertIn('完成', reasons[0])

    def test_all_tickets_done_but_not_released(self):
        ok, reasons = self.verdict([ticket(status='done', extra=DONE, evidence=PASS_UI)])
        self.assertFalse(ok)
        self.assertIn('released', reasons[0])

    def test_state_errors_block_stop(self):
        ok, _ = self.verdict([ticket(status='weird')])
        self.assertFalse(ok)


class Templates(unittest.TestCase):
    def test_templates_pass_the_checker(self):
        skill = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp) / '.done-done'
            (dl / 'tickets').mkdir(parents=True)
            for name in ('GOAL.md', 'QUESTIONS.md', 'CREATED.md'):
                (dl / name).write_text((skill / 'templates' / name).read_text(encoding='utf-8'),
                                       encoding='utf-8')
            (dl / 'tickets' / '01-example.md').write_text(
                (skill / 'templates' / 'ticket.md').read_text(encoding='utf-8'), encoding='utf-8')
            r = assess(Path(tmp), TODAY)
        self.assertEqual(r['errors'], [])
        self.assertEqual(r['actionable'], ['T01'])
        self.assertTrue(r['goal'].get('integrator'))
        self.assertEqual([q['id'] for q in r['ask_user']], ['確認', 'Q1'])
        self.assertEqual([q['id'] for q in r['forward']], ['Q2'])


class Cli(Base):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(a) for a in argv])
        return code, out.getvalue() + err.getvalue()

    def test_missing_goal_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, text = self.run_main(tmp)
        self.assertEqual(code, 2)
        self.assertIn('GOAL.md', text)

    def test_exit_codes_for_stop(self):
        root = self.build([ticket()])
        self.assertEqual(self.run_main(root, '--today', '2026-10-25')[0], 0)
        code, text = self.run_main(root, '--stop', '--today', '2026-10-25')
        self.assertEqual(code, 1)
        self.assertIn('不行', text)

    def test_now_option(self):
        root = self.build([ticket()], deadline='2026-10-27 07:30')
        code, text = self.run_main(root, '--stop', '--now', '2026-10-27 08:00')
        self.assertEqual(code, 0)
        self.assertIn('到期', text)

    def test_output_shows_integrator_and_questions(self):
        q = QUESTIONS.format(q1='open') + '\n' + USER_Q.format(answer='')
        root = self.build([ticket(status='blocked', extra='blocked_by: Q2')], questions=q)
        goal = root / '.done-done' / 'GOAL.md'
        goal.write_text(goal.read_text(encoding='utf-8').replace(
            'budget:', 'integrator: 接續交付迴圈\nbudget:'), encoding='utf-8')
        code, text = self.run_main(root, '--today', '2026-10-25')
        self.assertEqual(code, 0, text)
        self.assertIn('整合者：接續交付迴圈', text)
        self.assertIn('需要你回覆（1 題）', text)
        self.assertIn('「同意開本機測試站」', text)
        self.assertIn('需要你轉問（1 題', text)



def autopilot(root, on=True):
    goal = root / '.done-done' / 'GOAL.md'
    goal.write_text(goal.read_text(encoding='utf-8').replace(
        'budget:', f"autopilot: {'on' if on else 'off'}\nbudget:"), encoding='utf-8')
    return root / '.done-done'


class StopGate(Base):
    def test_off_never_blocks(self):
        dl = autopilot(self.build([ticket()]), on=False)
        self.assertEqual(loop_hook.stop_decision(dl, TODAY), (False, ''))

    def test_blocks_while_work_remains_then_releases_when_stuck(self):
        dl = autopilot(self.build([ticket()]))
        block, reason = loop_hook.stop_decision(dl, TODAY)
        self.assertTrue(block)
        self.assertIn('T01', reason)
        self.assertTrue(loop_hook.stop_decision(dl, TODAY)[0])
        self.assertFalse(loop_hook.stop_decision(dl, TODAY)[0])
        self.assertIn('停止閘門 放行', (dl / 'LOG.md').read_text(encoding='utf-8'))

    def test_progress_resets_the_count(self):
        dl = autopilot(self.build([ticket()]))
        for i in range(4):
            self.assertTrue(loop_hook.stop_decision(dl, TODAY)[0])
            with (dl / 'LOG.md').open('a', encoding='utf-8') as f:
                f.write(f'進展 {i}\n')

    def test_allows_stop_when_only_waiting_for_people(self):
        dl = autopilot(self.build([ticket(status='blocked', extra='blocked_by: Q1')],
                                  questions=QUESTIONS.format(q1='open')))
        self.assertFalse(loop_hook.stop_decision(dl, TODAY)[0])

    def test_hook_entry_points(self):
        root = self.build([ticket()])
        autopilot(root)

        def call(event, cwd):
            out = io.StringIO()
            payload = io.StringIO('{"hook_event_name": "%s", "cwd": "%s"}' % (event, cwd))
            with mock.patch.object(sys, 'stdin', payload), redirect_stdout(out):
                self.assertEqual(loop_hook.main(), 0)
            return out.getvalue()

        self.assertIn('"decision": "block"', call('Stop', root / 'src'))
        self.assertIn('可以並行做 T01', call('SessionStart', root))
        with tempfile.TemporaryDirectory() as elsewhere:
            self.assertEqual(call('Stop', elsewhere), '')


FAKE_AGENT = """#!/usr/bin/env python3
import sys
from pathlib import Path
project = Path(sys.argv[sys.argv.index('-C') + 1])
if {progress}:
    with (project / '.done-done' / 'LOG.md').open('a', encoding='utf-8') as f:
        f.write('agent 做了一點事\\n')
"""


class RunLoop(Base):
    def fake(self, progress):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'agent'
        path.write_text(FAKE_AGENT.format(progress=progress), encoding='utf-8')
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def rounds_started(self, root):
        return (root / '.done-done' / 'LOG.md').read_text(encoding='utf-8').count('輪開始')

    def test_stops_right_away_when_allowed(self):
        root = self.build([ticket(status='done', extra=DONE, evidence=PASS_UI)],
                          milestones='- M1 [released rev=abc env=local] 本機可用')
        code = run_loop.run(root, 'codex', '/does/not/exist', 3, 2, 60, [], out=lambda *_: None)
        self.assertEqual(code, 0)

    def test_stops_after_two_rounds_without_progress(self):
        root = self.build([ticket()])
        code = run_loop.run(root, 'codex', self.fake(False), 5, 2, 60, [], out=lambda *_: None)
        self.assertEqual(code, 1)
        self.assertEqual(self.rounds_started(root), 2)

    def test_keeps_going_while_there_is_progress(self):
        root = self.build([ticket()])
        code = run_loop.run(root, 'codex', self.fake(True), 3, 2, 60, [], out=lambda *_: None)
        self.assertEqual(code, 1)
        self.assertEqual(self.rounds_started(root), 3)

    def test_command_shapes(self):
        self.assertEqual(run_loop.build_command('codex', 'codex', Path('/p'), 'go', ['-s', 'x']),
                         ['codex', 'exec', '-C', '/p', '-s', 'x', 'go'])
        self.assertEqual(run_loop.build_command('claude', 'claude', Path('/p'), 'go', []),
                         ['claude', '-p', 'go'])


if __name__ == '__main__':
    unittest.main()
