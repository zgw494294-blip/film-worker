# 电影复刻执行端

执行端主动连接公网管理服务，领取首制、返修及出片工单。配置页运行在本机回环地址。任务归属、人工审核和已交付版本由管理服务保存。

## 安装和连接

1. 将与机器架构匹配的 ZIP 解压到本机。Apple Silicon 选择 `macos-arm64`，Windows 选择 `windows-x86_64`。离线包包含 CPython、uv、锁定的 Python 媒体库、FFmpeg 9.0.2、FFprobe 和目标系统组件。首次打开自动校验并安装到本机数据目录，再次打开沿用已有依赖。单独下载的开发源码可用本机 uv 安装。
2. macOS 打开 `启动执行端.command`，Windows 打开 `启动执行端.cmd`。如需独立数据盘，给启动程序传入 `--data <目录>`。配置页只在本机回环地址监听。
3. 准备 Blender 和已登录的 Codex CLI。离线安装会同时配置媒体工具，已有配置可在“工具路径”覆盖。macOS 可自动发现 Applications 里的 Blender 与 ChatGPT 所带 CLI。Windows 支持配置原生 `codex.exe` 和 `blender.exe` 等绝对路径，也会查找常见的 npm Codex 目录。[Codex 官方原生包布局](https://github.com/openai/codex/blob/main/codex-cli/scripts/build_npm_package.py)
4. 保存公网服务 URL、管理员发放的节点标识、建模 Token 和服务端下发的制作档案数组。点击“检测本机工具”查看本机工具与执行端版本，也可以在“模型与制作档案”测试已保存的 Codex 调用。更换服务商、模型或推理强度可生成新档案，由管理员使用相同档案派发。然后启动节点。调整配置前，暂停领单并等待在途工单交回。关闭浏览器可以在启动进程存续期间继续制作。

开发目录中可用 `uv run --locked python -m film_worker launch` 打开带更新功能的面板，也可用 `uv run --locked python -m film_worker run` 直接启动执行服务。机器 Token 保存在 macOS 钥匙串或 Windows 凭据管理器，部署脚本也可通过 `FILM_WORKER_TOKEN` 注入。Codex 的登录和模型服务凭证保存在本机 Codex 配置中。

机器接口见 `contract.openapi.yaml`。Mac 与 Windows 使用同一套锁定源码构建 FFmpeg；领同一制作档案还需使用相符的 Blender、Codex CLI、解码器及执行端版本。不匹配时面板显示要求值和本机值。正式环境使用 HTTPS。与本机模拟服务器联调时，可以在子命令前加 `--test-http`。模型、推理强度、工作流和程序提交由制作档案锁定，节点注册仅公布本机可兑现的档案。

## Git 更新

管理者先执行 `python scripts/release_manifest.py <版本> <递增序号>`，将生成的 `release.json` 随发布代码提交，再用受信任的 SSH 发布私钥签署 annotated tag。升级节点只读取所配置仓库的该标签。签名公钥以 Git `allowed_signers` 格式提供给节点，例如 `publisher ssh-ed25519 AAAA…`。Windows 节点需要 Git 和 OpenSSH 的 `ssh-keygen`。在面板填仓库、标签和公钥文件，开启自动检查，或按“检查并更新”。[Git 签名校验](https://git-scm.com/docs/git-verify-tag) · [可信发布者文件格式](https://git-scm.com/docs/git-config#Documentation/git-config.txt-gpgsshallowedSignersFile)

新版以锁文件准备独立环境，并完成 SQLite、编解码和像素签名自检。切换前暂停领单并等待在途工单收束。新版启动验证失败时自动用上一个版本恢复，也可从面板手动回退。之前已消化的发布序号保持记录，旧签名包不会触发自动降级。管理员须同步准备含新版提交号的制作档案，才能为新版派发相应作业。

用本地 Git 仓库模拟发布与网络 Git 使用相同的签名和版本规则。开发启动目录应保留，因为它是首次安装的启动器和首次回退目标。更新所需的依赖从锁文件安装，优先复用本机 uv 缓存。

## 制作安装包

先提交源码。在 Mac 上按目标运行 `uv run --locked python scripts/build_media_tools.py --target macos-arm64` 或 `--target windows-x86_64`，然后运行 `uv run --locked python scripts/build_portable.py --target <目标> --tools dist/media-tools/<目标>`。压缩包和 SHA-256 文件生成于 `dist/`。构建器用 `uv.lock` 校验各平台的 wheels。可通过 `--tools` 随包携带有权分发的 Codex、Blender 或 FFmpeg，工具目录须含 `components.json`，列出各工具版本、来源和许可证。首次启动安装到数据目录后，两种操作系统都能复用已有工具缓存。随包媒体工具使用 FFmpeg 9.0.2 与 zlib 1.4 的锁定源码，源码和许可证一并保存在包内。[FFmpeg 源码](https://ffmpeg.org/releases/) · [zlib](https://zlib.net/) · [Windows 交叉工具链](https://github.com/mstorsjo/llvm-mingw/releases/tag/20261117) · [uv 锁定依赖与同步](https://docs.astral.sh/uv/concepts/projects/sync/)

## 验证

运行 `uv run --locked python -m unittest discover -s tests -v` 可验证分块回传、断点续传、归档路径、复审尺寸和本地状态。媒体验收需将环境变量 `FILM_WORKER_TEST_MEDIA` 设为 `1`，它用测试脚本生成 Blender 场景，并通过真实 Blender、FFmpeg 和 FFprobe 跑首制、返修、出片三步。该用例不会调用在线模型。

模拟服务测试包括双槽位隔离、单单停止、续租重启和同版本重领。重领时核对冻结输入、运行版本和每个输出文件的 SHA-256，再通过新租约上传。文件损坏会重新生成。资格确认和分块回传都使用当前租约。

在目标系统运行 `uv run --locked python scripts/verify_portable.py <安装包路径>` 可检查安装包摘要、离线安装、重复启动复用、随包媒体工具和本机面板。GitHub Actions 配置包含原生 Apple Silicon Mac 和 Windows x64 两套运行环境，由 Mac 构建同一源码提交的两套安装包，再分别下载到原生系统运行验收。[运行器架构](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)


## 附件里的验收要求

《验收标准.pdf》和《错误类型分类汇总.docx》是本项目的制作规则来源。执行端把十类问题写入首制、返修的对照检查。位置、镜头、动作、遮挡、颜色和时序质量由人工逐帧验收。技术检查读取原片、白模和对比三套实际文件，在 `meta/video-technical.jsonl` 记录统一 sample ID、对应文件路径、编解码、画幅、时长、帧率、音轨和黑帧结果。模型制作说明保存在单独的镜头 JSONL。出片核验包含片尾最后一秒。

已确认的内部试制使用 `codex_trial`。若服务端将正式档案设置为 `human_required`，节点禁止自动首制和返修。此档案仅接收出片任务，要求已通过版本带有人工作品来源及冻结的人工通过清单。正式批次的片长和画幅按附件技术表检查。人工作品入库与来源审核由服务端负责。
