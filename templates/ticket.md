---
id: T01
title: 顧客在預約頁選好時段送出，預約成功並收到確認信
status: ready            # ready / doing / review / done / blocked / dropped
milestone: M1
depends_on:              # 其他工作單編號，逗號分隔
blocked_by:              # 只放 QUESTIONS.md 的 Q／X 編號
owner:                   # doing 之後必填：誰在做
reviewer:                # done 前必填，不能等於 owner
risk: normal             # normal / data（會動到資料或金額）/ access（權限）
qa:                      # 空白或 blind；risk 是 data／access 時一定要盲測
code_reviewer:           # risk 是 data／access 時必填：看過程式碼改動的人，不能是作者
resources:               # 例：db:test-site@write, file:/abs/path/app@write
lease: released          # 動手寫共用資源時改 held，交接時改回 released
---
# T01 顧客在預約頁選好時段送出，預約成功並收到確認信

## 為什麼
<!-- 真實情境與痛點，並指出來自哪一條使用者需求或哪個實際缺陷。改良類工作要寫預期效益；
     找不到需求或缺陷來源的，不開這張工作單。
     標題、為什麼、驗收標準用使用者看得懂的話寫；版本號、路徑、內部代號放在證據與交接紀錄。 -->

## 要做什麼
<!-- 一段使用者能獨立驗證的行為（垂直切片），不是「做後端」「做前端」。 -->

## 驗收標準
<!-- 格式：- AC1 [等級] 描述。等級：build / unit / api / ui / user / prod，
     代表最低要哪一級的證據。動到畫面就至少 ui；要給真實使用者用就寫 user。 -->
- AC1 [ui] 顧客選好時段按「送出」，畫面顯示預約成功，後台預約列表出現這一筆
- AC2 [api] 測試信箱收到確認信，信裡的日期和時段跟預約一致

## 證據
<!-- 格式：- AC1 PASS level=ui rev=<commit> env=<local|staging|prod> artifact=<專案內路徑或網址>
     ui 以上的 artifact 要是截圖、錄影或瀏覽器紀錄（或裝著它們的資料夾），文字報告不算。
     盲測驗收者自己留的證據加 by=<驗收者名稱>。失敗也要記（FAIL / PARTIAL / SKIP），不要刪。 -->

## 交接紀錄
<!-- 每次交接一段：日期、誰、做了什麼、結果、還沒做的、建立或釋放了哪些東西。 -->
