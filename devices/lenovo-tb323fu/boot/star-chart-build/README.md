# Y700 五代引导构建与打包

适用于 TB323FU / SM8850P（Baldur）。配套 [kShimC 源码](../kshim-success-source/README.md) 由四代与五代共用，固定来源见 [SOURCES.md](../../docs/SOURCES.md)。本目录的打包布局与 DTB 检查针对五代；四代使用[独立引导说明](../../../lenovo-tb322fc/boot/README.md)。

当前统一 Linux 7.2.6 的五代引导使用外置 initramfs。完整流程是准备本机原厂 Android 输入、构建 runtime、生成匹配内核的 initramfs，再打包 boot；所需原厂输入和依赖须自行准备。

## 选择构建路径

| 场景 | 入口 |
|---|---|
| 五代 runtime | `configs/tb323fu_unified_defconfig`，使用共享源码中的 `tools/build_runtime.py` |
| 外置 initramfs | [外置 initramfs 说明](EXTERNAL_INITRAMFS.md) |
| 统一内核 boot | `build_offline_boot.py`，传入 `--initrd` 使用九条目布局 |
| 早期内置 initramfs 流程 | `build.sh`，使用 `tb323fu_defconfig`，默认调用不带 `--initrd` 的八条目打包 |

`build.sh` 不会自动改用外置 initramfs。两个 runtime 配置目前都启用 STAR_CHART 和会话设置；是否使用外置 initramfs 仍由 Linux 载荷、DTB 和打包参数决定。

## 文件

| 文件 | 用途 |
|---|---|
| `build.sh` | 早期八条目流程：主机测试、主题预览、ARM64 runtime、QEMU 与 boot 打包 |
| `build_offline_boot.py` | 使用独立输入组装并校验八条目 manifest 和 boot；可选外置 initramfs 九条目布局 |
| `build_external_initramfs.py` | 由内置 initramfs 模板和统一内核模块目录重建外置 initramfs |
| `repack_boot_v4.py` | 保持 boot v4 包装结构的重打包工具 |
| `menu-required-text.txt` | 中英文菜单字形清单 |

## 依赖

恢复固定版本的 CrDK、picolibc 和 LVGL，并应用源码内 `ports/crdk/` 补丁层。`lib/libfdt` 和 `libfdt_env.h` 需单独准备；仅初始化 `.gitmodules` 中的子模块并不足够。打包需要配套 MultiBootKernelPatcher/UnpackTool 和 `patches/boot/` 的本地变更，具体版本见来源文档。

源码快照不包含顶层 `.git/`、`lib/` 和 `third_party/`。这不包括 `src/` 内同名的源码子目录，不能一并删除。

## 容器工作目录

```text
/work/
  source.tar
  source/                 # 源码和已恢复的固定依赖
  build.sh
  build_offline_boot.py
  repack_boot_v4.py
  inputs/
    boot-stock.img
    Image.stock
    Image.linux-165
    tf.dtb
    internal.dtb
    avbtool.py
  out/
  qemu-manifest/
  boot-candidate/
```

`Image.linux-165` 是脚本输入名，不能据此推断实际内核版本。Android payload、原厂 boot 和设备 DTB 必须由构建者从自己的设备准备，并核对 manifest 中的 payload、DT 索引及大小。

构建、字体生成及验证在 Linux Docker 环境执行。runtime 使用固定 4 MiB 槽位；生成 boot 的大小还受目标分区限制。

## 外置 initramfs

`build_external_initramfs.py` 在 Linux Docker 中由内置 initramfs 模板和统一内核模块暂存目录离线重建五代统一内核使用的外置 initramfs。输入约束、命令参数、运行环境与产物说明见 [EXTERNAL_INITRAMFS.md](EXTERNAL_INITRAMFS.md)。

## 离线 boot 的外置 initramfs（可选）

不带 `--initrd` 时仍生成八条目 manifest（条目 0..6 加条目 7 `Attached Shim`），默认 `Image-01`、超时 8000 ms。追加 `--initrd <path>` 后切换为九条目布局：条目 0..6 保持原索引，条目 7 为三份 Linux 菜单共享的字节精确 blob（`Type=Blob`、flags 0、`Align=16`，即 64 KiB 对齐），`Attached Shim` 顺延到条目 8。64 KiB 对齐使 initrd 按页取整后的保护区间不与其前面的已选 DTB 重叠：对支持的 4/16/64 KiB 页大小均成立，页大小未知时也取保守值。打包配置为 `[Image-07] ... Type=Blob` 后接 `[Manifest]`，shim 仍由 `[Pack] Shim=` 自动追加，不复制 Linux payload。

外置模式会在三份派生 DTB（internal、TF、diagnostic）的 `/chosen` 写入 `kshim,initrd-entry = <7>`，并删除其中的 `linux,initrd-start` / `linux,initrd-end`；输入 DTB 本身不被修改。脚本要求传入的 internal/TF DTB 已含 `/chosen` 与 `bootargs`，根 `compatible` 同时含 `lenovo,baldur`、`qcom,kaanapali`，且 `bootargs` 含 `baldur.end=hold`、`cpuidle_psci_domain.allow_cluster_off=0` 与 `msm.dpu_use_virtual_planes=0`。后者用于 TB323FU/Gen5 的固定 SSPP 映射，并不关闭全部 multirect；公共内核与 Gen4 的默认行为不变。校验还比对源/派生 DTS：除上述 initrd 属性外不得有任何差异，`armada,boot-stage` 仍为 internal/TF 的 `os`、diagnostic 的 `maintenance`。

`--initrd` 文件同时参与输入哈希、blob 字节精确比对、布局不重叠、UnpackTool 回读和重打包逐字节一致性检查；报告记录条目索引与 shim 索引。九条目外置 initrd 路径适用于 TB323FU 的内部根文件系统，配合统一 Linux 7.2.6 内核、五代专用的 `msm.dpu_use_virtual_planes=0` 固定平面配置，以及原生 DRM／Adreno 840 的 165 Hz 显示。TF 启动、触屏与游戏体验、休眠及长期显示稳定性仍待进一步验证，公共安装／OTA 流程尚未接通。打包在 Linux Docker 中执行。

## 默认启动条目（可选）

`build_offline_boot.py` 的 `--default-entry` 只决定 manifest 的默认条目，取值为 `android`、`internal`、`tf`、`diagnostic`，分别对应固定布局的条目 0..3（`Image-00`..`Image-03`）。不写该选项时默认仍为 `internal`（`Image-01`），命令行、八/九条目布局、8000 ms 超时及其余校验均不变。

`--default-entry diagnostic` 让 `Image-03` 成为 manifest 默认条目：它沿用已有的 `maintenance` 启动阶段并停留在 initramfs 内，不安装常规根文件系统模块，也不校验正常 OS 启动，仅用于采集日志。该选项只写 manifest 默认值，不保存用户偏好，也不改变菜单顺序、payload 或 DTB 内容。

## 字体

菜单使用 `src/ui/fonts/lxgw_wenkai_menu.ksf`，来源及 OFL 许可位于资产同目录。使用 `tools/gen_ui_font.py` 的菜单覆盖模式和 `menu-required-text.txt` 维护子集；新增菜单、翻译或 manifest 标签后，更新清单并验证字形及预览。菜单子集不是完整中文字库。

本目录提供离线构建，不负责部署和 OTA。系统中的 `armada-kshim-sync` 当前只匹配四代 Elden，不能用于五代。固定依赖、设备输入的自动准备以及五代完整安装／更新流程仍待完善，状态见[五代设备文档](../../README.md#已知限制)。
