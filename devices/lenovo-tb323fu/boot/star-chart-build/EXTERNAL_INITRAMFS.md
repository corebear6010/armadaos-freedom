# Y700 五代外置 initramfs 构建

`build_external_initramfs.py` 在 Linux Docker 中离线重建五代统一内核使用的外置 initramfs。输入是内置 initramfs（gzip 压缩的 newc cpio，此处作为外置模板）和统一内核的模块暂存目录；模板对 `/lib/modules` 以外的一切内容具有权威性。

本页适用于 TB323FU，构建与打包入口见[五代引导说明](README.md)。该工具保留模板内的启动逻辑和固件，并要求五代所用的 `nt36536_ts.ko`；不能因两代共用内核，就把同一模板直接用于四代。四代输入约定见[四代引导说明](../../../lenovo-tb322fc/boot/README.md)。

## 输入与命令

```text
python3 -B build_external_initramfs.py \
  --template-initrd inputs/initramfs-template.cpio.gz \
  --module-dir inputs/staging/lib/modules/7.2.6-... \
  --kernel-release 7.2.6-... \
  --out initramfs-candidate \
  [--early-only]
```

- `--template-initrd`：gzip 压缩的 newc cpio 模板，通常取自内置 initramfs 的现场备份。
- `--module-dir`：必须正好是 `<staging>/lib/modules/<KVER>`，末级目录名与 `--kernel-release` 完全一致。
- `--out`：必须为空；脚本不会清空已存在的目录，也不会修改模板或模块输入。失败时只留下 `failure.json`。
- `--early-only`：可选，默认关闭。开启后只把两个必需早期模块的 `modules.dep` 传递闭包放进 initramfs；关闭时暂存目录中的全部未压缩模块都会进入镜像。无论开关，所选内部或 TF 卡根文件系统都须另行安装与完整 KVER 匹配的模块。

## 运行环境

- 仅使用 Python 标准库，不联网、不访问设备、不修改安装器、OTA 或 bootc。
- 需要容器内真实的 `gzip`、`cpio`、`depmod` 和带 ZSTD 的 `mksquashfs`；缺失时明确失败，不用替身工具。
- 模板可能包含设备节点，因此 `cpio` 解包必须在 Linux Docker 内以 root 执行。

## 压缩格式

统一内核的实际配置为 `CONFIG_SQUASHFS_ZSTD=y`、XZ 关闭，因此模块镜像使用 `mksquashfs -comp zstd`，不引入 XZ 依赖。`artifact.json` 中记录 `modules.squashfs_compression = "zstd"`。

## 路径安全与模板保真

- 解包前先解析全部 cpio 成员：拒绝绝对路径、空组件、`.` 和 `..`，拒绝重复成员，并校验父目录类型。
- `/lib/modules` 以外的一切内容（`init`、`armada-root-resolve`、辅助程序、固件和字体）原样保留：打包前后核对 SHA-256、权限、符号链接目标和设备号；`init` 与根解析策略由模板决定，本脚本不修改。
- `/lib/modules` 整体替换为唯一的 `<KVER>.sqfs`，不保留模板中的旧模块树。

## 模块策略

- 旧 `init` 只解析未压缩 `.ko`：发现 `.ko.xz`、`.ko.zst`、`.ko.gz` 直接报错，不改变模块加载策略。
- `qcom_q6v5_pas.ko` 和 `nt36536_ts.ko` 必须存在。
- 模块暂存排除顶层 `vmlinuz`、`dtb`、`build`、`source`，保留其余 `.ko` 与模块索引，然后在正确的 `lib/modules/<KVER>` 布局中运行 `depmod`。
- 默认包含模块暂存目录中的全部未压缩模块；开启 `--early-only` 时只包含两个必需早期模块的依赖闭包（见下节）。与完整 KVER 匹配的全部模块仍须单独安装到所选内部或 TF 卡根文件系统，本脚本不安装、也不修改该根目录。

## 早期模块裁剪（可选，`--early-only`）

旧引导窗口对未压缩 initramfs 的大小敏感：完整模块暂存约 200 MB（去掉 `Image` 后仍约 120 MB），把全部模块打进 initramfs 会不必要地推高超出窗口的风险。根文件系统策略本来就要求另行安装同 KVER 的完整模块树，因此开启 `--early-only` 时 initramfs 只需要早期驱动。

- 开启后仍先复制完整模块树并在正确的 `lib/modules/<KVER>` 布局中运行 `depmod`，得到完整模块索引；随后读取 `modules.dep`，从 `qcom_q6v5_pas.ko` 和 `nt36536_ts.ko` 出发求传递闭包。
- 两个必需模块的文件名必须在暂存树中唯一出现；`modules.dep` 的每个依赖路径必须真实存在于暂存树中，重复条目直接报错。
- 只从临时暂存副本删除闭包之外的 `.ko`，保留原有模块索引文件；`modules.order` 过滤为仍然存在的选中路径，然后再次运行 `depmod` 并校验重建后的依赖仍然全部落在闭包内。
- `qcom_pil_info`、`qcom_common`、`qcom_sysmon`、`qcom_q6v5` 由 `qcom_q6v5_pas.ko` 的依赖闭包覆盖，`init` 中原有的 `modprobe` 序列不需要改动。
- 输入的原始模块目录不被修改；裁剪只发生在 `--out` 下的临时暂存副本。
- 产物在 `artifact.json` 中记录 `modules.early_only`、`modules.early_module_count` 和 `modules.early_modules`（只记模块文件名，不记路径或设备身份）；关闭时后两项为 `null`。

## 输出

- `initramfs.cpio.gz`：newc 格式、root 所有、成员按名称排序、时间戳归零、`gzip -n` 压缩。
- `artifact.json`：记录 KVER、模板与模块计数、镜像与 initramfs 的 SHA-256、`squashfs_compression: "zstd"`、`early_only`/`early_module_count`/`early_modules` 以及 `hardware_validated: false`。这是离线产物，不代表真机可启动。

## 测试

```sh
python3 -m unittest -v test_build_external_initramfs.py
```

用例覆盖成员路径校验、模块与输入约束、端到端构建以及 `--early-only` 的依赖闭包选择；容器缺少 `gzip`、`cpio`、`depmod` 或 `mksquashfs` 时相应用例跳过。
