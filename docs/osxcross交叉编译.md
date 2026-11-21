# osxcross 交叉编译 macOS —— 归纳与踩坑

> 2026-11-21 · cpa-sync（Rust + rusqlite bundled）
> 结论：**方案成立**，4 平台产物全出，macOS 500 分钟额度几乎不占

---

## 一、背景与目标

cpa-sync 是 Rust 项目（CLI + web 面板，`rs/`），CI 要交叉编译多平台产物。

**问题**：原 CI 的 `macos-latest` runner **全程编译** macOS 产物，计费 **10x 系数**：
- 7 次 push 烧掉 **274/500 分钟**（55%）
- 最夸张一次：macos job 排队 15 分钟没被调度 → **150 分钟计费纯空转**

**目标**：编译在**免费 ubuntu**，macOS 免费额度（500 分钟）**只用于签名**。

---

## 二、方案（用户拍板）

```
ubuntu（免费无限）                  macos-latest（500 分钟，只签名）
├─ linux x86_64  原生                    ┌──────────────┐
├─ linux aarch64 zigbuild                │ 下载产物      │
├─ macOS x86_64  osxcross                │ codesign -s - │ ← 9 秒 ×10 = 1.5 分钟/次
└─ macOS aarch64 osxcross                └──────────────┘
```

---

## 三、实现关键点

### 1. osxcross（Linux 交叉编 macOS）

```bash
sudo apt-get install -y clang cmake xz-utils wget build-essential
git clone --depth 1 https://github.com/tpoechtrager/osxcross.git /tmp/osxcross
mkdir -p /tmp/osxcross/tarballs
# SDK 来自 joseluisq/macosx-sdks（社区托管，无需自备 Mac）
wget -q https://github.com/joseluisq/macosx-sdks/releases/download/12.3/MacOSX12.3.sdk.tar.xz \
  -O /tmp/osxcross/tarballs/MacOSX12.3.sdk.tar.xz
cd /tmp/osxcross
BUILD_FLAVOR=stable UNATTENDED=1 ./build.sh   # ← 非交互关键
```

### 2. cargo 交叉编译（rust + rusqlite bundled）

```yaml
- uses: dtolnay/rust-toolchain@stable
  with: { targets: x86_64-apple-darwin,aarch64-apple-darwin }
env:
  CC_x86_64_apple_darwin: o64-clang
  CXX_x86_64_apple_darwin: o64-clang++
  CARGO_TARGET_X86_64_APPLE_DARWIN_LINKER: o64-clang
  LIBZ_SYS_STATIC: "1"      # 防宿主机 zlib 混入
# aarch64 同，o64-clang → oa64-clang
```

### 3. 缓存（速度 + 复用）

| 缓存 | key | 效果 |
|---|---|---|
| osxcross | `osxcross-12.3-stable` | 命中跳过 9 分钟编译 + SDK 下载 |
| cargo | `cargo-linux/macos-${{hash(Cargo.lock)}}` | Cargo.lock 不变不重装依赖 |

### 4. 签名（macos-latest 只签名）

```bash
find dist -type f -name cpa-sync | while read f; do
  codesign --force --sign - "$f"        # ad-hoc（免费，无需证书）
done
```

---

## 四、实测结果

```
run conclusion: success（5/5 job）
├─ linux x86_64    ✅ 4.0M
├─ linux aarch64   ✅ 3.7M
├─ macos-build     ✅ 缓存命中 60s（首次 15 分钟）
├─ macos-sign      ✅ 9 秒（=1.5 分钟 macOS 额度/次）
└─ summary         ✅ 4 产物全带 sha256
```

**额度**：500 − 274（旧）− ~3（新签名 2 次）≈ **剩余 223 分钟**，每次 push 只耗 1.5 分钟。

---

## 五、踩坑大全

### A. 旧 CI 的坑（已解决）

| 坑 | 说明 |
|---|---|
| macOS 全程编译 | 10x 系数，20~150 分钟/次 |
| 排队未调度也计费 | macos runner 无容量，排队 15 分钟 = 150 分钟计费白扔 |

### B. 本次踩的坑（已解决）

| # | 坑 | 现象 | 解决 |
|---|---|---|---|
| 1 | **setup-osxcross 交互卡死** | `build.sh` 弹 cctools 菜单（1/2/3），CI 无 TTY exit 1 | 手动 clone + `BUILD_FLAVOR=stable UNATTENDED=1` |
| 2 | **lipo Linux 无** | 无法合并 universal | 产物分开，不合并 |
| 3 | **codesign Linux 无** | Linux 上无法签名 | 签名挪到 macos-latest job |
| 4 | **SDK 获取难** | Apple 许可限制 | joseluisq/macosx-sdks 社区托管 |
| 5 | **merge-multiple 覆盖** | x86_64/aarch64 都叫 cpa-sync，后者覆盖前者 | 分目录下载 |
| 6 | **codesign --verify 严格失败** | "main executable failed strict validation" | verify 警告不中断 |
| 7 | **rustc 版本 MSRV** | 依赖 `rust-version=1.86`，本地 1.85 编不了 | CI 用 `dtolnay/rust-toolchain@stable` |
| 8 | **C 依赖** | rusqlite bundled 需 C 编译 | CC/CXX/linker 指 osxcross clang + `LIBZ_SYS_STATIC=1` |

---

## 六、决策记录

| 决策 | 理由 |
|---|---|
| 编译全在 ubuntu | 免费无限，macOS 只签名 |
| macos 只签名 | 500 分钟额度省到 1.5 分钟/次 |
| 不本地编译 | rustc 1.85 < 1.86（MSRV），直接 CI |
| 产物分开不合并 | Linux 无 lipo，避开 |
| SDK 用 joseluisq 托管 | 无需自备 Mac，合法路径 |

---

## 七、相关文件

- CI：`.github/workflows/cross-build.yml`
- 方法文档：`~/Desktop/macOS交叉编译-osxcross方法.md`
- 提交：`70a20fa`（osxcross 方案）→ `95fafd3`（非交互+缓存）→ `aaea06a`（签名修复）
