import json
import os
import random
import shutil
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import QSettings, Qt, QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QDoubleSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QCheckBox,
)

try:
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import ContentDetector
except Exception:
    SceneManager = None
    open_video = None
    ContentDetector = None

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def resolve_tool_binary(tool_name: str) -> str:
    candidates = []
    exe_name = f"{tool_name}.exe" if os.name == "nt" else tool_name
    base = Path(sys.argv[0]).resolve().parent
    candidates.append(base / exe_name)
    candidates.append(Path.cwd() / exe_name)
    for c in candidates:
        if c.exists():
            return str(c)
    found = shutil.which(tool_name)
    if found:
        return found
    found_exe = shutil.which(exe_name)
    if found_exe:
        return found_exe
    raise FileNotFoundError(f"Không tìm thấy {exe_name}. Hãy đặt cạnh file chạy hoặc thêm vào PATH.")


@dataclass
class SettingsData:
    scene_sensitivity: float = 27.0
    image_focus: str = "center"
    image_height_percent: float = 35.0
    overlap_percent: float = 5.0
    fade_curve: str = "linear"
    auto_open_output: bool = True


class ProcessRunner:
    def __init__(self, log_cb, stop_flag):
        self.log_cb = log_cb
        self.stop_flag = stop_flag
        self.running: List[subprocess.Popen] = []

    def kill_all(self):
        for p in self.running[:]:
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass

    def run(self, cmd: List[str], cwd: Optional[Path] = None):
        if self.stop_flag():
            raise RuntimeError("Đã dừng bởi người dùng")
        self.log_cb("$ " + " ".join(shlex.quote(x) for x in cmd))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(cwd) if cwd else None)
        self.running.append(p)
        try:
            for line in p.stdout:
                self.log_cb(line.rstrip())
                if self.stop_flag() and p.poll() is None:
                    p.kill()
                    raise RuntimeError("Đã dừng bởi người dùng")
            code = p.wait()
        finally:
            if p in self.running:
                self.running.remove(p)
        if code != 0:
            raise RuntimeError(f"Lệnh lỗi: {' '.join(cmd)}")


def ffprobe_size(ffprobe_bin: str, video: Path) -> Tuple[int, int]:
    cmd = [ffprobe_bin, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(video)]
    out = subprocess.check_output(cmd, text=True)
    data = json.loads(out)
    s = data["streams"][0]
    return int(s["width"]), int(s["height"])


def detect_scenes(video_path: Path, threshold: float) -> List[Tuple[float, float]]:
    if SceneManager is None:
        return []
    try:
        v = open_video(str(video_path))
        sm = SceneManager()
        sm.add_detector(ContentDetector(threshold=threshold))
        sm.detect_scenes(v)
        scenes = sm.get_scene_list()
        return [(a.get_seconds(), b.get_seconds()) for a, b in scenes]
    except Exception:
        return []


def fallback_segments(duration: float) -> List[Tuple[float, float]]:
    segs = []
    t = 0.0
    while t < duration:
        l = random.uniform(3, 5)
        end = min(duration, t + l)
        segs.append((t, end))
        t = end
    return segs


def ffprobe_duration(ffprobe_bin: str, video: Path) -> float:
    cmd = [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video)]
    return float(subprocess.check_output(cmd, text=True).strip())


def y_for_focus(focus: str) -> str:
    if focus == "top":
        return "0"
    if focus == "bottom":
        return "ih-oh"
    return "(ih-oh)/2"


def fade_alpha_expr(curve: str, overlap_h: int) -> str:
    if curve == "smooth":
        return f"255*pow(1-Y/{overlap_h},1.5)"
    if curve == "strong":
        return f"255*pow(1-Y/{overlap_h},2)"
    return f"255*(1-Y/{overlap_h})"


