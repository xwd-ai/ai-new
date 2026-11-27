"""从管理员上传的 Markdown 或 PDF 提取可检索文本。"""

import os
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore
from time import monotonic


MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 20
PDF_TIMEOUT = 24
_pdf_slot = BoundedSemaphore(1)


class DocumentUnavailable(RuntimeError):
    """PDF 提取资源正忙、工具不可用或达到处理时间上限。"""


def document_text(extension: str, data: bytes) -> str:
    if extension in (".md", ".markdown"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Markdown 文件必须使用 UTF-8 编码。") from exc
        if "\x00" in text:
            raise ValueError("Markdown 文件包含无效字符。")
        return text.strip()
    if not data.startswith(b"%PDF-"):
        raise ValueError("文件内容不是有效的 PDF。")
    if not _pdf_slot.acquire(blocking=False):
        raise DocumentUnavailable("正在处理另一份 PDF，请稍后重试。")
    try:
        return _pdf_text(data)
    finally:
        _pdf_slot.release()


def _pdf_text(data: bytes) -> str:
    deadline = monotonic() + PDF_TIMEOUT
    env = {**os.environ, "LC_ALL": "C", "OMP_THREAD_LIMIT": "1"}

    def run(*args: str) -> bytes:
        try:
            return subprocess.run(
                args, check=True, capture_output=True,
                timeout=max(0.001, deadline - monotonic()), env=env,
            ).stdout
        except FileNotFoundError as exc:
            raise DocumentUnavailable("服务器尚未安装 PDF 提取或 OCR 工具。") from exc
        except subprocess.TimeoutExpired as exc:
            raise DocumentUnavailable("PDF 处理超时，请拆成更少页后上传。") from exc
        except subprocess.CalledProcessError as exc:
            if args[0] == "tesseract":
                raise DocumentUnavailable("OCR 未能完成，请检查语言包或稍后重试。") from exc
            raise ValueError("无法读取 PDF，请确认文件完整且未加密。") from exc

    with TemporaryDirectory(prefix="ainew-document-") as folder:
        pdf = Path(folder) / "upload.pdf"
        image = Path(folder) / "page"
        pdf.write_bytes(data)
        info = run("pdfinfo", str(pdf))
        count = re.search(rb"^Pages:\s+(\d+)", info, re.MULTILINE)
        if not count or int(count[1]) < 1:
            raise ValueError("PDF 没有可读取的页面。")
        pages = int(count[1])
        if pages > MAX_PDF_PAGES:
            raise ValueError(f"PDF 最多 {MAX_PDF_PAGES} 页，请拆分后上传。")

        native = run("pdftotext", "-layout", "-enc", "UTF-8", str(pdf), "-").decode("utf-8", errors="replace")
        extracted = []
        for page, text in enumerate(native.split("\f")[:pages], start=1):
            if sum(character.isalnum() for character in text) < 12:
                run("pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", "1800",
                    "-gray", "-png", str(pdf), str(image))
                scanned = run("tesseract", str(image.with_suffix(".png")), "stdout", "-l", "chi_sim+eng")
                text = scanned.decode("utf-8", errors="replace").strip() or text
            if text.strip():
                extracted.append(text.strip())
        return "\n\n".join(extracted)
