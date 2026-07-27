# bili-audio

在 terminal 搜尋與瀏覽 Bilibili，透過 `fzf` 選片，再選擇：

- 只聽音訊：沿用原本的 `mpv --no-video`
- 在 terminal 播放影片：使用 mpv 的 `tct` 或 `kitty` video output

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

原本的關鍵字語法仍然保留：

```bash
./bili_audio.py "lofi"
./bili_audio.py "python 教學" --pages 2
```

若關鍵字剛好是 `hot`、`rank`、`user`、`users`、`search` 或
`backend-info`，請明確加上 `search`，避免和新子命令混淆：

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

## 播放操作

影片清單中的按鍵：

| 按鍵 | 動作 |
| --- | --- |
| `Enter` | 使用 `--play-mode` 的預設模式（預設音訊） |
| `Ctrl-A` | 只聽音訊 |
| `Ctrl-V` / `Alt-V` | 在 terminal 播放影片 |
| `Ctrl-F` / `Alt-N` | 搜尋結果下一頁 |
| `Ctrl-B` / `Alt-P` | 搜尋結果上一頁 |
| `Esc` | 離開 |

播放時使用 mpv 的一般快捷鍵：`Space` 暫停、左右方向鍵快退／快進、`q`
回到清單。

非互動模式可直接指定影片：

```bash
# 第一筆只聽音訊
./bili_audio.py search "lofi" --first

# 第一筆在 terminal 播放影片
./bili_audio.py search "lofi" --first --play-mode video

# 強制使用通用的 ANSI true-color 方塊輸出
./bili_audio.py hot --first --play-mode video --video-output tct

# Kitty / WezTerm 可使用較快的圖片協定
./bili_audio.py hot --first --play-mode video --video-output kitty
```

`--video-output auto` 在 Kitty／WezTerm／Ghostty 使用 `kitty`，其他
terminal 使用相容性較高的 `tct`；在 tmux／screen／Zellij 裡會安全退回
`tct`。程式也會先用 `mpv --vo=help` 確認該 output 確實存在。`tct`
是彩色字元方塊畫面，
解析度與效能會低於一般 GUI 播放器，這是 terminal 內播放的正常取捨。
影片模式必須直接在互動式 TTY 執行，不能 pipe 到檔案或其他程式。

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

需要登入狀態的影片，可以明確讓 mpv／yt-dlp 讀瀏覽器 cookies：

```bash
./bili_audio.py search "關鍵字" --cookies-from-browser chrome
```

或使用 Netscape 格式的 cookie 檔：

```bash
./bili_audio.py search "關鍵字" --cookies /path/to/cookies.txt
```

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
python3 -m py_compile bili_audio.py bili_backend.py
```

## 上游致謝

資料 adapter 依照 `public-clis/bilibili-cli` 的 Apache-2.0 JSON envelope
與命令介面實作；本專案只把它當作可選的外部程式使用。上游專案與授權：

- <https://github.com/public-clis/bilibili-cli>
- <https://github.com/public-clis/bilibili-cli/blob/main/LICENSE>
