"""jmcomic 下载适配层。

只使用 jmcomic 的公开能力：

* ``JmOption.construct`` 构造客户端与下载选项；
* ``new_downloader(option).client.get_album_detail`` 读取相册元数据；
* ``download_photo(photo_id, option)`` 下载并解码单章图片；
* 章节 PDF 由本插件调用 img2pdf 生成（见 :mod:`core.pdf`），不依赖 jmcomic
  的导出插件，便于把「下载」与「合成」分开缓存、统计体积与重试。

所有 jmcomic 异常都在这里翻译成插件自己的错误类型，上层只处理
:class:`core.errors.JmDownloaderError`。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import jmcomic
from astrbot.api import logger
from jmcomic import jm_config, jm_exception

from .config import PluginConfig
from .errors import (
    AlbumNotFoundError,
    ChapterDownloadError,
    JmSourceNetworkError,
    PdfBuildError,
)
from .models import AlbumInfo, ChapterArtifact, ChapterRef
from .pdf import build_chapter_pdf, image_sort_key, list_chapter_images

# jmcomic 默认对每张图片打 INFO 日志（一本 40 章约数千行），会淹没 AstrBot 日志；
# 使用 jmcomic 自己导出的 logger 只保留 WARNING 及以上，下载失败仍由本模块翻译为用户提示。
jm_config.jm_logger.setLevel("WARNING")

_ALBUM_ACCESS_HINT = "若内容仅登录可见，请在插件配置中填写 cookies。"
_PROXY_HINT = "请稍后重试，或在插件配置中设置可用代理。"


def _ordered_chapter_images(
    image_dir: Path,
    manifest_images: list[Path],
) -> list[Path]:
    """按 jmcomic manifest 的页序排列章节图片。

    ``download_photo`` 的 manifest 会按图片 index 排序（即真实页序），
    比按文件名自然排序更可靠；manifest 不可用时退回目录自然排序。
    """
    images = list_chapter_images(image_dir)
    if not manifest_images:
        return images
    by_name = {image.name: image for image in images}
    ordered: list[Path] = []
    for image in manifest_images:
        item = by_name.pop(image.name, None)
        if item is not None:
            ordered.append(item)
    if by_name:
        ordered.extend(sorted(by_name.values(), key=image_sort_key))
    return ordered


def _manifest_image_paths(result: object) -> list[Path]:
    """从 jmcomic 的 DownloadResult 中取按页序排好的图片路径。"""
    manifest = getattr(result, "manifest", None)
    raw_list = getattr(manifest, "image_filepath_list", None) if manifest else None
    paths: list[Path] = []
    for raw in raw_list or []:
        path = Path(str(raw))
        if path.is_file():
            paths.append(path)
    return paths


def _episode_fields(episode: object) -> tuple[str, str, str]:
    """jmcomic 的 episode 元素通常是 ``(pid, index, title[, pub_date])``。"""
    if not isinstance(episode, (tuple, list)):
        return str(episode), "", ""
    cells = [str(part) for part in episode[:3]]
    cells += [""] * (3 - len(cells))
    return cells[0], cells[1], cells[2]


def _chapter_ref(episode: object) -> ChapterRef:
    """把 jmcomic 的 episode 转成插件内部的章节引用。"""
    pid, index, title = _episode_fields(episode)
    return ChapterRef(pid=pid, index=index, title=title)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _album_error(
    album_ref: str, exc: Exception
) -> AlbumNotFoundError | JmSourceNetworkError:
    """把读取相册元数据时的异常翻译成带用户文案的插件错误。"""
    if isinstance(exc, jm_exception.MissingAlbumPhotoException):
        return AlbumNotFoundError(
            f"没有找到 JM{album_ref}。请检查 ID；{_ALBUM_ACCESS_HINT}"
        )
    if isinstance(exc, jm_exception.RequestRetryAllFailException):
        return JmSourceNetworkError(f"JM 站点连接失败。{_PROXY_HINT}")
    return JmSourceNetworkError(
        f"读取 JM{album_ref} 相册信息失败（{type(exc).__name__}），"
        "详细原因已写入 AstrBot 日志。"
    )


def _chapter_error(photo_id: str, exc: Exception) -> ChapterDownloadError:
    """把单章下载异常翻译成可跳过、可续传的章节错误。"""
    if isinstance(exc, jm_exception.MissingAlbumPhotoException):
        return ChapterDownloadError(f"章节 {photo_id} 不存在或仅登录可见", photo_id)
    if isinstance(exc, jm_exception.PartialDownloadFailedException):
        return ChapterDownloadError(f"章节 {photo_id} 有图片下载失败", photo_id)
    if isinstance(exc, jm_exception.RequestRetryAllFailException):
        return ChapterDownloadError(f"章节 {photo_id} 网络请求失败", photo_id)
    return ChapterDownloadError(
        f"章节 {photo_id} 下载失败（{type(exc).__name__}）", photo_id
    )


class JmSource:
    """按插件配置访问 jmcomic 的一组方法（无状态，可跨任务复用配置快照）。"""

    def __init__(self, config: PluginConfig) -> None:
        self.config = config

    def _option(self, base_dir: Path) -> "jmcomic.JmOption":
        return jmcomic.JmOption.construct(
            self.config.jmcomic_option_dict(base_dir)
        )

    # -- 相册元数据 -------------------------------------------------------

    def fetch_album(self, album_id: str) -> AlbumInfo:
        """只读取相册标题与章节列表，不下载任何图片。"""
        album_ref = str(album_id)
        workspace = Path(tempfile.mkdtemp(prefix="jm_meta_"))
        try:
            option = self._option(workspace)
            try:
                with jmcomic.new_downloader(option) as downloader:
                    album = downloader.client.get_album_detail(album_ref)
            except Exception as exc:  # noqa: BLE001 - 统一翻译成可读错误
                raise _album_error(album_ref, exc) from exc
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

        chapters = tuple(
            chapter
            for chapter in (
                _chapter_ref(episode) for episode in (album.episode_list or [])
            )
            if chapter.pid
        )
        if len(chapters) == 0:
            raise AlbumNotFoundError(f"JM{album_ref} 没有可下载的章节。")
        def _text_tuple(value: object) -> tuple[str, ...]:
            if not isinstance(value, (list, tuple)):
                return ()
            return tuple(str(item).strip() for item in value if str(item).strip())

        return AlbumInfo(
            album_id=str(album.id),
            title=str(album.name or "").strip(),
            chapters=chapters,
            page_count=int(getattr(album, "page_count", 0) or 0),
            publish_date=str(getattr(album, "pub_date", "") or ""),
            update_date=str(getattr(album, "update_date", "") or ""),
            likes=str(getattr(album, "likes", "") or ""),
            views=str(getattr(album, "views", "") or ""),
            comment_count=int(getattr(album, "comment_count", 0) or 0),
            authors=_text_tuple(getattr(album, "authors", ())),
            tags=_text_tuple(getattr(album, "tags", ())),
            works=_text_tuple(getattr(album, "works", ())),
            actors=_text_tuple(getattr(album, "actors", ())),
            description=str(getattr(album, "description", "") or "").strip(),
        )

    # -- 单章下载 + PDF 合成 ---------------------------------------------

    def _download_photo_images(
        self, photo_id: str, images_root: Path
    ) -> tuple[Path, list[Path]]:
        """下载一章的全部图片，返回 (图片目录, 按页序排列的图片)。

        jmcomic 2.7.x 的 ``download_photo`` 返回 ``DownloadResult(detail,
        downloader)``，``save_path`` 与 ``manifest`` 都在返回对象上。
        """
        images_root.mkdir(parents=True, exist_ok=True)
        option = self._option(images_root)
        try:
            downloaded = jmcomic.download_photo(photo_id, option)
        except Exception as exc:  # noqa: BLE001 - 单章失败不拖垮其它章节
            raise _chapter_error(photo_id, exc) from exc
        detail = getattr(downloaded, "detail", downloaded)
        saved_at = getattr(detail, "save_path", None)
        image_dir = Path(saved_at) if saved_at else images_root / photo_id
        images = _ordered_chapter_images(
            image_dir,
            _manifest_image_paths(downloaded),
        )
        if len(images) == 0:
            raise ChapterDownloadError(
                f"章节 {photo_id} 没有下载到图片", photo_id
            )
        return image_dir, images

    def preview_pdf(
        self,
        pid: str,
        images_root: Path,
        pdf_path: Path,
        limit: int,
    ) -> tuple[int, int]:
        """下载一章图片，取前 ``limit`` 页合成预览 PDF，返回 (页数, 字节数)。"""
        photo_id = str(pid)
        image_dir, images = self._download_photo_images(photo_id, images_root)
        selected = images[: max(1, int(limit))]
        try:
            page_count = build_chapter_pdf(selected, pdf_path)
        except PdfBuildError as exc:
            reason = f"章节 {photo_id} 预览 PDF 生成失败（{exc.user_message}）"
            raise ChapterDownloadError(reason, photo_id) from exc
        finally:
            shutil.rmtree(image_dir, ignore_errors=True)
        try:
            size_bytes = pdf_path.stat().st_size
        except OSError as exc:
            raise ChapterDownloadError(
                f"章节 {photo_id} 的预览 PDF 未落盘", photo_id
            ) from exc
        return page_count, size_bytes

    def download_chapter(
        self,
        pid: str,
        images_root: Path,
        pdf_path: Path,
    ) -> ChapterArtifact:
        """下载一章图片并合成 PDF，成功后删除原始图片。"""
        photo_id = str(pid)
        image_dir, images = self._download_photo_images(
            photo_id, images_root
        )
        image_bytes = sum(_file_size(image) for image in images)
        try:
            page_count = build_chapter_pdf(images, pdf_path)
        except PdfBuildError as exc:
            reason = f"章节 {photo_id} 合成 PDF 失败（{exc.user_message}）"
            raise ChapterDownloadError(reason, photo_id) from exc
        finally:
            # 缓存只保留章节 PDF，原始图片在合成后立即清理。
            shutil.rmtree(image_dir, ignore_errors=True)

        try:
            size_bytes = pdf_path.stat().st_size
        except OSError as exc:
            missing_pdf = f"章节 {photo_id} 的 PDF 未落盘"
            raise ChapterDownloadError(missing_pdf, photo_id) from exc
        return ChapterArtifact(
            pid=photo_id,
            page_count=page_count,
            size_bytes=size_bytes,
            image_bytes=image_bytes,
            pdf_path=pdf_path,
        )
