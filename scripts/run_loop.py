#!/usr/bin/env python3
"""自動續跑：一輪開一個全新的 agent session，直到檢查程式說可以停。

每一輪都是乾淨的 context，只靠 .done-done/ 的檔案接續，所以可以長時間跑又不會越跑越慢。
連續兩輪狀態都沒變化、到了輪數上限、或過了截止時間，就停下來。
跑這支程式的期間，不要同時在別的 session 當整合者。

用法：
  run_loop.py <專案> [--tool codex|claude] [--rounds 5] [--per-round 3] [--bin 路徑]
              [--timeout 秒數] [-- 傳給 agent 的其他參數]

例：
  run_loop.py ~/work/booking --tool codex -- --sandbox workspace-write
  run_loop.py ~/work/booking --tool claude -- --permission-mode acceptEdits

離開碼：0 檢查程式說可以停（完成、到期或在等使用者）；1 空轉或到輪數上限；2 讀不到狀態或找不到 agent。
"""
import argparse
import datetime
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_state import StateError, assess, stop_verdict  # noqa: E402
from loop_hook import find_loop, fingerprint  # noqa: E402

PROMPT = ('使用 done-done 接續這個專案（{project}）。先讀 {state}/ 的 GOAL.md、QUESTIONS.md、'
          'LEARNINGS.md 和 LOG.md 最後 30 行，照迴圈做，這一輪你是整合者。每完成一張工作單就更新狀態檔；'
          '檢查程式說可以停，或這一輪已經完成 {per_round} 張工作單，就把狀態寫回檔案後結束。')
CODEX_FALLBACKS = ('/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex',
                   '/Applications/Codex.app/Contents/Resources/codex')
STALL_ROUNDS = 2


def find_binary(tool, given):
    if given:
        return given
    found = shutil.which(tool)
    if found:
        return found
    if tool == 'codex':
        for path in CODEX_FALLBACKS:
            if Path(path).exists():
                return path
    return None


def build_command(tool, binary, project, prompt, extra):
    if tool == 'codex':
        return [binary, 'exec', '-C', str(project), *extra, prompt]
    return [binary, '-p', prompt, *extra]


def log(dl, text):
    with (dl / 'LOG.md').open('a', encoding='utf-8') as f:
        f.write(f'{datetime.datetime.now():%Y-%m-%d %H:%M} 自動續跑 {text}\n')


def verdict(dl):
    return stop_verdict(assess(dl))


def run(project, tool, binary, rounds, per_round, timeout, extra, out=print):
    dl = find_loop(project)
    if dl is None:
        out(f'找不到 {project}/.done-done/GOAL.md')
        return 2
    project = dl.parent
    prompt = PROMPT.format(project=project, state=dl.name, per_round=per_round)
    stall = 0
    for i in range(1, rounds + 1):
        ok, reasons = verdict(dl)
        if ok:
            out('可以停了：\n' + '\n'.join(f'  - {x}' for x in reasons))
            log(dl, f'結束：檢查程式說可以停（{reasons[0] if reasons else ""}）')
            return 0
        log(dl, f'第 {i} 輪開始')
        before = fingerprint(dl)  # 在寫完自己的紀錄之後才取，只有 agent 的進展會讓它改變
        out(f'第 {i}／{rounds} 輪：開一個新的 {tool} session')
        try:
            subprocess.run(build_command(tool, binary, project, prompt, extra), cwd=project,
                           timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            out(f'第 {i} 輪超過 {timeout} 秒，強制結束這一輪')
        if fingerprint(dl) == before:
            stall += 1
            if stall >= STALL_ROUNDS:
                out(f'連續 {STALL_ROUNDS} 輪狀態都沒變化，停下來交給使用者')
                log(dl, f'停止：連續 {STALL_ROUNDS} 輪沒有進展')
                return 1
        else:
            stall = 0
    ok, reasons = verdict(dl)
    if ok:
        out('可以停了：\n' + '\n'.join(f'  - {x}' for x in reasons))
        log(dl, '結束：檢查程式說可以停')
        return 0
    out(f'跑完 {rounds} 輪還沒結束；要繼續就再執行一次')
    log(dl, f'停止：到達 {rounds} 輪上限')
    return 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if '--' in argv:
        cut = argv.index('--')
        argv, extra = argv[:cut], argv[cut + 1:]
    parser = argparse.ArgumentParser(description='done-done 自動續跑')
    parser.add_argument('project', type=Path)
    parser.add_argument('--tool', choices=('codex', 'claude'), default='codex')
    parser.add_argument('--bin', help='agent 執行檔路徑；預設自動尋找')
    parser.add_argument('--rounds', type=int, default=5, help='最多跑幾輪（預設 5）')
    parser.add_argument('--per-round', type=int, default=3, help='每輪最多完成幾張工作單（預設 3）')
    parser.add_argument('--timeout', type=int, default=3 * 3600, help='每輪最長秒數（預設 3 小時）')
    args = parser.parse_args(argv)
    binary = find_binary(args.tool, args.bin)
    if not binary:
        print(f'找不到 {args.tool} 執行檔；用 --bin 指定路徑', file=sys.stderr)
        return 2
    try:
        return run(args.project, args.tool, binary, args.rounds, args.per_round, args.timeout, extra)
    except (OSError, UnicodeDecodeError, StateError) as exc:
        print(f'無法讀取狀態：{exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
