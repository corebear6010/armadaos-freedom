# TB323FU 硬件旋转与游戏直出

SM8850 的显示处理单元可以旋转游戏和 HUD 图层，避免为了横屏显示而先用 GPU 合成整屏画面。该路径需要配套的内核旋转支持、面板方向和 Gamescope 图层选择。

## 内核与面板

统一内核的 `0224-drm-msm-dpu-sm8850-inline-rotation.patch` 为 Kaanapali VIG 接入 inline rotation，保留 REC0/REC1 寄存器布局：

- DPU 13 使用 OP_MODE bit 15 表示 ROT90；不再误用 FORMAT 中属于像素字节数的 bit 11。
- 根据目录中的 1088 像素旋转源高度限制拆分图层，为原生横屏分配独立管线。
- 在分配资源前保留旋转信息，90°/270° 旋转不共享 multirect，方向变化重新分配资源。
- 按旋转后的尺寸计算时钟，并检查 SM8850 无预缩小时的 11/5 缩小边界。

设备树补丁 `0201-baldur-panel-orientation.patch` 在面板节点设置 `rotation = <270>`。补丁在板级 DTS 复制后应用。其他机型保持各自的目录和面板配置。

已验证的运行组合使用 `msm.dpu_use_virtual_planes=1`、Gamescope `--force-orientation left`，并去掉 `--force-composition-rotation`。这组启动配置仅适用于配套更新后的 TB323FU。切换前应更新同一次构建的 Image、DT、initramfs 与完整模块树，并保留共享 boot 的 Android 签名元数据和可启动回退。

## Gamescope 图层选择

游戏、MangoApp HUD 和完全透明的 Steam 占位图原先会构成三个旋转图层，超出该场景的可用旋转资源，导致整屏 GPU 合成回退。

`0042-steamcompmgr-skip-redundant-steam-placeholder.patch` 在已有可见外部 HUD 图层时省去这个透明占位图。真实 Steam 菜单照常绘制；没有 HUD、HUD 隐藏或外部图层绘制关闭时，保留原占位逻辑。该判断依据图层状态，不使用机型硬编码。

菜单收起时，已验证的游戏与 HUD 分别使用两条 VIG 管线完成 90° 旋转与扫描输出。菜单、通知或其他图层出现时，Gamescope 仍可正常回退到 GPU 合成。

## 已验证范围

在 165 Hz 的动态 PV 场景、55 帧原始帧和三倍补帧配置下，游戏与 HUD 两层直出已确认；采样未出现 Gamescope 的 GPU 合成提交，实际新游戏帧显示约 163–164 帧/秒。实体屏幕的方向、颜色、PV 更新及左右菜单正常，HUD 隐藏和恢复后也能回到直出路径。

仍有少量漏刷新。以上不代表所有游戏、全部刷新档位、外接显示、方向热切换或休眠唤醒均已验证，也不解决已记录的 90 Hz 偶发物理花屏。此源码更新不附带可直接刷写的通用 boot 镜像。
