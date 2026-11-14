# btop-cn

`btop` keeps Fedora's English interface. `zbtop` opens the Chinese interface with an independent configuration and desktop entry.

The Chinese program comes from [dijiaozhibei-top/btop-cn](https://github.com/dijiaozhibei-top/btop-cn), pinned to `e292f440397e4978869bd25c0f245f2c33d1107d` (`v1.4.7-cn`). It retains the upstream Apache-2.0 licence. `BASE.env` records the source version and archive checksum.

The normal package workflow builds a dynamically linked `btop-cn` RPM from source, with GPU monitoring disabled. It installs `/usr/libexec/armada/btop-cn` alongside the original `btop` package.

On first launch, `zbtop` copies its Chinese default configuration to `${XDG_CONFIG_HOME:-$HOME/.config}/zbtop/btop.conf`. Existing settings are preserved. Command-line options are passed through to btop-cn.