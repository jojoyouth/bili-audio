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

## 使用

```bash
cd 小玩具/bili_audio
./bili_audio.py "搜尋關鍵字"
```

常用模式：

```bash
# 搜尋兩頁結果，再用 fzf 選擇播放
./bili_audio.py "python 教學" --pages 2

# 只印出選到的 Bilibili 影片頁網址
./bili_audio.py "lofi" --print-url

# 不進 fzf，直接取第一筆影片網址
./bili_audio.py "lofi" --first --print-url

# 輸出搜尋結果 JSON，可接其他 terminal workflow
./bili_audio.py "lofi" --json

# 用 yt-dlp 取直接音訊串流網址
./bili_audio.py "lofi" --direct-url
```

如果影片需要登入狀態，可以讓 `mpv` / `yt-dlp` 讀瀏覽器 cookies：

```bash
./bili_audio.py "關鍵字" --cookies-from-browser chrome
```

也可以用環境變數給搜尋 API cookie：

```bash
export BILIBILI_COOKIE='SESSDATA=...; bili_jct=...'
./bili_audio.py "關鍵字"
```

`--direct-url` 印出的串流網址通常會過期，而且有些網址仍需要 Referer 或 cookies；日常播放建議直接用預設的 `mpv --no-video` 模式。
