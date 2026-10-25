# 自動模式：停止閘門、自動續跑、接手提醒

skill 本身沒辦法讓 agent 一直跑。這三個工具補上這一塊，都要使用者自己裝一次，或授權 agent 幫忙裝。不裝也能用 done-done，只是 agent 比較容易提早停下，或 session 結束就停了。

## 什麼時候開自動模式

- 開工訪談完成、使用者確認「我理解你要的」、說可以開始之後，把 GOAL.md 標頭的 `autopilot` 改成 on。
- 使用者喊停、要改方向，或只是想聊天討論，就改回 off。
- 自動模式下也只照四種情況停（見 SKILL.md「何時停」）。等使用者回覆是合理的停：先把問題寫進 QUESTIONS.md，檢查程式就會放行。

## 停止閘門（Stop hook）

agent 每次想結束一輪回覆時，`scripts/loop_hook.py` 會跑一次檢查：

- 不在 done-done 專案、或 autopilot 不是 on：什麼都不做。
- 檢查程式說可以停：放行。
- 還不能停：擋下來，把「還有哪些工作單可以做」當成下一個指示，agent 會接著做。
- 狀態檔連續兩次沒有任何變化還想停：放行，並在 LOG.md 記一行，交給使用者判斷，避免空轉燒額度。

安裝（擇一）：

- **Codex**：把 `templates/hooks/codex-hooks.json` 的內容放到 `~/.codex/hooks.json`（全部專案）或 `<專案>/.codex/hooks.json`（只有這個專案）。接著在 Codex 裡執行 `/hooks`，檢查內容後按信任；沒信任前 hook 不會執行。
- **Claude Code**：把 `templates/hooks/claude-settings.json` 的 hooks 合併進 `~/.claude/settings.json` 或 `<專案>/.claude/settings.json`。

同一份設定也裝了 SessionStart hook：新 session、恢復對話或 context 被壓縮後，它會用三行提醒 agent 這個專案用 done-done、整合者是誰、目前可以做什麼。

## 自動續跑（run_loop.py）

適合要整段時間不在電腦前、又希望專案持續推進的時候。每一輪開一個全新的 agent session，只靠 `.done-done/` 的檔案接續，所以不會越跑越慢、越跑越貴。

```sh
python3 <skill>/scripts/run_loop.py <專案> --tool codex --rounds 5 -- --sandbox workspace-write
python3 <skill>/scripts/run_loop.py <專案> --tool claude --rounds 5 -- --permission-mode acceptEdits
```

- 每輪開始前先跑檢查；檢查程式說可以停（完成、到期、在等使用者），就結束。
- 連續兩輪狀態都沒變化、到了輪數上限，也會停，並在 LOG.md 記一行。
- `--` 後面的參數原封不動交給 agent，例如權限模式、模型。要給多大的權限由使用者決定；不確定就先用較小的權限。
- 跑的期間，不要同時在別的 session 當整合者。GOAL.md 的 integrator 寫「run_loop（每輪新 session）」。
- 每一輪都會用掉額度。額度緊的話，把 `--rounds` 設小，並在 GOAL.md 的 budget 寫清楚收尾門檻。

## 接手提醒（AGENTS.md）

不想裝 hook，也可以把 `templates/AGENTS-snippet.md` 的內容加進專案的 AGENTS.md（Codex）或 CLAUDE.md（Claude Code）。agent 每次開新 session 都會讀這兩個檔，就會知道要先讀狀態檔、跑檢查。

## 這些做法的來源

停止閘門與接手提醒參考 planning-with-files 的 hook 做法；每輪全新 session 的自動續跑參考 Ralph。都用自己的寫法重新實作，詳見 README 的致謝。
