# NCM 音乐转换器

Windows 本地小工具，用来从网易云音乐 `.ncm` 文件中恢复其实际保存的
FLAC 或 MP3 音频。它做的是解密与原样提取，不是二次转码：如果 NCM 中
原本是无损 FLAC，输出仍是同一份 FLAC 音频数据，不会先转成 WAV 或 MP3。

> 仅用于处理个人有权使用、已经合法下载到本地的文件。软件不负责下载
> 歌曲、绕过账号或订阅权限，也不会删除源 NCM。

## 最简单的用法

1. 解压 `NCM音乐转换器-v1.0.0-Windows-x64.zip`。
2. 双击 `NCM音乐转换器.exe`。
3. 将 NCM 文件/文件夹拖入窗口，或点击“添加 NCM 文件”“添加文件夹”。
4. 保持“保存到源文件旁”和“自动重命名（推荐）”，点击“开始转换”。
5. 完成后点击“打开输出目录”，把得到的 `.flac` 或 `.mp3` 复制到播放器。


## 功能

- 多文件、文件夹递归扫描、窗口拖放、拖到程序图标启动。
- 按解密后真实文件头识别 FLAC、带 ID3 的 MP3 和无 ID3 的 MP3。
- 流式处理大文件，不把整首无损音乐同时载入内存。
- 支持中文、日文、空格和 emoji；长路径还需 Windows 系统策略已启用长路径支持。
- 输出先写入临时 `.part`，成功后原子提交；取消/失败会清理临时文件。
- 同名文件默认自动添加序号，也可选择跳过或覆盖。
- 标签/封面错误只作为提示，不会丢弃已经成功恢复的音频。
- 默认完全离线。可选的“联网补封面”仅允许访问网易 HTTPS 图片域名，带
  超时、大小上限和图片格式检查。
- 单个坏文件不会终止整个批次；详细信息保存在程序旁的 `logs` 文件夹。

## 命令行

在源码环境中：

```powershell
.\.venv\Scripts\python.exe -m src.ncm_cli "E:\Music\CloudMusic" `
  -o "E:\Music\Converted" --on-exist rename
```

常用参数：

```text
--on-exist rename|skip|overwrite
--no-recursive
--fetch-cover
--no-tags
--json
```

不指定 `-o` 时，输出保存在每个 NCM 源文件旁。输入文件夹且指定 `-o` 时，
会保留该文件夹内部的相对目录结构。

## 从源码运行和构建

需要 Windows x64 和 Python 3.10 或更高版本。`build.ps1` 会优先使用 `PATH`
中的 `python.exe`（其次为 `py.exe`）创建项目自己的 `.venv`。

运行 GUI：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\src\ncm_gui.py
```

运行测试并构建 Windows x64 便携版：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

如果 Python 不在 `PATH`，可以显式指定：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1 `
  -BootstrapPython "D:\Path\To\python.exe"
```

构建脚本会先执行纯合成 NCM 回归测试，再生成 onedir 便携包、SHA-256 清单、
完整对应源码和第三方许可证；随后还会让打包 EXE 以及重新解压的 ZIP 各完成
一次带标签、封面的真实转换自测。测试素材由程序生成，不包含真实歌曲或封面。

## 安全与隐私

- 不执行或捆绑旧转换器目录中的任何 EXE。
- 不默认联网；远程封面开关默认关闭。
- 不默认覆盖输出，不提供“转换后删除源文件”功能。
- NCM 内所有长度字段都先做文件边界和合理上限校验。
- 元数据解密/JSON/标签失败会降级处理，不会让批量任务崩溃。

## 已知边界

- 当前目标是普通 NCM 中的 FLAC/MP3。若某种“沉浸声/全景声”下载使用了
  不同音频载荷或容器变体，软件会明确报“不支持的音频格式”，不会伪装成
  FLAC 输出。
- 仅靠合成样本无法覆盖网易云所有历史客户端版本。最好再用一份你现有工具
  能转换的 NCM 和一份当前失败的 NCM 做回归；日志会帮助区分下载不完整、
  容器变体、路径权限和标签问题。
- 便携 EXE 没有商业代码签名证书；首次运行时 Windows 可能显示信誉提示。
  可用随包 `SHA256SUMS.txt` 校验文件完整性。

## 许可证与参考

本项目按 `GPL-2.0-or-later` 提供，完整许可证见 `LICENSE`。便携包内附全部
对应源码和第三方许可证。NCM 格式兼容研究参考了 `ncmdump`、`ncmdump-go`、
`ncmdump.rs` 和 `ncmdump-py` 等公开项目；详情见 `THIRD-PARTY-NOTICES.txt`。
