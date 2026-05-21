# YouTube Video Downloader & Player for NVDA

Author: Vatsal Gautam

URL: https://github.com/thundergod60/YouTubeVideoDownloader

This addon allows NVDA users to download YouTube videos and playlists accessibly.

The addon includes:
- a download queue
- playlist support
- a status window
- a persistent video library
- built-in playback support
- keyboard shortcuts for quick downloading

Videos can be downloaded directly from the clipboard and managed through accessible dialogs designed for screen reader users.

---

## Features

- Download YouTube videos from the clipboard
- Download full playlists
- Accessible download queue
- Download progress announcements
- Download status window
- Video library saved between NVDA sessions
- Playlist browsing
- Built-in media playback
- Playback controls
- Open downloaded videos later from the library
- Keyboard shortcut support

---

## Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| NVDA + Shift + Y | Add YouTube URL from clipboard |
| NVDA + Shift + Q | Open download queue to add youtube videos manually |
| NVDA + Shift + P | Open video library and player |
| NVDA + Shift + O | Open download status window |

All shortcuts can be changed from:

NVDA Menu → Preferences → Input Gestures

---

## Installation

1. Download the addon
2. Open the `.nvda-addon` file
3. Press Yes when NVDA asks to install the addon
4. Restart NVDA if needed

---

## How To Download a Video

1. Copy a YouTube link
2. Press:
   `NVDA + Shift + Y`
3. The video will be added to the download queue

---

## Download Queue

Press:

`NVDA + Shift + Q`

The queue window allows you to:
- monitor downloads
- cancel downloads
- clear completed downloads
- manually add URLs

---

## Video Library & Player

Press:

`NVDA + Shift + P`

The library window allows you to:
- browse downloaded videos
- browse playlists
- read video descriptions
- play videos
- pause playback
- seek backward and forward
- open the downloads folder

---

## Download Status Window

Press:

`NVDA + Shift + O`

The status window shows:
- active downloads
- playlist progress
- completed downloads
- errors

---

## Download Location

Videos are downloaded to:

```text
Downloads\NVDA_YTDownloader
```

---

## Notes

- Only YouTube links are currently supported
- Large playlists may take time to download
- Some videos may fail if YouTube changes its systems
- Region-restricted videos may not work

---

## Credits

- NVDA
- yt-dlp
- VLC
- NVDA community

---

    
