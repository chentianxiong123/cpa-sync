# macOS 交叉编译方法（osxcross）—— Linux 编产物 + macOS 签名

> 2026-11-21 · cpa-sync（Rust + rusqlite bundled）
> 目的：**编译在免费 ubuntu，macOS 500 分钟免费额度只用于签名**

---

## 一、为什么这么做

之前的 CI 用 `macos-latest` runner **全程编译** macOS 产物：
- macOS runner 计费 **10x 系数**（500 分钟/月免费）
- 一次 push 的 macOS 编译 = 2~15 分钟 × 10 = 20~150 分钟计费
- 曾实测：7 次 push 烧掉 **274/500 分钟**（55%），大半是"排队未调度"空转

**优化思路（用户拍板）**：把**耗时**的编译放 `ubuntu-latest`（免费无限），macOS runner **只签名**（秒级）：

```
ubuntu（免费）       macos-latest（500 分钟，只签名）
├─ linux x86_64 原生     ┌──────────────┐
├─ linux aarch64 zigbuild│ 下载产物      │
├─ macOS x86_64 osxcross │ codesign -s - │ ← 几秒 ×10，用不完
└─ macOS aarch64 osxcross└──────────────┘
```

---

## 二、osxcross 搭建（Linux 交叉编 macOS 的核心）

### 依赖

```bash
sudo apt-get install -y clang cmake xz-utils wget build-essential
```

### SDK 从哪来（无需自备 Mac）

**`joseluisq/macosx-sdks`**（GitHub Release，社区托管 SDK tarball）：
```
https://github.com/joseluisq/macosx-sdks/releases/download/<版本>/MacOSX<版本>.sdk.tar.xz
```
示例 `12.3` → `MacOSX12.3.sdk.tar.xz`。放到 `osxcross/tarballs/`。

### 克隆 + 构建（⚠️ 关键：非交互）

```bash
git clone --depth 1 https://github.com/tpoechtrager/osxcross.git /tmp/osxcross
mkdir -p /tmp/osxcross/tarballs
wget -q .../MacOSX12.3.sdk.tar.xz -O /tmp/osxcross/tarballs/MacOSX12.3.sdk.tar.xz
cd /tmp/osxcross
BUILD_FLAVOR=stable UNATTENDED=1 ./build.sh
```

**坑**：`build.sh` 默认是**交互式**（选 cctools 版本 1/2/3），CI 无 TTY 下直接失败（exit 1）。
**解决**：设 `BUILD_FLAVOR=stable UNATTENDED=1` 走非交互（stable = cctools 986 + ld64 711）。
（`setup-osxcross` action 没设这俩变量，所以才卡死。）

产物在 `/tmp/osxcross/target/bin/`（`o64-clang` / `oa64-clang` / `x86_64-apple-darwin-*`）。

---

## 三、cargo 交叉编译（rust + rusqlite bundled）

```yaml
- uses: dtolnay/rust-toolchain@stable
  with:
    targets: x86_64-apple-darwin,aarch64-apple-darwin
```

### 关键环境变量（C 代码 + linker 指向 osxcross clang）

x86_64：
```yaml
env:
  CC_x86_64_apple_darwin: o64-clang
  CXX_x86_64_apple_darwin: o64-clang++
  CARGO_TARGET_X86_64_APPLE_DARWIN_LINKER: o64-clang
  LIBZ_SYS_STATIC: "1"      # 防宿主机 zlib 混入
```

aarch64：把 `o64-clang` 换成 `oa64-clang`（osxcross 的 ARM 变体）。

`rusqlite bundled`（编 sqlite C 源码）用 osxcross clang 编 → 没问题。

---

## 四、缓存设计（一切可复用、速度快）

| 缓存 | key | 内容 |
|---|---|---|
| osxcross | `osxcross-12.3-stable` | `/tmp/osxcross/target`（编译产物）+ `tarballs`（SDK）|
| cargo (linux) | `cargo-linux-${{target}}-${{hash(Cargo.lock)}}` | `~/.cargo/registry` + `~/.cargo/git` + `rs/target` |
| cargo (macos) | `cargo-macos-${{hash(Cargo.lock)}}` | 同上 |

- **osxcross 缓存命中** → 跳过 9 分钟首次编译（检测 `target/bin/o64-clang` 存在）
- **SDK 缓存命中** → 跳过几百 MB 下载
- **cargo 缓存** → `Cargo.lock` 不变不重装依赖

---

## 五、签名（macos-latest，500 分钟额度用一点点）

```yaml
- uses: actions/download-artifact@v4      # 下载 macOS 产物
  with:
    pattern: cpa-sync-*-apple-darwin
    merge-multiple: true
- run: |
    find dist -type f -name cpa-sync | while read f; do
      codesign --force --sign - "$f"       # ad-hoc 签名（免费，无需证书）
      codesign --verify "$f"
    done
```

产物**分开**（x86_64 / aarch64 两个 artifact），**不做 universal 合并**——Linux 没有 `lipo`，分开正好避开。

---

## 六、最终 CI 结构（cross-build.yml）

```
jobs:
  linux:       ubuntu → x86_64 原生 / aarch64 zigbuild（cargo 缓存）
  macos-build: ubuntu → osxcross（BUILD_FLAVOR=stable UNATTENDED=1）
                       + rustup apple target + CC/linker → 编 x86_64/aarch64
  macos-sign:  macos-latest → 下载 → codesign ad-hoc → 上传（只签名）
  summary:     linux + macos-sign 产物校验和
```

---

## 七、踩坑汇总

| 坑 | 现象 | 解决 |
|---|---|---|
| `setup-osxcross` 交互卡死 | build.sh 选 cctools 菜单，无 TTY exit 1 | 手动 `BUILD_FLAVOR=stable UNATTENDED=1` |
| lipo Linux 无 | 无法合并 universal | 产物分开，不合并 |
| codesign Linux 无 | 无法在 Linux 签名 | 签名挪到 macos-latest job |
| SDK 获取难 | Apple 许可限制 | joseluisq/macosx-sdks 社区托管 |
| rustc 版本 | 依赖 `rust-version=1.86`，本地 1.85 编不了 | CI 用 `dtolnay/rust-toolchain@stable` |
| C 依赖 | rusqlite bundled 需 C 编译 | CC/CXX/linker 指 osxcross clang + `LIBZ_SYS_STATIC=1` |
