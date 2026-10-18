"""JM 下载插件的核心能力包。

模块划分：

* :mod:`core.config`   配置快照与 jmcomic 选项构造；
* :mod:`core.models`   领域数据模型；
* :mod:`core.errors`   可预期错误（携带用户文案）；
* :mod:`core.jm_source` jmcomic 相册/章节下载适配；
* :mod:`core.pdf`      img2pdf 章节合成、PDF 合并/加密/校验；
* :mod:`core.planner`  按体积动态规划分卷；
* :mod:`core.cache`    章节 PDF 缓存与 LRU 淘汰；
* :mod:`core.delivery` 跨容器上传 URI 与 OneBot 文件上传。
"""
