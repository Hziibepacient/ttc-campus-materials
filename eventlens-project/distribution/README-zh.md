# EventLens 0.2.0 安装与使用

EventLens 统计固定机位或延时摄影视频里，在某个位置附近（立牌、摊位、海报）停下来的人次。它是一个本地工具，加上一个给 AI 助手用的 skill。你不需要写代码，装好后直接跟 Codex 或 Claude Code 说话即可。视频和分析结果都留在你自己的电脑上，不会上传。

## 包里有什么

| 文件 | 用途 |
|---|---|
| `eventlens-0.2.0-py3-none-any.whl` | 工具本体（已包含 AI 助手 skill） |
| `install.ps1` | Windows 一键安装 |
| `install.sh` | macOS / Linux 一键安装 |
| `AGENT-PROMPT.txt` | 直接复制发给你的 AI 助手的话 |

## 最简单的用法：让 AI 助手帮你装

1. 解压到任意文件夹，例如 `C:\Users\你\Downloads\EventLens-0.2.0`。
2. 打开 Codex 或 Claude Code，把 `AGENT-PROMPT.txt` 的内容发给它，并把里面的路径改成你自己的。
3. 装好后，重启一次 Codex / Claude Code，让它加载 eventlens skill。

## 自己装（Windows）

在解压目录里，右键选"在终端中打开"，然后运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

安装脚本会依次做这几件事：
- 用 winget 安装 uv 和 FFmpeg（已经装过就跳过）
- 安装 `eventlens` 命令
- 从 YOLOX 官方下载约 36 MB 的检测模型，并校验 SHA-256
- 把 skill 放进 `%USERPROFILE%\.codex\skills` 和 `%USERPROFILE%\.claude\skills`
- 最后运行 `eventlens doctor` 自检，每一项都应显示 `[ok]`

## 装好后怎么说

> 用 eventlens 分析 C:\Users\16212\Videos\DJI_20261018105551_0036_D.MP4，这是延时摄影，每 2 秒拍一张，统计在奶龙立牌附近停留的人次。

AI 助手会先问你几个问题：最短停留几秒算数、工作人员要不要排除等。之后它会出图，请你确认三件事：附近区域、立牌是否要排除、机位移动和黑屏的边界。全部确认后才出报告。

## 看结果时注意

- 报告标题会写明状态：**机器估算（未核验）**、**抽查修正**或**逐条核验**。没核验过的数字不是准确人数。
- 统计的是"人次"，不是"人数"。同一个人离开超过 30 秒后再回来，会再计一次（这个秒数可以改）。
- AI 助手自己的判断只算"建议"，不计入人工确认。

## 速度

- 不同电脑差别很大，可以先运行 `eventlens bench 你的视频.MP4` 测一下。
- 只用 CPU 时，8,000 帧大约要十几到二十几分钟。
- 有独立显卡的 Windows 电脑，可以先运行 `pip install onnxruntime-directml`，再运行 `eventlens detector set provider=dml` 尝试加速。

## 遇到问题

- 在终端里运行 `eventlens doctor`，把输出发给 AI 助手或开发者。
- 想看详细报错，先运行 `$env:EVENTLENS_DEBUG=1`，再重跑出错的命令。
- 本版本在 Windows 上还没有经过实机测试。第一次安装请把 `eventlens doctor` 和 `eventlens bench` 的输出发回给开发者。
