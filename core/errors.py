"""插件可预期错误：统一携带可直接回给用户的中文文案。"""

from __future__ import annotations


class JmDownloaderError(RuntimeError):
    """所有可预期失败的基类。

    ``user_message`` 会被原样发给用户；内部细节（路径、异常链）留在日志里。
    """

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class InvalidAlbumIdError(JmDownloaderError):
    """命令参数不是合法的 JM 数字 ID。"""


class AlbumMismatchError(JmDownloaderError):
    """目标相册与输入不符或没有可下载章节。"""


class ChapterOutOfRangeError(JmDownloaderError):
    """章节编号不在相册范围内。"""


class AlbumNotFoundError(JmDownloaderError):
    """相册不存在，或仅登录可见。"""


class JmSourceNetworkError(JmDownloaderError):
    """jmcomic 站点请求失败（可重试）。"""


class PartialChapterError(JmDownloaderError):
    """有章节下载失败；已完成的章节一般已写入缓存。"""

    def __init__(self, user_message: str, failed_pids: list[str]) -> None:
        super().__init__(user_message)
        self.failed_pids = tuple(failed_pids)


class ChapterDownloadError(JmDownloaderError):
    """单个章节下载/合成失败（其他章节可继续）。"""

    def __init__(self, user_message: str, pid: str) -> None:
        super().__init__(user_message)
        self.pid = str(pid)


class PdfBuildError(JmDownloaderError):
    """章节合成或整本合并得到的 PDF 不可用。"""


class SplitNotPossibleError(JmDownloaderError):
    """无法按章节边界生成回退分卷。"""


class ArchiveBuildError(JmDownloaderError):
    """压缩包生成失败（格式不支持、缺少依赖或打包异常）。"""


class UploadConfigError(JmDownloaderError):
    """上传前置条件不满足（如 base64 兜底超过体积上限）。"""


class UploadFailedError(JmDownloaderError):
    """协议端上传失败。"""
