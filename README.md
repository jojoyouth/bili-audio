# Bilibili Audio FZF

用 Bilibili 搜尋 API 找影片，透過 `fzf` 選標題，再用 `mpv --no-video` 播放音訊。

## 需要的工具

```bash
brew install fzf mpv
```

如果要把選到的影片解析成直接音訊串流網址，再加裝：

```bash
brew install yt-dlp
```

## 基本使用

```bash
cd 小玩具/bili_audio
./bili_audio.py "搜尋關鍵字"
```

預設會進入互動模式：

1. 搜尋 Bilibili 影片。
2. 用 `fzf` 選擇影片。
3. 用 `mpv --no-video` 播放音訊。
4. mpv 播完或在 mpv 裡按 `q` 後，回到同一頁 `fzf` 結果。

在 `fzf` 裡可以使用：

| 按鍵 | 動作 |
| --- | --- |
| `Enter` | 播放目前選取的影片 |
| `Ctrl-F` | 下一頁，只顯示下一頁結果，例如 21-40 |
| `Ctrl-B` | 上一頁，只顯示上一頁結果，例如 1-20 |
| `Esc` / `Ctrl-C` | 離開程式 |

`Ctrl+C` 會乾淨結束並顯示 `已中斷。`，不會印出 Python traceback。

## 常用範例

```bash
# 搜尋後進入 fzf，選取影片播放
./bili_audio.py "python 教學"

# 預先搜尋兩頁；fzf 一開始顯示第 1 頁，Ctrl-F 切到第 2 頁
./bili_audio.py "python 教學" --pages 2

# 調整 mpv 音量，範圍 0-100，預設 40
./bili_audio.py "lofi" --volume 20
```

## 非互動模式

這些模式維持一次性行為，不會進入循環播放：

```bash
# 輸出搜尋結果 JSON
./bili_audio.py "lofi" --json

# 不進 fzf，直接取第一筆影片網址
./bili_audio.py "lofi" --first --print-url

# 用 fzf 選擇後，只印出 Bilibili 影片頁網址
./bili_audio.py "lofi" --print-url

# 用 yt-dlp 取直接音訊串流網址
./bili_audio.py "lofi" --direct-url

# 不進 fzf，直接播放第一筆
./bili_audio.py "lofi" --first
```

`--direct-url` 印出的串流網址通常會過期，而且有些網址仍需要 Referer 或 cookies；日常播放建議直接用預設的 `mpv --no-video` 模式。

## Cookies

如果影片需要登入狀態，可以讓 `mpv` / `yt-dlp` 讀瀏覽器 cookies：

```bash
./bili_audio.py "關鍵字" --cookies-from-browser chrome
```

也可以用環境變數給搜尋 API cookie：

```bash
export BILIBILI_COOKIE='SESSDATA=...; bili_jct=...'
./bili_audio.py "關鍵字"
```