class Worker(QThread):
    log = Signal(str)
    progress = Signal(int)
    done = Signal(str)

    def __init__(self, videos, images, outdir, settings: SettingsData, ffmpeg_bin, ffprobe_bin):
        super().__init__()
        self.videos = videos
        self.images = images
        self.outdir = Path(outdir)
        self.settings = settings
        self.ffmpeg_bin = ffmpeg_bin
        self.ffprobe_bin = ffprobe_bin
        self._stop = False
        self.runner = ProcessRunner(self.log.emit, self.is_stopped)

    def is_stopped(self):
        return self._stop

    def stop_now(self):
        self._stop = True
        self.runner.kill_all()

    def run(self):
        ok = 0
        for i, vp in enumerate(self.videos):
            if self._stop:
                break
            try:
                self.process_one(Path(vp))
                ok += 1
            except Exception as e:
                self.log.emit(f"[LỖI] {vp}: {e}")
            self.progress.emit(int((i + 1) * 100 / max(1, len(self.videos))))
        self.done.emit(f"Hoàn tất: {ok}/{len(self.videos)} video")

    def process_one(self, video: Path):
        if not self.images:
            raise RuntimeError("Chưa có ảnh")
        image = Path(random.choice(self.images))
        stem = video.stem
        safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem).strip("_") or "video"
        video_outdir = self.outdir / safe_stem
        video_outdir.mkdir(parents=True, exist_ok=True)
        out_final = video_outdir / f"{safe_stem}_processed.mp4"
        with tempfile.TemporaryDirectory(prefix="autosf_") as td:
            td = Path(td)
            audio = td / "audio.aac"
            self.runner.run([self.ffmpeg_bin, "-y", "-i", str(video), "-vn", "-acodec", "copy", str(audio)])

            scenes = detect_scenes(video, self.settings.scene_sensitivity)
            if len(scenes) <= 1:
                duration = ffprobe_duration(self.ffprobe_bin, video)
                scenes = fallback_segments(duration)

            seg_dir = td / "segs"
            seg_dir.mkdir()
            segs = []
            for idx, (s, e) in enumerate(scenes):
                seg = seg_dir / f"seg_{idx:04d}.mp4"
                self.runner.run([self.ffmpeg_bin, "-y", "-ss", f"{s:.3f}", "-to", f"{e:.3f}", "-i", str(video), "-c", "copy", str(seg)])
                segs.append(seg)

            if len(segs) > 1:
                first = segs[0]
                rest = segs[1:]
                random.shuffle(rest)
                segs = [first] + rest

            lst = td / "concat.txt"
            lst.write_text("\n".join(f"file '{p.as_posix()}'" for p in segs), encoding="utf-8")
            shuffled = td / "shuffled.mp4"
            self.runner.run([self.ffmpeg_bin, "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(shuffled)])

            W, H = ffprobe_size(self.ffprobe_bin, shuffled)
            ih = int(round(self.settings.image_height_percent / 100.0 * H))
            ov = int(round(self.settings.overlap_percent / 100.0 * H))
            ov = min(ov, ih)
            visible_video_total = H - (ih - ov)
            offset_y = -(H - visible_video_total)
            image_top = H - ih
            fade_start = image_top
            main_video_h = max(1, int(visible_video_total - ov))
            alpha = fade_alpha_expr(self.settings.fade_curve, max(1, ov))
            crop_y = y_for_focus(self.settings.image_focus)

            composed = td / "composed.mp4"
            fc = (
                f"[1:v]scale={W}:-1,crop={W}:{ih}:0:{crop_y}[img];"
                f"[0:v]setpts=PTS-STARTPTS[v];"
                f"color=c=black:s={W}x{H}:d=1[base];"
                f"[base][img]overlay=0:{image_top}[base_img];"
                f"[base][v]overlay=0:{offset_y}[vp];"
                f"[vp]crop={W}:{main_video_h}:0:0[v_main];"
                f"[base_img][v_main]overlay=0:0[tmp];"
                f"[vp]crop={W}:{max(1,ov)}:0:{image_top},format=yuva420p,geq=lum='p(X,Y)':a='{alpha}'[fade];"
                f"[tmp][fade]overlay=0:{fade_start}[outv]"
            )
            self.runner.run([
                self.ffmpeg_bin, "-y", "-i", str(shuffled), "-loop", "1", "-i", str(image),
                "-filter_complex", fc,
                "-map", "[outv]", "-t", f"{ffprobe_duration(self.ffprobe_bin, shuffled):.3f}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(composed)
            ])

            try:
                self.runner.run([self.ffmpeg_bin, "-y", "-i", str(composed), "-i", str(audio), "-c:v", "copy", "-c:a", "aac", "-shortest", str(out_final)])
            except Exception:
                self.runner.run([self.ffmpeg_bin, "-y", "-i", str(composed), "-i", str(audio), "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out_final)])


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Auto Video Shuffle + Image Compositor")
        self.settings = QSettings("autoshuffle", "app")
        self.worker = None
        self.ffmpeg_bin = ""
        self.ffprobe_bin = ""
        self.last_output_used = ""
        self.setup_ui()

    def setup_ui(self):
        w = QWidget()
        v = QVBoxLayout(w)

        row = QHBoxLayout()
        self.video_list = QListWidget()
        self.image_list = QListWidget()
        row.addWidget(self.video_list)
        row.addWidget(self.image_list)
        v.addLayout(row)

        btn = QHBoxLayout()
        self.btn_add_video = QPushButton("Thêm video")
        self.btn_add_image = QPushButton("Thêm ảnh")
        self.btn_remove = QPushButton("Xóa")
        self.btn_start = QPushButton("Bắt đầu")
        self.btn_stop = QPushButton("Dừng")
        self.btn_kill = QPushButton("Kết thúc")
        self.btn_pick_output = QPushButton("Chọn output (tuỳ chọn)")
        self.btn_output = QPushButton("Mở thư mục output")
        for b in [self.btn_add_video, self.btn_add_image, self.btn_remove, self.btn_pick_output, self.btn_start, self.btn_stop, self.btn_kill, self.btn_output]:
            btn.addWidget(b)
        v.addLayout(btn)

        grid = QGridLayout()
        self.scene_spin = QDoubleSpinBox(); self.scene_spin.setRange(1, 100); self.scene_spin.setValue(27)
        self.focus_combo = QComboBox(); self.focus_combo.addItems(["center", "top", "bottom"])
        self.ih_spin = QDoubleSpinBox(); self.ih_spin.setRange(1, 90); self.ih_spin.setValue(35)
        self.ov_spin = QDoubleSpinBox(); self.ov_spin.setRange(0, 90); self.ov_spin.setValue(5)
        self.fade_combo = QComboBox(); self.fade_combo.addItems(["linear", "smooth", "strong"])
        self.auto_open = QCheckBox("Tự mở thư mục output")
        self.auto_open.setChecked(True)

        grid.addWidget(QLabel("Độ nhạy scene"), 0, 0); grid.addWidget(self.scene_spin, 0, 1)
        grid.addWidget(QLabel("Crop ảnh"), 0, 2); grid.addWidget(self.focus_combo, 0, 3)
        grid.addWidget(QLabel("Chiều cao ảnh (%)"), 1, 0); grid.addWidget(self.ih_spin, 1, 1)
        grid.addWidget(QLabel("Overlap (%)"), 1, 2); grid.addWidget(self.ov_spin, 1, 3)
        grid.addWidget(QLabel("Fade kiểu"), 2, 0); grid.addWidget(self.fade_combo, 2, 1)
        grid.addWidget(self.auto_open, 2, 2, 1, 2)
        v.addLayout(grid)

        self.progress = QProgressBar()
        self.log = QTextEdit(); self.log.setReadOnly(True)
        v.addWidget(self.progress); v.addWidget(self.log)

        self.setCentralWidget(w)

        self.btn_add_video.clicked.connect(self.add_video)
        self.btn_add_image.clicked.connect(self.add_image)
        self.btn_remove.clicked.connect(self.remove_items)
        self.btn_pick_output.clicked.connect(self.pick_output_folder)
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_kill.clicked.connect(self.kill_now)
        self.btn_output.clicked.connect(self.open_output)

    def append_log(self, text):
        self.log.append(text)

    def add_video(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Chọn video", "", "Video (*.mp4 *.mov *.avi *.mkv)")
        for f in files:
            self.video_list.addItem(f)

    def add_image(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Chọn ảnh", "", "Image (*.jpg *.jpeg *.png *.webp *.bmp)")
        for f in files:
            self.image_list.addItem(f)

    def remove_items(self):
        selected = self.video_list.selectedItems() + self.image_list.selectedItems()
        if selected:
            for it in selected:
                it.listWidget().takeItem(it.listWidget().row(it))
            return
        self.video_list.clear(); self.image_list.clear()

    def get_output(self):
        p = self.settings.value("last_openable_output", "", str)
        if p and Path(p).exists():
            return p
        d = Path.cwd() / "output_processed"
        d.mkdir(exist_ok=True)
        return str(d)

    def pick_output_folder(self):
        chosen = QFileDialog.getExistingDirectory(self, "Chọn thư mục output", self.get_output())
        if chosen:
            self.settings.setValue("last_openable_output", chosen)
            self.last_output_used = chosen
            self.append_log(f"Đã chọn output: {chosen}")

    def start_run(self):
        videos = [self.video_list.item(i).text() for i in range(self.video_list.count())]
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Đang chạy", "Batch đang chạy, vui lòng dừng trước khi chạy mới")
            return
        images = [self.image_list.item(i).text() for i in range(self.image_list.count())]
        if not videos:
            QMessageBox.warning(self, "Thiếu dữ liệu", "Bạn chưa thêm video")
            return
        if not images:
            QMessageBox.warning(self, "Thiếu dữ liệu", "Bạn chưa thêm ảnh")
            return
        outdir = self.get_output()
        self.settings.setValue("last_openable_output", outdir)
        self.last_output_used = outdir
        try:
            self.ffmpeg_bin = resolve_tool_binary("ffmpeg")
            self.ffprobe_bin = resolve_tool_binary("ffprobe")
        except FileNotFoundError as e:
            QMessageBox.critical(self, "Thiếu công cụ", str(e))
            self.append_log(f"[LỖI] {e}")
            return

        if self.ov_spin.value() > self.ih_spin.value():
            QMessageBox.warning(self, "Sai cấu hình", "Overlap (%) phải <= Chiều cao ảnh (%)")
            return

        conf = SettingsData(
            scene_sensitivity=self.scene_spin.value(),
            image_focus=self.focus_combo.currentText(),
            image_height_percent=self.ih_spin.value(),
            overlap_percent=self.ov_spin.value(),
            fade_curve=self.fade_combo.currentText(),
            auto_open_output=self.auto_open.isChecked(),
        )
        self.worker = Worker(videos, images, outdir, conf, self.ffmpeg_bin, self.ffprobe_bin)
        self.worker.log.connect(self.append_log)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.done.connect(self.on_done)
        self.worker.start()

    def stop_run(self):
        if self.worker:
            self.worker.stop_now()

    def kill_now(self):
        if self.worker:
            self.worker.stop_now()
            self.append_log("Đã kill toàn bộ tiến trình ffmpeg")

    def on_done(self, msg):
        self.append_log(msg)
        if self.auto_open.isChecked():
            self.open_output()

    def open_output(self):
        path = self.last_output_used or self.get_output()
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    mw = MainWindow()
    mw.resize(1200, 700)
    mw.show()
    sys.exit(app.exec())
