# bili-audio

登入後，在 terminal 瀏覽帳號的首頁推薦，挑一部或多部影片連續播放。
預設播放影片、使用 kitty 圖片輸出，並選取帳號可取得的最高可用影像＋音訊串流。
純音訊請用 `--play-mode audio`，或在影片清單按 Ctrl-A。

```bash
brew install fzf mpv yt-dlp qrencode
./bili_audio.py             # 首次掃碼 → 自動驗證保存 → 直接進推薦
# 之後同一條命令直接進推薦，登入失效才再次要求掃碼
./bili_audio.py home        # 其他功能選單：搜尋、熱門、帳號等
```

`qrencode` 只用於本機產生 QR code，不會把登入連結送到第三方服務。
不用掃碼時，可以用 `login --cookies /path/to/cookies.txt` 匯入現有登入。

在 terminal 搜尋與瀏覽 Bilibili，透過 `fzf` 選片，再選擇：

- 只聽音訊：沿用原本的 `mpv --no-video`
- 在 terminal 播放影片：使用 mpv 的 `tct` 或 `kitty` video output
- 一般播放器視窗：`--play-mode window`，可用滑鼠點選進度條、暫停與音量

另外整合了 [public-clis/bilibili-cli](https://github.com/public-clis/bilibili-cli)
的結構化輸出，可查影片、UP 主、熱門與排行榜。整合採可替換 adapter，
沒有把上游套件或其 SDK 複製進本專案。

## 安裝工具

需要 Python 3.10 以上版本。

```bash
brew install fzf mpv yt-dlp
```

- `fzf`：互動選擇
- `mpv`：播放音訊與 terminal 影片
- `yt-dlp`：讓 mpv 解析 Bilibili 影片頁；`--direct-url` 也會使用
- `qrencode`：選用，QR code 登入需要；`brew install qrencode`

執行 `./bili_audio.py doctor` 檢查工具與本機登入是否已儲存。
`doctor` 不連線驗證帳號，也不顯示 cookies；驗證帳號請用 `status`。

可用以下指令確認 mpv 的 terminal video output：

```bash
mpv --vo=help | grep -E 'tct|kitty|sixel'
```

### 選用：bilibili-cli 後端

`--backend auto` 是預設值：找到 `bili` 指令時優先使用它，失敗或未安裝
則退回 Bilibili 公開 API。因此舊的影片搜尋、熱門與排行榜不必安裝額外
Python 套件。

如果要使用上游較完整的使用者影片資料：

```bash
uv tool install bilibili-cli
./bili_audio.py backend-info
```

也可以指定模式：

```bash
./bili_audio.py hot --backend direct
./bili_audio.py user 946974 --backend bili-cli
./bili_audio.py backend-info --bili-cli /path/to/bili
```

> 上游核心 SDK 已在 2026-07 停止維護，所以本專案不會自動安裝或直接
> import 它；adapter 可隨時停用，公開 API fallback 仍可運作。

## 使用方式

不帶參數會直接開啟帳號推薦；需要時在同一流程內掃碼登入、驗證並儲存，
不必另外執行 login／status。Esc 離開推薦。
`home` 保留完整功能選單，輸入文字篩選、Enter 開啟、Esc 離開；
從選單進入影片清單後，結束會回到選單。腳本或 pipe 請使用明確子命令，例如 `hot --json`。

原本的關鍵字語法仍然保留：

```bash
./bili_audio.py "lofi"
./bili_audio.py "python 教學" --pages 2
```

若關鍵字剛好是子命令名稱（例如 `hot`、`login`、`recommend`），
請明確加上 `search`，避免和子命令混淆：

```bash
./bili_audio.py search "hot"
./bili_audio.py search "hot dog"
```

也可以明確使用子命令：

```bash
# 搜尋影片
./bili_audio.py search "lofi"

# 搜尋 UP 主，先找出正確 UID，避免同名使用者
./bili_audio.py users "影視颶風"

# 顯示 UP 主資料
./bili_audio.py user 946974

# 顯示資料並載入 10 部影片供播放（需要 bilibili-cli 後端）
./bili_audio.py user 946974 --videos 10

# 熱門影片與排行榜
./bili_audio.py hot
./bili_audio.py hot --page 2 --limit 10
./bili_audio.py rank
./bili_audio.py rank --limit 30
```

目前 Bilibili 的全站榜是即時榜，不區分 3 日／7 日；`--day 3` 與
`--day 7` 只保留作為相容參數，執行時會明確提示已忽略，避免把同一份
榜單誤標成不同週期。

## 本機播放紀錄與續播

```bash
./bili_audio.py history            # 最近播放過的，新的在最前
./bili_audio.py history -n 20      # 只看最近 20 筆
./bili_audio.py history --json
./bili_audio.py history --clear    # 清除紀錄；不影響登入與續播位置
```

播放位置交給 mpv 自己的 watch-later 機制處理：用 `q` 或關掉視窗離開時存檔，
影片正常播完則由 mpv 自行刪除，所以這個工具不會替你把任何影片標記成「看完」。
位置檔在 `~/.config/bili-audio/watch-later/`，紀錄本身在 `history.json`，
兩者都與 cookies 分開存放。

紀錄只保存 bvid、標題、作者與最後一次「開始播放」的時間，上限 200 筆並依 bvid 去重；
寫入失敗只會提示，不會中斷播放。檔案被改壞時會提示一次並當成空清單，不會崩潰。

## 稍後再看與收藏夾（唯讀）

```bash
./bili_audio.py watchlater                      # 帳號的稍後再看
./bili_audio.py favorites                       # 先選收藏夾，再選影片
./bili_audio.py favorites --json                # 列出收藏夾與 media_id
./bili_audio.py favorites --media-id 123456789    # 直接開啟指定收藏夾（id 由上一行取得）
```

兩者都只讀取，不會替你加入或移除任何項目。失效與非影片項目（合集、下架影片）
不會送進播放器，略過幾筆會明確提示。收藏夾每頁 20 筆，`Ctrl-F`／`Ctrl-B` 換頁。

## 帳號與個人推薦

```bash
./bili_audio.py                          # 一個流程完成登入與推薦
./bili_audio.py status                   # 選用：強制向伺服器重驗
./bili_audio.py login                    # 選用：主動切換帳號
./bili_audio.py recommend --page 2 --limit 10 --json
./bili_audio.py recommend --first --print-url
./bili_audio.py logout
```

首次登入驗證成功後，cookies 與驗證結果一起保存，下次開啟沿用結果，
跳過登入與重驗，直接用同一份 cookies 查詢推薦。
已知 Cookie 到期時間時，快取隨 Cookie 到期；沒有到期時間則在 24 小時後背景重驗。
背景重驗仍有效就繼續，只有未登入、Cookie 過期或伺服器明確回覆登入失效才要求掃碼。
推薦查詢中途若回覆登入失效，會清除驗證快取，重新登入後從新清單開始，最多重試一次。
網路逾時、412／429 不會觸發掃碼或覆蓋原帳號。若伺服器提前撤銷登入卻回傳匿名成功資料，
本機快取無法單靠該回應判斷；可用 `status` 強制重新驗證。

`--json`／`--first`／URL 輸出及非 TTY 不會突然啟動掃碼，失效時回報錯誤。
顯式 `--cookies` 或環境變數失效時需更新該來源，避免掃碼成功後又被舊設定覆蓋。
推薦每批最多 30 筆，
`--limit 0` 表示保留 API 該批回傳的全部影片；廣告、直播及非影片卡片會略過。
推薦由 Bilibili 決定，內容與網頁首頁不一定完全相同。

清單內用 Ctrl-F 取下一批、Ctrl-B 返回已載入的上一頁、Ctrl-R 換一批。
返回上一頁使用記憶體內的清單，不會重發請求；換一批會更新目前頁面。
`--page` 是推薦請求的起始批次，不是永久固定的頁碼。

登入憑證預設存於 `~/.config/bili-audio/cookies.txt`（支援 `XDG_CONFIG_HOME`）；
可用 `BILI_AUDIO_CONFIG_DIR` 指定這個應用程式的設定目錄。
新增的設定目錄權限為 `700`，cookie 檔為 `600`，先驗證成功才原子取代舊登入。
掃碼逾時、取消或匯入無效 cookies 不會覆蓋原本帳號。

`logout` 只移除這個工具儲存的登入，不會撤銷 Bilibili 伺服器 session，
也不會登出瀏覽器或手機。互動推薦在登入過期後自動引導掃碼；目前不做背景續期。

## 播放操作

影片清單中的按鍵：

| 按鍵 | 動作 |
| --- | --- |
| `Enter` | 使用 `--play-mode` 的預設模式（預設影片） |
| `Tab` / `Shift-Tab` | 勾選／取消勾選，並移動到下一筆／上一筆 |
| `Ctrl-A` | 只聽音訊 |
| `Ctrl-V` / `Alt-V` | 在 terminal 播放影片 |
| `Ctrl-F` / `Alt-N` | 搜尋結果下一頁 |
| `Ctrl-B` / `Alt-P` | 搜尋結果上一頁 |
| `Ctrl-R` | 推薦清單換一批 |
| `?` | 顯示按鍵與常用參數說明；移動游標回到影片簡介 |
| `Esc` | 離開 |

播放時使用 mpv 的一般快捷鍵：`Space` 暫停、左右方向鍵快退／快進、`q`
回到清單。

多選時按選取順序逐部播放；mpv 的 `q` 結束目前影片並播放下一部，
Ctrl-C 中止整批。播放失敗會停止這批佇列並返回清單。
底部預覽顯示完整標題、作者、時長、觀看數與簡介／推薦理由。
多選只用於互動播放；`--print-url`／`--direct-url` 仍選單一影片。

terminal 影片不是 GUI 控制列：使用 `Space` 暫停、`←`／`→` 跳轉、`9`／`0`
調小／調大音量、`m` 切換靜音。畫面疊上播放時間、總長度、音量及暫停／靜音狀態。
音量調整顯示數字與調整條；靜音／取消靜音、暫停／繼續播放會顯示兩秒提示。
若需要可點擊的進度條與控制列，改用一般 mpv 視窗：

```bash
./bili_audio.py recommend --play-mode window
```

沒有聲音時可先確認音訊裝置；播放預設啟用音軌、取消靜音，音量預設 40%。

```bash
mpv --audio-device=help
# macOS：明確輸出到 MacBook 內建喇叭
./bili_audio.py recommend --play-mode video --video-output kitty --audio-device=coreaudio/BuiltInSpeakerDevice
```

`--audio-device` 只影響這次 mpv 播放，不修改系統預設音訊裝置。

非互動模式可直接指定影片：

```bash
# 第一筆只聽音訊
./bili_audio.py search "lofi" --first --play-mode audio

# 第一筆在 terminal 播放影片
./bili_audio.py search "lofi" --first --play-mode video

# 強制使用通用的 ANSI true-color 方塊輸出
./bili_audio.py hot --first --play-mode video --video-output tct

# 確認 terminal 相容 mpv kitty 輸出時，可明確指定圖片協定
./bili_audio.py hot --first --play-mode video --video-output kitty
```

`--video-output` 預設為 `kitty`，畫質仍受 terminal 視窗尺寸與帳號可用串流限制。
手動指定 `--video-output auto` 時，只在偵測到 Kitty 時使用 `kitty`；Ghostty、WezTerm 與其他
terminal 使用相容性較高的 `tct`，避免僅支援部分圖片協定時沒有畫面；在 tmux／screen／Zellij 裡會退回
`tct`。程式也會先用 `mpv --vo=help` 確認該 output 確實存在。`tct`
是彩色字元方塊畫面，
解析度與效能會低於一般 GUI 播放器，這是 terminal 內播放的正常取捨。
影片模式必須直接在互動式 TTY 執行，不能 pipe 到檔案或其他程式。

若顯示「終端播放影片（kitty）」後沒有畫面，先按 Ctrl-C，再執行：

```bash
./bili_audio.py recommend --play-mode video --video-output tct
```

影片模式保留 mpv 警告與錯誤；`--timeout` 也用於 mpv 網路與 yt-dlp socket，
yt-dlp 一般重試限制為一次。這是單次網路操作的逾時，不是整部影片的播放時間上限。

## 裝成系統指令

```bash
./bili_audio.py install
```

做兩件事，都不需要改 `.zshrc`：

- `~/.local/bin/bili-audio` → 指向這支腳本的 symlink（該目錄已在 PATH）
- `/opt/homebrew/share/zsh/site-functions/_bili-audio` → zsh 補全（該目錄已在 fpath）

開一個新的 terminal 之後就可以：

```bash
bili-audio                 # 等同 ./bili_audio.py
bili-audio hi<TAB>         # 補成 history
bili-audio history --<TAB> # 列出該子命令的參數
```

補全是 `#compdef` 自動載入形式，zsh 第一次用到才讀取。
新增參數之後重跑一次 `install` 就會重新產生。

```bash
bili-audio install --prefix ~/somewhere   # 裝到別的位置
bili-audio install --uninstall            # 移除
```

symlink 指向原始檔案的位置，所以不要在安裝後移動或刪除這個專案資料夾。

## 查可用參數

```bash
./bili_audio.py completion --list        # 列出每個子命令與它的參數
./bili_audio.py history -h               # 單一子命令的完整說明
```

補全建議直接用上面的 `install`。不想安裝的話也可以每次手動載入
（`install` 不會做這件事，這兩行要自己加進 `~/.zshrc`）：

```bash
eval "$(./bili_audio.py completion zsh)"
eval "$(./bili_audio.py completion bash)"    # bash 版
```

補全清單直接從 argparse parser 產生，新增參數不需要另外維護一份名單。
在影片清單中按 `?` 也會顯示同一份常用參數。

## 終端機與畫面速度

kitty 圖片輸出有兩種傳輸方式，`--kitty-shm` 預設 `auto`：

| 終端機 | 傳輸方式 | 說明 |
| --- | --- | --- |
| kitty.app | 共享記憶體 | 每幀不經過 PTY，明顯較快 |
| Ghostty、WezTerm 等 | base64 | 看得懂圖片協定但不讀共享記憶體 |
| tmux／screen／zellij 內 | base64 | 多工器不轉送共享記憶體 |

**共享記憶體只有 kitty.app 自己支援。** 在 Ghostty 強制開啟會完全沒有畫面
（mpv 只送出一個指向記憶體的指標，終端機不會去讀），所以 `auto` 只在偵測到
真正的 kitty 時啟用。要手動覆寫用 `--kitty-shm yes|no`。

在 Ghostty 這類只能走 base64 的終端機，每幀約 230 KB 要經過 PTY，
畫面越大越吃力；想更順就把終端機視窗縮小，或改用 `--video-output tct`。

完全沒有畫面時，先試字元方塊模式確認其他環節正常：

```bash
./bili_audio.py --video-output tct
```

## 浮動小視窗（推薦的摸魚方式）

```bash
bili-audio --play-mode float
bili-audio --play-mode float --float-size 20   # 最大佔螢幕 20%
```

開一個無邊框、置頂的 mpv 小視窗，釘在**螢幕**右下角：

- 對齊的是螢幕角落而不是 terminal，所以 terminal 佔右半邊或只佔右下 1/4，
  小視窗都在同一個位置（兩種佈局共用螢幕右下角）
- GPU 繪圖加 VideoToolbox 硬解，沒有 terminal 圖片協定的速度上限
- 視窗比例跟著影片，不會變形也不會被裁切；之後可直接用滑鼠拖動
- 開啟時不搶焦點（繼續在 terminal 打字），Dock 不出現 mpv 圖示，
  視窗標題顯示 `zsh` 而不是片名
- terminal 只印一行「載入中…」，播放紀錄與續播位置照常運作

terminal 會停在這一部播完（或在小視窗按 `q`）才回到清單，想同時做事請開另一個分頁。

### 全域 boss key

```bash
bili-audio boss    # 收起：暫停＋靜音＋縮小；再執行一次就還原
```

不用切到小視窗就能操作，透過 mpv 的 IPC socket（`~/.config/bili-audio/mpv.sock`）。
還原時會恢復收起前的靜音狀態，原本就靜音的不會被打開。

綁成全域快捷鍵（macOS 內建「捷徑」App，不用另外安裝東西）：

1. 打開「捷徑」→ 新增捷徑 → 加入動作「執行 Shell 指令碼」
2. 內容填：

   ```bash
   /opt/homebrew/bin/python3 "$HOME/.local/bin/bili-audio" boss
   ```

   **要寫 Homebrew 的 python3 完整路徑。** 「捷徑」執行時的 PATH 會先找到
   系統內建的 `/usr/bin/python3`（3.9），這個專案需要 3.10 以上。
3. 捷徑資訊 → 加入鍵盤快速鍵
4. 第一次執行時 macOS 會要求允許執行指令碼，在「捷徑」設定的「進階」裡開啟

## 隱蔽播放（terminal 內）

```bash
./bili_audio.py --stealth
```

不想開額外視窗時用，例如透過 SSH。畫面走 terminal 圖片協定，
所以有速度上限，位置也只在啟動時計算；一般情況建議用上面的 `--play-mode float`。

畫面貼在右下角，關掉 OSD；不印片名、網址與操作提示，只留一行「載入中…」。

畫面高度用**文字列數**計算（預設 14 列，用 `--stealth-rows` 調整），不隨視窗縮放。
用列數而不是像素，是因為 Retina 的一個字元是兩倍像素高，用像素會在不同螢幕上大小不一。
所以把 terminal 從半螢幕切成右下 1/4 時，小畫面維持同樣大小與同樣的相對位置，
只有視窗小到放不下才會自動縮減。

```bash
./bili_audio.py --stealth --stealth-rows 8    # 想更小
```

畫面會貼齊右緣，底部留一列。mpv 用 `ESC[top;leftf`（1-based）定位、
並以 `C=1` 傳圖（游標不動）且在 alternate screen 上，所以用到最後一欄不會捲動。

**位置是啟動 mpv 當下算好的，之後不會跟著視窗變動。**
啟動後把視窗縮小會讓畫面超出邊界被切掉，拉大則會讓畫面停在中間。
先把視窗調成要用的大小再開始播放。不確定時可以先看：

```bash
./bili_audio.py doctor --terminal
```

mpv 內按 `b` 立刻收掉畫面但音訊繼續，`B` 則同時暫停與靜音。
按 `q` 是結束目前這部並回到影片清單，要完全離開請在清單按 `Esc`。

定位會預留 16:9 的寬度，所以 4:3 的影片會稍微離右緣遠一點；
mpv 自己保持比例，不會變形。

這只改變 terminal 上看到的樣子，不會隱藏網路連線，
受管控的機器仍可從網路紀錄看到連往 Bilibili 的流量。

## 輸出與串流

```bash
# 結構化 JSON
./bili_audio.py search "lofi" --json --limit 5
./bili_audio.py users "音樂" --json
./bili_audio.py user 946974 --json
./bili_audio.py hot --json
./bili_audio.py rank --json

# 只印影片頁 URL
./bili_audio.py search "lofi" --first --print-url

# 用 yt-dlp 印出直接串流 URL
./bili_audio.py search "lofi" --first --direct-url
```

直接串流 URL 通常會過期，而且可能仍需要 Referer 或 cookies；日常使用
建議直接交給 mpv。

## Cookies

推薦使用 `login` 儲存登入；API 與 mpv／yt-dlp 都會沿用這份登入。

匯入 Netscape cookie 檔（只保留 Bilibili 網域，先驗證再儲存）：

```bash
./bili_audio.py login --cookies /path/to/cookies.txt
```

也可以執行 `./bili_audio.py login --cookie-stdin`，在不回顯的提示中貼上
Cookie header。非互動 stdin 可用於腳本；不要將 Cookie 值直接寫進 shell 指令。

需要登入狀態的影片，可以明確讓 mpv／yt-dlp 讀瀏覽器 cookies：

```bash
./bili_audio.py search "關鍵字" --cookies-from-browser chrome
```

或使用 Netscape 格式的 cookie 檔：

```bash
./bili_audio.py search "關鍵字" --cookies /path/to/cookies.txt
```

`--cookies` 同時用於 API 與播放，僅該次執行有效，不會自動匯入本機登入。
API 憑證優先順序為 `--cookies` → `BILIBILI_COOKIE`／`BILI_COOKIE` → 儲存的登入。
`--cookies-from-browser` 仍只交給 mpv／yt-dlp，不會自動把整個瀏覽器 Cookie 資料庫
匯入本工具；查看推薦請先用 `login` 登入。
有指定憑證時，`--backend auto` 會選 direct，避免選用另一份 `bili` 帳號。
`recommend` 固定使用本工具的帳號與 direct API。

搜尋公開 API 也可讀既有環境變數：

```bash
export BILIBILI_COOKIE='SESSDATA=...; bili_jct=...'
./bili_audio.py search "關鍵字" --backend direct
```

程式不會印出 cookie 值，也不會把環境變數中的 cookie 放進子程序命令列。
公開 API 為通過初始反爬檢查而取得的 cookie，只會放在權限隔離的暫存目錄，
程序結束時清除；除非你明確設定 `BILI_COOKIE_JAR`，否則不會持久保存。
不要把 cookie 放進 Git。

## 測試

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile bili_audio.py bili_auth.py bili_backend.py

# 選用：需要網路與 qrencode；只查匿名 API，不讀取或保存帳號
python3 tests/live_smoke.py
```

單元測試涵蓋登入成功／失效、cookie 網域與權限、QR 輪詢、推薦資料正規化、
分頁／換一批與多選播放。真實帳號驗收需本人執行 `login` 掃碼，再確認
`status`、`recommend` 與播放。匿名 smoke test 不代表已驗證帳號個人化結果。

本輪狀態與下一輪工作：[state.md](state.md)、[NEXT_SESSION.md](NEXT_SESSION.md)。

## 設計參考

- [biliterminal](https://github.com/teee32/biliterminal)：參考本機掃碼登入、帳號推薦與登入狀態的使用流程。
- [ytfzf](https://github.com/pystardust/ytfzf)：沿用 fzf + mpv 的操作方向，增加多選連播與文字預覽；未複製上游程式碼。
- [Bilibili API collect 的推薦介面紀錄](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/video/recommend.md)：對照新版首頁 V8 推薦參數與每批筆數限制。這是社群紀錄，並非官方穩定 API 承諾。

## 上游致謝

資料 adapter 依照 `public-clis/bilibili-cli` 的 Apache-2.0 JSON envelope
與命令介面實作；本專案只把它當作可選的外部程式使用。上游專案與授權：

- <https://github.com/public-clis/bilibili-cli>
- <https://github.com/public-clis/bilibili-cli/blob/main/LICENSE>
