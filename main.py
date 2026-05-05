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

from PySide6.QtCore import QSettings, Qt, QThread, Signal, QUrl, QPoint
from PySide6.QtGui import QDesktopServices, QIcon, QPainter, QColor, QPixmap, QPolygon
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
    QStyle,
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

    def run(self, cmd: List[str], cwd: Optional[Path] = None, step_name: Optional[str] = None):
        if self.stop_flag():
            raise RuntimeError("Đã dừng bởi người dùng")
        if step_name:
            self.log_cb(f"▶ {step_name}")
        self.log_cb("   ↳ " + " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(cwd) if cwd else None)
        self.running.append(p)
        lines = []
        progress_tick = 0
        try:
            for line in p.stdout:
                txt = line.rstrip()
                if len(lines) > 80:
                    lines.pop(0)
                lines.append(txt)
                if "time=" in txt or "frame=" in txt or "speed=" in txt:
                    progress_tick += 1
                    if progress_tick % 30 == 0:
                        self.log_cb("   ... " + txt[:180])
                elif "Error" in txt or "Invalid" in txt:
                    self.log_cb("   ! " + txt[:180])
                if self.stop_flag() and p.poll() is None:
                    p.kill()
                    raise RuntimeError("Đã dừng bởi người dùng")
            code = p.wait()
        finally:
            if p in self.running:
                self.running.remove(p)
        if code != 0:
            tail = "\n".join(lines[-8:])
            raise RuntimeError(f"Lệnh lỗi: {' '.join(cmd)}\n{tail}")


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


def make_colored_icon(kind: str, color: str) -> QIcon:
    size = 20
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.NoPen)
    if kind == "play":
        pts = [QPoint(6, 4), QPoint(16, 10), QPoint(6, 16)]
        painter.drawPolygon(QPolygon(pts))
    elif kind == "pause":
        painter.drawRoundedRect(5, 4, 4, 12, 1, 1)
        painter.drawRoundedRect(11, 4, 4, 12, 1, 1)
    elif kind == "image_plus":
        painter.drawRoundedRect(3, 4, 14, 12, 2, 2)
        painter.setBrush(QColor("#2e7d32"))
        painter.drawEllipse(6, 7, 3, 3)
        painter.setBrush(QColor(color))
        painter.drawRect(14, 2, 4, 4)
        painter.drawRect(15, 1, 2, 6)
        painter.drawRect(12, 4, 8, 2)
    painter.end()
    return QIcon(pm)


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
        self.outdir.mkdir(parents=True, exist_ok=True)
        out_final = self.outdir / f"{safe_stem}_processed.mp4"
        with tempfile.TemporaryDirectory(prefix="autosf_") as td:
            td = Path(td)
            audio = td / "audio.aac"
            self.runner.run([self.ffmpeg_bin, "-y", "-i", str(video), "-vn", "-acodec", "copy", str(audio)], step_name="Tách audio")

            scenes = detect_scenes(video, self.settings.scene_sensitivity)
            if len(scenes) <= 1:
                duration = ffprobe_duration(self.ffprobe_bin, video)
                scenes = fallback_segments(duration)

            seg_dir = td / "segs"
            seg_dir.mkdir()
            segs = []
            self.log.emit(f"▶ Cắt {len(scenes)} segment")
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
            self.runner.run([self.ffmpeg_bin, "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(shuffled)], step_name="Ghép segment")

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
                f"[0:v]setpts=PTS-STARTPTS,split=2[v_main_src][v_fade_src];"
                f"color=c=black:s={W}x{H}:d=1[base];"
                f"[base][img]overlay=0:{image_top}[base_img];"
                f"[base][v_main_src]overlay=0:{offset_y}[vp_main];"
                f"[vp_main]crop={W}:{main_video_h}:0:0[v_main];"
                f"[base_img][v_main]overlay=0:0[tmp];"
                f"[base][v_fade_src]overlay=0:{offset_y}[vp_fade];"
                f"[vp_fade]crop={W}:{max(1,ov)}:0:{image_top},format=yuv420p[fade_crop];"
                f"color=white:s={W}x{max(1,ov)}:d=1,format=gray,geq=lum='{alpha}'[mask_gray];"
                f"[fade_crop][mask_gray]alphamerge[fade];"
                f"[tmp][fade]overlay=0:{fade_start}[outv]"
            )
            self.runner.run([
                self.ffmpeg_bin, "-y", "-threads", "0", "-filter_threads", "0", "-i", str(shuffled), "-loop", "1", "-i", str(image),
                "-filter_complex", fc,
                "-map", "[outv]", "-t", f"{ffprobe_duration(self.ffprobe_bin, shuffled):.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-shortest", str(composed)
            ], step_name="Composite video + ảnh + fade")

            try:
                self.runner.run([self.ffmpeg_bin, "-y", "-i", str(composed), "-i", str(audio), "-c:v", "copy", "-c:a", "aac", "-shortest", str(out_final)], step_name="Gắn lại audio")
            except Exception:
                self.runner.run([self.ffmpeg_bin, "-y", "-i", str(composed), "-i", str(audio), "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out_final)], step_name="Gắn lại audio (fallback)")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Phần mềm Auto Video Shuffle + Image Compositor")
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
        video_col = QVBoxLayout()
        image_col = QVBoxLayout()

        self.video_list = QListWidget()
        self.image_list = QListWidget()
        video_col.addWidget(self.video_list)
        image_col.addWidget(self.image_list)

        video_btns = QHBoxLayout()
        self.btn_add_video = QPushButton("Thêm video")
        self.btn_remove_video = QPushButton("Xoá video")
        video_btns.addWidget(self.btn_add_video)
        video_btns.addWidget(self.btn_remove_video)
        video_col.addLayout(video_btns)

        image_btns = QHBoxLayout()
        self.btn_add_image = QPushButton("Thêm ảnh")
        self.btn_remove_image = QPushButton("Xoá ảnh")
        image_btns.addWidget(self.btn_add_image)
        image_btns.addWidget(self.btn_remove_image)
        image_col.addLayout(image_btns)

        row.addLayout(video_col)
        row.addLayout(image_col)
        v.addLayout(row)

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

        action_row = QHBoxLayout()
        self.btn_pick_output = QPushButton("Chọn output (tuỳ chọn)")
        self.btn_start = QPushButton("Bắt đầu")
        self.btn_stop = QPushButton("Dừng")
        self.btn_kill = QPushButton("Kết thúc")
        self.btn_output = QPushButton("Mở thư mục output")
        self.btn_help = QPushButton("Hướng dẫn sử dụng")
        for b in [self.btn_pick_output, self.btn_start, self.btn_stop, self.btn_kill, self.btn_output, self.btn_help]:
            action_row.addWidget(b)
        v.addLayout(action_row)

        self.progress = QProgressBar()
        self.log = QTextEdit(); self.log.setReadOnly(True)
        v.addWidget(self.progress); v.addWidget(self.log)

        self.setCentralWidget(w)

        st = self.style()
        self.btn_add_video.setIcon(st.standardIcon(QStyle.SP_FileDialogNewFolder))
        self.btn_remove_video.setIcon(st.standardIcon(QStyle.SP_TrashIcon))
        self.btn_add_image.setIcon(make_colored_icon("image_plus", "#1976d2"))
        self.btn_remove_image.setIcon(st.standardIcon(QStyle.SP_TrashIcon))
        self.btn_pick_output.setIcon(st.standardIcon(QStyle.SP_DirOpenIcon))
        self.btn_start.setIcon(make_colored_icon("play", "#00c853"))
        self.btn_stop.setIcon(make_colored_icon("pause", "#ffab00"))
        self.btn_kill.setIcon(st.standardIcon(QStyle.SP_BrowserStop))
        self.btn_output.setIcon(st.standardIcon(QStyle.SP_DialogOpenButton))
        self.btn_help.setIcon(st.standardIcon(QStyle.SP_MessageBoxInformation))

        self.btn_add_video.clicked.connect(self.add_video)
        self.btn_remove_video.clicked.connect(self.remove_videos)
        self.btn_add_image.clicked.connect(self.add_image)
        self.btn_remove_image.clicked.connect(self.remove_images)
        self.btn_pick_output.clicked.connect(self.pick_output_folder)
        self.btn_start.clicked.connect(self.start_run)
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_kill.clicked.connect(self.kill_now)
        self.btn_output.clicked.connect(self.open_output)
        self.btn_help.clicked.connect(self.show_help)

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

    def remove_videos(self):
        selected = self.video_list.selectedItems()
        if selected:
            for it in selected:
                self.video_list.takeItem(self.video_list.row(it))
            return
        self.video_list.clear()

    def remove_images(self):
        selected = self.image_list.selectedItems()
        if selected:
            for it in selected:
                self.image_list.takeItem(self.image_list.row(it))
            return
        self.image_list.clear()

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


    def show_help(self):
        release_date = "2026-05-05"
        guide = f"""PHẦN MỀM AUTO VIDEO SHUFFLE + IMAGE COMPOSITOR
Phiên bản: v1.0
Tác giả: Nguyễn Xuân Thoán
Ngày phát hành: {release_date}

HƯỚNG DẪN SỬ DỤNG:
1) Bấm 'Thêm video' để chọn một hoặc nhiều file video.
2) Bấm 'Thêm ảnh' để chọn danh sách ảnh dùng để compositing.
3) (Tuỳ chọn) Bấm 'Chọn output' để đổi thư mục xuất.
4) Thiết lập thông số: độ nhạy scene, crop ảnh, chiều cao ảnh, overlap, kiểu fade.
5) Bấm 'Bắt đầu' để chạy batch. Có thể bấm 'Dừng' hoặc 'Kết thúc' để ngắt tiến trình.
6) Bấm 'Mở thư mục output' để mở thư mục chứa video đã xử lý.

ĐIỀU KIỆN CẦN:
- Bắt buộc có ffmpeg.exe và ffprobe.exe nằm trong cùng thư mục chạy phần mềm/dự án.

CÁCH TẢI ffmpeg.exe / ffprobe.exe:
1) Truy cập trang chính thức: https://ffmpeg.org/download.html
2) Chọn bản Windows build (gợi ý: gyan.dev hoặc BtbN builds).
3) Giải nén và copy ffmpeg.exe + ffprobe.exe vào cùng thư mục với main.py hoặc file .exe của phần mềm.

QUY TRÌNH XỬ LÝ:
- Tách audio từ video đầu vào.
- Phát hiện scene (hoặc fallback chia đoạn 3-5 giây).
- Cắt segment, giữ segment đầu và xáo trộn các segment còn lại.
- Ghép lại video đã shuffle.
- Composite video + ảnh + overlap + fade mask theo thông số.
- Gắn lại audio và xuất file hoàn chỉnh.
"""
        QMessageBox.information(self, "Hướng dẫn sử dụng", guide)

    def open_output(self):
        path = self.last_output_used or self.get_output()
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    mw = MainWindow()
    mw.resize(1200, 700)
    mw.show()
    sys.exit(app.exec())
