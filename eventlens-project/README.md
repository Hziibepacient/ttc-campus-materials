# EventLens 0.2.0

固定机位与延时摄影的本地停留人次分析工具，配有可供 AI 助手调用的命令行和技能。用于立牌、摊位和校园活动的观察、复核与报告。

[查看真实校招案例与视频](../showcase/README.md)

## 工程能力

流程为视频解码 → YOLOX-S 人体检测 → 检测与外观特征缓存 → 分机位区域筛选 → 停留与轨迹归并 → 证据复核 → JSON / CSV / HTML 报告。

检测缓存与计数规则分离，调整停留门槛、区域和合并间隔时可复用已有检测；核验记录区分人工结论与助手建议。源码还提供黑屏/机位建议、静态对象排查、标注窗口与评估命令。

## 安装与使用

需要 Python 3.10+、FFmpeg 与 FFprobe。建议使用独立虚拟环境。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\eventlens.exe doctor
.\.venv\Scripts\eventlens.exe --help
```

分析新素材前需准备 YOLOX-S 模型；`eventlens models fetch` 会联网下载。`eventlens skill install` 会修改助手技能目录，按需运行。`distribution` 中保留原始分发说明与安装脚本；脚本会安装系统依赖与技能，阅读后再使用。

```text
eventlens init --name "Campus activity"
eventlens video add /path/to/timelapse.mp4 --timelapse-interval 2
eventlens analyze
eventlens count
eventlens report
```

正式计数前应配置区域、排除黑屏并核对机位切换，详见 [EventLens 技能](src/eventlens/skill/eventlens/SKILL.md)。

## 源码来源与验证边界

本目录 Python 源码从本机已有 `eventlens-0.2.0-py3-none-any.whl` 原样提取；`pyproject.toml` 按 wheel 元数据补建，便于浏览、安装和后续开发。不是恢复出的完整原始开发仓库，原分发包不含 0.2.0 测试目录。源码与 wheel 的对应 SHA256 见 `provenance.json`。

本次展示使用 2026-10-20 已保存的检测缓存，重新运行原版计数引擎核对到 1,093 条记录，记录键与历史报告完全相同。导出视频读取并解码两段原片的全部 8,300 帧后，每 4 帧选一帧展示。没有重新运行检测模型，也没有新增人工确认或完成精度评测。

历史 Windows 分析采用 OpenCV 解码适配器和 ONNX Runtime CPU，并非原安装脚本 / FFmpeg 完整链路验收。原有代码检查仍有待解决项：部分解码失败的完成状态、源素材变更时的缓存验证、重算后的历史核验关联。当前状态为 Alpha。

原创工具代码沿用 Apache-2.0；视频、人物影像及第三方素材不随代码许可证授权。原始 0.1.0 源码另保存在展示 Release 的历史源码包中，其测试不代表 0.2.0 已通过相同测试。
