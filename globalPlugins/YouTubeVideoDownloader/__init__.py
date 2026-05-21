# YouTube Video Downloader & Player - NVDA Addon
# __init__.py
#
# Dependencies (bundle in addon/globalPlugins/youtubeDownloader/lib/):
#   - yt_dlp folder  (required — copy from pip's site-packages)
#   - vlc.py         (optional — from python-vlc; needs VLC app installed)
#
# Keyboard shortcuts (remappable via NVDA Input Gestures):
#   NVDA+Shift+Y  -> Grab URL from clipboard & add to queue
#   NVDA+Shift+Q  -> Open Download Queue dialog
#   NVDA+Shift+P  -> Open Player / Library dialog

# ---------------------------------------------------------------------------
# STEP 1: sys and os MUST come before everything else so we can inject the
#         lib folder into sys.path before any third-party import is attempted.
# ---------------------------------------------------------------------------
import sys
import os

# Resolve the real directory of this __init__.py (handles symlinks too)
_addon_dir = os.path.dirname(os.path.abspath(__file__))
_lib_path  = os.path.join(_addon_dir, "lib")

if _lib_path not in sys.path:
    sys.path.insert(0, _lib_path)   # position 0 = highest priority

# ---------------------------------------------------------------------------
# STEP 2: Standard NVDA / stdlib imports
# ---------------------------------------------------------------------------
import globalPluginHandler
import ui
import api
import wx
import threading
import json
import time
import subprocess
import logHandler          # NVDA's built-in logger -> %TEMP%\nvda.log
from pathlib import Path

# ---------------------------------------------------------------------------
# STEP 3: Third-party imports from lib/
# ---------------------------------------------------------------------------

# yt-dlp
try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
    logHandler.log.info(
        "YTDownloader: yt_dlp loaded OK  (file: %s)"
        % getattr(yt_dlp, "__file__", "?")
    )
except ImportError as _err:
    YT_DLP_AVAILABLE = False
    logHandler.log.warning(
        "YTDownloader: yt_dlp MISSING.  sys.path=%s  error=%s"
        % (sys.path, _err)
    )

# python-vlc  (fully optional)
try:
    import vlc
    VLC_AVAILABLE = True
    logHandler.log.info("YTDownloader: python-vlc loaded OK")
except ImportError:
    VLC_AVAILABLE = False
    logHandler.log.info(
        "YTDownloader: python-vlc not found — will use system default player"
    )

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ADDON_DIR     = Path(_addon_dir)
DOWNLOADS_DIR = Path.home() / "Downloads" / "NVDA_YTDownloader"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

LIBRARY_FILE = ADDON_DIR / "library.json"


