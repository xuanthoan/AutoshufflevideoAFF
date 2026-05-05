# AutoshufflevideoAFF

Ứng dụng desktop Python (PySide6) để:
- cắt scene video
- xáo trộn segment (giữ segment đầu)
- ghép lại bằng FFmpeg
- compositing video + image + overlap + fade mask động theo %
- giữ audio
- chạy batch

## Cài đặt

```bash
pip install -r requirements.txt
```

Cần có `ffmpeg` và `ffprobe` trong PATH hoặc để cạnh `main.py` (`ffmpeg.exe`, `ffprobe.exe`).

## Chạy

```bash
python main.py
```

## Build .exe portable (Windows)

```bash
pyinstaller --noconfirm --onedir --windowed main.py
```

Sau khi build, copy thêm `ffmpeg.exe` + `ffprobe.exe` vào thư mục phát hành.


## Output

- Nếu không chọn output, app tự tạo `output_processed`.
- Mỗi video sẽ được xuất vào **sub-folder riêng** theo tên video để tránh trùng file khi chạy batch.