# ===========================================================================
#  VideoLibrary - JSON-backed store of downloaded video metadata
# ===========================================================================
class VideoLibrary:
    """Persists video records between sessions as a JSON file."""

    def __init__(self, path):
        self.path  = path
        self._data = []
        self.load()

    def load(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = []

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def add(self, record):
        existing = {r.get("video_id") for r in self._data}
        if record.get("video_id") not in existing:
            self._data.append(record)
            self.save()

    def all(self):
        return list(self._data)

    def by_playlist(self, playlist_id):
        return [r for r in self._data if r.get("playlist_id") == playlist_id]

    def playlists(self):
        seen = {}
        for r in self._data:
            pid = r.get("playlist_id")
            if pid and pid not in seen:
                seen[pid] = {
                    "playlist_id":    pid,
                    "playlist_title": r.get("playlist_title", "Unknown Playlist"),
                }
        return list(seen.values())

    def remove(self, video_id):
        self._data = [r for r in self._data if r.get("video_id") != video_id]
        self.save()


# ===========================================================================
#  DownloadItem - one queued URL (may expand into a whole playlist)
# ===========================================================================
class DownloadItem:

    STATUS_PENDING   = "Pending"
    STATUS_ACTIVE    = "Downloading"
    STATUS_DONE      = "Done"
    STATUS_ERROR     = "Error"
    STATUS_CANCELLED = "Cancelled"

    def __init__(self, url, download_dir, library):
        self.url          = url
        self.download_dir = download_dir
        self.library      = library
        self.status        = self.STATUS_PENDING
        self.progress      = 0
        self.title         = url
        self.error_msg     = ""
        self.track_index   = 0
        self.track_total   = 0
        self.current_track = ""
        self._last_filepath = None
        self._cancelled    = False
        self._thread       = None
        self._notify_cb    = None

    def cancel(self):
        self._cancelled = True
        self.status = self.STATUS_CANCELLED

    def start(self, notify=None, on_done=None):
        self._notify_cb = notify
        self._thread = threading.Thread(
            target=self._run, args=(on_done,), daemon=True
        )
        self._thread.start()

    def _fire(self):
        if self._notify_cb:
            wx.CallAfter(self._notify_cb)

    def status_detail(self):
        """Human-friendly status for the queue list — sounds good when read aloud."""
        if self.status == self.STATUS_ACTIVE:
            if self.track_total > 1:
                return "Video %d of %d, %d percent" % (
                    self.track_index, self.track_total, self.progress)
            return "%d percent" % self.progress
        if self.status == self.STATUS_ERROR:
            return "Error: %s" % self.error_msg[:60]
        return self.status

    def _run(self, on_done):
        self.status = self.STATUS_ACTIVE

        # Track the real file path yt_dlp chose after title sanitisation
        self._last_filepath = None

        ydl_opts = {
            # Single pre-muxed stream — no ffmpeg merge needed
            "format":         "best[ext=mp4]/best",
            "outtmpl":        str(self.download_dir
                                  / "%(playlist_id,_singles)s"
                                  / "%(id)s - %(title)s.%(ext)s"),
            "quiet":          True,
            "no_warnings":    True,
            "progress_hooks": [self._ydl_hook],
            # postprocessor hook lets us capture the actual saved filename
            "postprocessor_hooks": [self._pp_hook],
            "writeinfojson":  False,
            "ignoreerrors":   True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Fetch metadata; materialise lazy playlist generator upfront
                info = ydl.extract_info(self.url, download=False)
                if info is None:
                    raise Exception("Could not fetch video info")

                is_playlist    = info.get("_type") == "playlist"
                playlist_id    = info.get("id")    if is_playlist else None
                playlist_title = info.get("title") if is_playlist else None

                raw     = list(info.get("entries") or [info])
                entries = [e for e in raw if e is not None]

                self.title       = info.get("title", self.url)
                self.track_total = len(entries)
                self._fire()

                if self.track_total > 1:
                    wx.CallAfter(ui.message,
                        "Starting playlist: %s. %d videos."
                        % (self.title, self.track_total))
                else:
                    wx.CallAfter(ui.message,
                        "Starting download: %s" % self.title)

                for idx, entry in enumerate(entries, 1):
                    if self._cancelled:
                        break

                    self.track_index   = idx
                    self.current_track = entry.get("title", "video %d" % idx)
                    self.progress      = 0
                    self._last_filepath = None
                    self._fire()

                    if self.track_total > 1:
                        wx.CallAfter(ui.message,
                            "%d of %d: %s"
                            % (idx, self.track_total, self.current_track))

                    ydl.download([entry.get("webpage_url", self.url)])

                    # Use the real path yt_dlp reported (via _pp_hook),
                    # fall back to our prediction only if hook didn't fire
                    if self._last_filepath:
                        fpath = Path(self._last_filepath)
                    else:
                        vid_id = entry.get("id", "")
                        ext    = entry.get("ext", "mp4")
                        subdir = playlist_id or "_singles"
                        fname  = "%s - %s.%s" % (vid_id, self.current_track, ext)
                        fpath  = self.download_dir / subdir / fname

                    self.library.add({
                        "video_id":       entry.get("id", ""),
                        "title":          self.current_track,
                        "uploader":       entry.get("uploader", ""),
                        "duration":       entry.get("duration", 0),
                        "description":    (entry.get("description") or "")[:500],
                        "webpage_url":    entry.get("webpage_url", self.url),
                        "local_path":     str(fpath),
                        "playlist_id":    playlist_id,
                        "playlist_title": playlist_title,
                        "downloaded_at":  time.strftime("%Y-%m-%d %H:%M"),
                    })

            if not self._cancelled:
                self.status   = self.STATUS_DONE
                self.progress = 100

        except Exception as exc:
            self.status    = self.STATUS_ERROR
            self.error_msg = str(exc)
            logHandler.log.warning("YTDownloader: download error: %s" % exc)

        if on_done:
            wx.CallAfter(on_done, self)

    def _ydl_hook(self, d):
        if self._cancelled:
            raise Exception("Cancelled by user")
        s = d.get("status")
        if s == "downloading":
            total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
            downloaded = d.get("downloaded_bytes", 0)
            pct        = int(downloaded / total * 100)
            if pct != self.progress:
                prev          = self.progress
                self.progress = pct
                self._fire()
                for mark in (25, 50, 75):
                    if prev < mark <= pct:
                        wx.CallAfter(ui.message, "%d percent" % mark)
        elif s == "finished":
            self.progress = 99
            self._fire()

    def _pp_hook(self, d):
        """Postprocessor hook — yt_dlp calls this with the real output filepath."""
        if d.get("status") == "finished":
            fpath = d.get("info_dict", {}).get("filepath") or d.get("filepath")
            if fpath:
                self._last_filepath = fpath


# ===========================================================================
#  DownloadQueue
# ===========================================================================
class DownloadQueue:
    MAX_CONCURRENT = 2

    def __init__(self, library, download_dir):
        self.library      = library
        self.download_dir = download_dir
        self._items       = []
        self._lock        = threading.Lock()
        self._callbacks   = []

    def add_url(self, url):
        item = DownloadItem(url, self.download_dir, self.library)
        with self._lock:
            self._items.append(item)
        self._notify()
        self._schedule()
        return item

    def cancel_item(self, item):
        item.cancel()
        self._notify()

    def remove_done(self):
        terminal = (DownloadItem.STATUS_DONE,
                    DownloadItem.STATUS_CANCELLED,
                    DownloadItem.STATUS_ERROR)
        with self._lock:
            self._items = [i for i in self._items if i.status not in terminal]
        self._notify()

    def items(self):
        with self._lock:
            return list(self._items)

    def register_callback(self, cb):
        self._callbacks.append(cb)

    def _schedule(self):
        with self._lock:
            active = sum(1 for i in self._items
                         if i.status == DownloadItem.STATUS_ACTIVE)
            for item in self._items:
                if active >= self.MAX_CONCURRENT:
                    break
                if item.status == DownloadItem.STATUS_PENDING:
                    item.start(notify=self._notify, on_done=self._item_done)
                    active += 1

    def _item_done(self, item):
        self._notify()
        self._schedule()

    def _notify(self):
        for cb in self._callbacks:
            try:
                wx.CallAfter(cb)
            except Exception:
                pass



# ===========================================================================
#  StatusDialog - live download status window (NVDA+Shift+S)
# ===========================================================================
class StatusDialog(wx.Dialog):
    """
    Persistent status window showing all active and recent downloads.
    Can stay open while the user does other things.
    NVDA reads the list naturally: title, then status detail on each row.
    """

    def __init__(self, parent, queue):
        super().__init__(
            parent, title="YouTube Downloader - Status",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.STAY_ON_TOP
        )
        self.queue  = queue
        self._items = []
        queue.register_callback(self._refresh)
        self._build_ui()
        self._refresh()
        self.SetSize((660, 360))
        self.CentreOnScreen()
        # Poll every second as safety net
        self._poll = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self._refresh(), self._poll)
        self._poll.Start(1000)
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _build_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        h = wx.StaticText(panel, label="Download Status")
        h.SetFont(wx.Font(13, wx.FONTFAMILY_DEFAULT,
                          wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(h, 0, wx.ALL, 8)

        # Main status list — NVDA reads row by row
        self.list_ctrl = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list_ctrl.InsertColumn(0, "Title",   width=260)
        self.list_ctrl.InsertColumn(1, "Status",  width=200)
        self.list_ctrl.InsertColumn(2, "Track",   width=160)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        # Summary line — e.g. "2 downloading, 1 done, 0 errors"
        self.lbl_summary = wx.StaticText(panel, label="")
        sizer.Add(self.lbl_summary, 0, wx.LEFT | wx.BOTTOM, 8)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        btn_cancel = wx.Button(panel, label="&Cancel selected")
        btn_clear  = wx.Button(panel, label="Clear &finished")
        btn_close  = wx.Button(panel, id=wx.ID_CLOSE, label="C&lose")
        btn_cancel.Bind(wx.EVT_BUTTON, self._on_cancel)
        btn_clear.Bind(wx.EVT_BUTTON,  self._on_clear)
        btn_close.Bind(wx.EVT_BUTTON,  lambda e: self.Close())
        btns.Add(btn_cancel, 0, wx.RIGHT, 6)
        btns.Add(btn_clear,  0, wx.RIGHT, 6)
        btns.AddStretchSpacer()
        btns.Add(btn_close, 0)
        sizer.Add(btns, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

    def _refresh(self):
        items = self.queue.items()
        self.list_ctrl.DeleteAllItems()
        active = done = errors = 0
        for idx, item in enumerate(items):
            row = self.list_ctrl.InsertItem(idx, item.title)
            self.list_ctrl.SetItem(row, 1, item.status_detail())
            # Show current track if it's a playlist
            if item.track_total > 1 and item.current_track:
                self.list_ctrl.SetItem(row, 2,
                    "%d of %d: %s" % (item.track_index,
                                      item.track_total,
                                      item.current_track[:40]))
            else:
                self.list_ctrl.SetItem(row, 2, item.current_track[:50])
            if item.status == DownloadItem.STATUS_ACTIVE:
                active += 1
            elif item.status == DownloadItem.STATUS_DONE:
                done += 1
            elif item.status == DownloadItem.STATUS_ERROR:
                errors += 1
        self._items = items
        self.lbl_summary.SetLabel(
            "%d downloading,  %d done,  %d errors" % (active, done, errors))

    def _selected_item(self):
        idx = self.list_ctrl.GetFirstSelected()
        if 0 <= idx < len(self._items):
            return self._items[idx]
        return None

    def _on_cancel(self, event):
        item = self._selected_item()
        if item:
            self.queue.cancel_item(item)

    def _on_clear(self, event):
        self.queue.remove_done()

    def _on_close(self, event):
        self._poll.Stop()
        self.Destroy()

# ===========================================================================
#  QueueDialog
# ===========================================================================
class QueueDialog(wx.Dialog):

    def __init__(self, parent, queue):
        super().__init__(
            parent, title="YouTube Downloader - Download Queue",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        self.queue  = queue
        self._items = []
        queue.register_callback(self._refresh)
        self._build_ui()
        self._refresh()
        self.SetSize((700, 450))
        self.CentreOnScreen()
        self._poll = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self._refresh(), self._poll)
        self._poll.Start(1000)
        self.Bind(wx.EVT_CLOSE, self._on_close_queue)

    def _on_close_queue(self, event):
        self._poll.Stop()
        self.Destroy()

    def _build_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        heading = wx.StaticText(panel, label="Download Queue")
        heading.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT,
                                wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(heading, 0, wx.ALL, 8)

        url_row = wx.BoxSizer(wx.HORIZONTAL)
        self.url_ctrl = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.url_ctrl.SetHint("Paste a YouTube URL or playlist URL here...")
        self.url_ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_add)
        add_btn  = wx.Button(panel, label="&Add to Queue")
        clip_btn = wx.Button(panel, label="Add from &Clipboard")
        add_btn.Bind(wx.EVT_BUTTON,  self._on_add)
        clip_btn.Bind(wx.EVT_BUTTON, self._on_clip)
        url_row.Add(self.url_ctrl, 1, wx.EXPAND | wx.RIGHT, 4)
        url_row.Add(add_btn,  0, wx.RIGHT, 4)
        url_row.Add(clip_btn, 0)
        sizer.Add(url_row, 0, wx.EXPAND | wx.ALL, 8)

        self.list_ctrl = wx.ListCtrl(panel,
                                     style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list_ctrl.InsertColumn(0, "Title",    width=320)
        self.list_ctrl.InsertColumn(1, "Status",   width=110)
        self.list_ctrl.InsertColumn(2, "Progress", width=80)
        self.list_ctrl.InsertColumn(3, "URL",      width=160)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        cancel_btn = wx.Button(panel, label="&Cancel Selected")
        clear_btn  = wx.Button(panel, label="Clear &Finished")
        close_btn  = wx.Button(panel, id=wx.ID_CLOSE, label="C&lose")
        cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel)
        clear_btn.Bind(wx.EVT_BUTTON,  self._on_clear)
        close_btn.Bind(wx.EVT_BUTTON,  lambda e: self.Close())
        btn_row.Add(cancel_btn, 0, wx.RIGHT, 6)
        btn_row.Add(clear_btn,  0, wx.RIGHT, 6)
        btn_row.AddStretchSpacer()
        btn_row.Add(close_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

    def _refresh(self):
        self._items = self.queue.items()
        self.list_ctrl.DeleteAllItems()
        for idx, item in enumerate(self._items):
            row = self.list_ctrl.InsertItem(idx, item.title)
            detail = item.status_detail() if hasattr(item, "status_detail") else item.status
            self.list_ctrl.SetItem(row, 1, detail)
            self.list_ctrl.SetItem(row, 2, "%d%%" % item.progress)
            self.list_ctrl.SetItem(row, 3, item.url[:60])

    def _selected_item(self):
        idx = self.list_ctrl.GetFirstSelected()
        if idx == -1 or idx >= len(self._items):
            return None
        return self._items[idx]

    def _on_add(self, event):
        url = self.url_ctrl.GetValue().strip()
        if url:
            self.queue.add_url(url)
            self.url_ctrl.Clear()
            ui.message("Added to queue: %s" % url[:60])

    def _on_clip(self, event):
        url = _clipboard_text()
        if url:
            self.queue.add_url(url)
            ui.message("Added from clipboard: %s" % url[:60])
        else:
            wx.MessageBox("Clipboard is empty or contains no text.",
                          "Info", wx.OK | wx.ICON_INFORMATION, self)

    def _on_cancel(self, event):
        item = self._selected_item()
        if item:
            self.queue.cancel_item(item)

    def _on_clear(self, event):
        self.queue.remove_done()


# ===========================================================================
#  PlayerDialog - accessible YouTube-like library + player
# ===========================================================================
class PlayerDialog(wx.Dialog):
    """
    Screen-reader friendly layout:
      H1 "Your Video Library"
        H2 "Playlists / Collections"  -> ListBox
        H2 "Videos"                   -> ListBox (Enter or double-click plays)
        H2 "Now Playing"              -> title, uploader, duration, description
          Transport: Play | Pause | Stop | -10s | +10s | Volume

    Playback:
      - If python-vlc is available: plays inside the dialog with full transport.
      - If not: opens the file in whatever media player the user has set as
        default (Windows Media Player, VLC app, AIMP, Foobar2000, etc.).
        Pause/seek controls are hidden in that mode since we don't control
        the external app.
    """

    def __init__(self, parent, library):
        super().__init__(
            parent, title="YouTube Downloader - Video Library & Player",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        self.library         = library
        self._player         = None
        self._current_record = None
        self._records        = []
        self._playlists      = []
        self._build_ui()
        self._populate_library()
        self.SetSize((820, 620))
        self.CentreOnScreen()
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _build_ui(self):
        panel = wx.Panel(self)
        root  = wx.BoxSizer(wx.VERTICAL)

        # H1
        h1 = wx.StaticText(panel, label="Your Video Library")
        h1.SetFont(wx.Font(16, wx.FONTFAMILY_DEFAULT,
                           wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        root.Add(h1, 0, wx.ALL, 10)

        split = wx.BoxSizer(wx.HORIZONTAL)

        # ---- LEFT: browser ------------------------------------------------
        left = wx.BoxSizer(wx.VERTICAL)

        h2_pl = wx.StaticText(panel, label="Playlists / Collections")
        h2_pl.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT,
                              wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        left.Add(h2_pl, 0, wx.LEFT | wx.TOP, 4)

        self.playlist_list = wx.ListBox(panel, style=wx.LB_SINGLE)
        self.playlist_list.Bind(wx.EVT_LISTBOX, self._on_playlist_select)
        left.Add(self.playlist_list, 1, wx.EXPAND | wx.ALL, 4)

        h2_vid = wx.StaticText(panel, label="Videos  (Enter or double-click to play)")
        h2_vid.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT,
                               wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        left.Add(h2_vid, 0, wx.LEFT | wx.TOP, 4)

        self.video_list = wx.ListBox(panel, style=wx.LB_SINGLE)
        self.video_list.Bind(wx.EVT_LISTBOX,        self._on_video_select)
        self.video_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_video_play)
        self.video_list.Bind(wx.EVT_KEY_DOWN,       self._on_video_key)
        self.video_list.Bind(wx.EVT_CHAR_HOOK,      self._on_video_key)
        left.Add(self.video_list, 2, wx.EXPAND | wx.ALL, 4)

        split.Add(left, 1, wx.EXPAND | wx.RIGHT, 8)

        # ---- RIGHT: player ------------------------------------------------
        right = wx.BoxSizer(wx.VERTICAL)

        h2_np = wx.StaticText(panel, label="Now Playing")
        h2_np.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT,
                              wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        right.Add(h2_np, 0, wx.TOP | wx.LEFT, 4)

        self.lbl_title = wx.StaticText(panel, label="No video selected")
        self.lbl_title.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT,
                                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.lbl_title.Wrap(360)
        right.Add(self.lbl_title, 0, wx.ALL, 6)

        self.lbl_meta = wx.StaticText(panel, label="")
        right.Add(self.lbl_meta, 0, wx.LEFT | wx.BOTTOM, 6)

        right.Add(wx.StaticText(panel, label="Description:"), 0, wx.LEFT, 4)
        self.txt_desc = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
            size=(-1, 110)
        )
        right.Add(self.txt_desc, 0, wx.EXPAND | wx.ALL, 4)

        # Seek slider - only meaningful with VLC
        self.seek_label = wx.StaticText(panel, label="Seek:")
        right.Add(self.seek_label, 0, wx.LEFT, 4)
        self.seek_slider = wx.Slider(panel, minValue=0, maxValue=1000,
                                     style=wx.SL_HORIZONTAL | wx.SL_LABELS)
        self.seek_slider.Bind(wx.EVT_SLIDER, self._on_seek)
        right.Add(self.seek_slider, 0, wx.EXPAND | wx.ALL, 4)

        # Transport buttons
        transport = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_play  = wx.Button(panel, label="Play  [Enter]")
        self.btn_pause = wx.Button(panel, label="Pause")
        self.btn_stop  = wx.Button(panel, label="Stop")
        self.btn_rew   = wx.Button(panel, label="-10 sec")
        self.btn_fwd   = wx.Button(panel, label="+10 sec")
        self.btn_play.Bind(wx.EVT_BUTTON,  self._on_play)
        self.btn_pause.Bind(wx.EVT_BUTTON, self._on_pause)
        self.btn_stop.Bind(wx.EVT_BUTTON,  self._on_stop)
        self.btn_rew.Bind(wx.EVT_BUTTON,   lambda e: self._seek_relative(-10))
        self.btn_fwd.Bind(wx.EVT_BUTTON,   lambda e: self._seek_relative(10))
        for w in (self.btn_play, self.btn_pause, self.btn_stop,
                  self.btn_rew, self.btn_fwd):
            transport.Add(w, 0, wx.RIGHT, 4)
        right.Add(transport, 0, wx.ALL, 6)

        # Volume
        vol_row = wx.BoxSizer(wx.HORIZONTAL)
        vol_row.Add(wx.StaticText(panel, label="Volume:"), 0,
                    wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.vol_slider = wx.Slider(panel, value=80, minValue=0, maxValue=100,
                                    style=wx.SL_HORIZONTAL)
        self.vol_slider.Bind(wx.EVT_SLIDER, self._on_volume)
        vol_row.Add(self.vol_slider, 1, wx.EXPAND)
        right.Add(vol_row, 0, wx.EXPAND | wx.ALL, 4)

        # Playback mode label
        if VLC_AVAILABLE:
            mode_text = "Playback: in-dialog via VLC"
            mode_color = wx.Colour(0, 120, 0)
        else:
            mode_text  = "Playback: system default media player (VLC not found)"
            mode_color = wx.Colour(100, 100, 100)
            # Hide VLC-only controls
            for w in (self.btn_pause, self.btn_stop,
                      self.btn_rew, self.btn_fwd,
                      self.seek_slider, self.seek_label):
                w.Hide()

        mode_lbl = wx.StaticText(panel, label=mode_text)
        mode_lbl.SetForegroundColour(mode_color)
        right.Add(mode_lbl, 0, wx.ALL, 4)

        btn_open_folder = wx.Button(panel, label="Open Downloads Folder")
        btn_open_folder.Bind(wx.EVT_BUTTON, lambda e: _open_folder(DOWNLOADS_DIR))
        right.Add(btn_open_folder, 0, wx.ALL, 4)

        split.Add(right, 1, wx.EXPAND)
        root.Add(split, 1, wx.EXPAND | wx.ALL, 8)

        close_btn = wx.Button(panel, id=wx.ID_CLOSE, label="Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        root.Add(close_btn, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

        # Timer for seek slider updates
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._update_seek, self._timer)
        self._timer.Start(500)

    # ---- library -----------------------------------------------------------

    def _populate_library(self):
        self.playlist_list.Clear()
        self.playlist_list.Append("All Videos")
        playlists = self.library.playlists()
        for pl in playlists:
            self.playlist_list.Append(pl["playlist_title"])
        self._playlists = [None] + playlists
        self.playlist_list.SetSelection(0)
        self._load_videos(None)

    def _load_videos(self, playlist_id):
        self.video_list.Clear()
        records = (self.library.all() if playlist_id is None
                   else self.library.by_playlist(playlist_id))
        self._records = records
        for r in records:
            dur = _fmt_duration(r.get("duration", 0))
            self.video_list.Append("%s  [%s]" % (r["title"], dur))

    # ---- events: browsing --------------------------------------------------

    def _on_playlist_select(self, event):
        idx = self.playlist_list.GetSelection()
        pl  = self._playlists[idx] if idx < len(self._playlists) else None
        pid = pl["playlist_id"] if pl else None
        self._load_videos(pid)
        if self._records:
            self.video_list.SetSelection(0)
            self._show_record(self._records[0])
        ui.message(self.playlist_list.GetStringSelection())

    def _on_video_select(self, event):
        idx = self.video_list.GetSelection()
        if 0 <= idx < len(self._records):
            self._show_record(self._records[idx])
            ui.message(self._records[idx]["title"])

    def _on_video_play(self, event):
        idx = self.video_list.GetSelection()
        if 0 <= idx < len(self._records):
            self._play_record(self._records[idx])

    def _on_video_key(self, event):
        kc = event.GetKeyCode()
        if kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_video_play(event)
            # Don't skip — consume the event so it doesn't trigger anything else
        else:
            event.Skip()

    # ---- metadata display --------------------------------------------------

    def _show_record(self, record):
        self._current_record = record
        self.lbl_title.SetLabel(record.get("title", "Unknown"))
        self.lbl_title.Wrap(360)
        uploader = record.get("uploader", "Unknown")
        dur      = _fmt_duration(record.get("duration", 0))
        self.lbl_meta.SetLabel("By: %s     Duration: %s" % (uploader, dur))
        desc = record.get("description", "")
        self.txt_desc.SetValue(desc or "(No description available)")
        self.Layout()

    # ---- playback ----------------------------------------------------------

    def _play_record(self, record):
        self._show_record(record)
        path = record.get("local_path", "")
        if not path or not Path(path).exists():
            wx.MessageBox(
                "File not found:\n%s\n\nThe video may not have finished downloading yet." % path,
                "File Not Found", wx.OK | wx.ICON_WARNING, self
            )
            return
        if VLC_AVAILABLE:
            self._vlc_play(path)
        else:
            _open_with_system(path)
            ui.message(
                "Opening in your default media player: %s"
                % record.get("title", "video")
            )

    def _vlc_play(self, path):
        try:
            if self._player:
                self._player.stop()
            instance     = vlc.Instance("--no-xlib", "--no-video", "--quiet")
            self._player = instance.media_player_new()
            media        = instance.media_new(path)
            self._player.set_media(media)
            self._player.audio_set_volume(self.vol_slider.GetValue())
            self._player.play()
            ui.message("Playing: %s" % (self._current_record or {}).get("title", "video"))
        except Exception as exc:
            wx.MessageBox("VLC error: %s" % exc, "Playback Error",
                          wx.OK | wx.ICON_ERROR, self)

    def _on_play(self, event):
        if self._player:
            self._player.play()
        elif self._current_record:
            self._play_record(self._current_record)

    def _on_pause(self, event):
        if self._player:
            self._player.pause()
            ui.message("Paused")

    def _on_stop(self, event):
        if self._player:
            self._player.stop()
            ui.message("Stopped")

    def _seek_relative(self, seconds):
        if self._player:
            cur     = self._player.get_time()
            new_t   = max(0, cur + seconds * 1000)
            self._player.set_time(new_t)
            # Announce new position so user knows where they are
            wx.CallAfter(ui.message, _fmt_duration(new_t // 1000))

    def _on_seek(self, event):
        if self._player:
            total_ms = self._player.get_length()
            if total_ms > 0:
                pos = self.seek_slider.GetValue() / 1000.0
                self._player.set_time(int(total_ms * pos))

    def _on_volume(self, event):
        if self._player:
            self._player.audio_set_volume(self.vol_slider.GetValue())

    def _update_seek(self, event):
        if self._player and self._player.is_playing():
            self.seek_slider.SetValue(int(self._player.get_position() * 1000))

    def _on_close(self, event):
        self._timer.Stop()
        if self._player:
            self._player.stop()
        self.Destroy()


# ===========================================================================
#  Utility helpers
# ===========================================================================

def _clipboard_text():
    text = ""
    if wx.TheClipboard.Open():
        if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_TEXT)):
            data = wx.TextDataObject()
            wx.TheClipboard.GetData(data)
            text = data.GetText().strip()
        wx.TheClipboard.Close()
    return text


def _fmt_duration(seconds):
    seconds = int(seconds or 0)
    h, rem  = divmod(seconds, 3600)
    m, s    = divmod(rem, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


def _open_with_system(path):
    """Hand a file off to whatever the OS has set as the default media player."""
    try:
        os.startfile(path)                   # Windows
    except AttributeError:
        subprocess.Popen(["xdg-open", path]) # Linux


def _open_folder(folder):
    try:
        os.startfile(str(folder))
    except AttributeError:
        subprocess.Popen(["xdg-open", str(folder)])


# ===========================================================================
#  GlobalPlugin - NVDA entry point
# ===========================================================================
class GlobalPlugin(globalPluginHandler.GlobalPlugin):

    __gestures = {
        "kb:NVDA+shift+y": "addFromClipboard",
        "kb:NVDA+shift+q": "openQueue",
        "kb:NVDA+shift+p": "openPlayer",
        "kb:NVDA+shift+o": "openStatus",
    }

    def __init__(self):
        super().__init__()

        if not YT_DLP_AVAILABLE:
            ui.message(
                "YouTube Downloader: yt-dlp not found. "
                "Check that the yt_dlp folder is inside your addon's lib folder, "
                "then reload NVDA. See NVDA log for exact paths searched."
            )

        self._library    = VideoLibrary(LIBRARY_FILE)
        self._queue      = DownloadQueue(self._library, DOWNLOADS_DIR)
        self._queue_dlg  = None
        self._player_dlg = None
        self._status_dlg = None

    def script_addFromClipboard(self, gesture):
        if not YT_DLP_AVAILABLE:
            ui.message("yt-dlp is not installed. Cannot download.")
            return
        url = _clipboard_text()
        if not url:
            ui.message("Clipboard is empty.")
            return
        if "youtube.com" not in url and "youtu.be" not in url:
            ui.message("That doesn't look like a YouTube URL: %s" % url[:60])
            return
        self._queue.add_url(url)
        ui.message("Added to queue: %s" % url[:60])

    script_addFromClipboard.__doc__ = \
        "Add YouTube URL from clipboard to download queue"

    def script_openQueue(self, gesture):
        if self._queue_dlg and self._queue_dlg.IsShown():
            self._queue_dlg.Raise()
            return
        self._queue_dlg = QueueDialog(None, self._queue)
        self._queue_dlg.Show()

    script_openQueue.__doc__ = "Open YouTube Downloader queue dialog"

    def script_openPlayer(self, gesture):
        if self._player_dlg and self._player_dlg.IsShown():
            self._player_dlg.Raise()
            return
        self._player_dlg = PlayerDialog(None, self._library)
        self._player_dlg.Show()

    script_openPlayer.__doc__ = "Open YouTube Downloader video library and player"

    def script_openStatus(self, gesture):
        if self._status_dlg and self._status_dlg.IsShown():
            self._status_dlg.Raise()
            return
        self._status_dlg = StatusDialog(None, self._queue)
        self._status_dlg.Show()

    script_openStatus.__doc__ = "Open YouTube Downloader status window"

    def terminate(self):
        for dlg in (self._queue_dlg, self._player_dlg, self._status_dlg):
            if dlg:
                try:
                    dlg.Destroy()
                except Exception:
                    pass
        super().terminate()