# Maintainer: Your Name <you@example.com>
pkgname=vrc-mpris-osc
pkgver=0.1.0
pkgrel=1
pkgdesc="Lightweight daemon that bridges Linux MPRIS media players to the VRChat OSC Chatbox"
arch=('x86_64' 'aarch64')
url="https://github.com/yourname/vrc-mpris-osc"
license=('MIT')
depends=('gcc-libs' 'glibc')
makedepends=('cargo')
backup=("etc/vrc-mpris-osc/config.example.toml")
source=("$pkgname-$pkgver.tar.gz::$url/archive/refs/tags/v$pkgver.tar.gz")
sha256sums=('SKIP') # replace with the real checksum when cutting a release

prepare() {
    cd "$pkgname-$pkgver"
    export RUSTUP_TOOLCHAIN=stable
    cargo fetch --locked --target "$(rustc -vV | sed -n 's/host: //p')"
}

build() {
    cd "$pkgname-$pkgver"
    export RUSTUP_TOOLCHAIN=stable
    export CARGO_TARGET_DIR=target
    cargo build --frozen --release
}

check() {
    cd "$pkgname-$pkgver"
    export RUSTUP_TOOLCHAIN=stable
    cargo test --frozen --release
}

package() {
    cd "$pkgname-$pkgver"

    install -Dm755 "target/release/$pkgname" "$pkgdir/usr/bin/$pkgname"
    install -Dm644 "README.md" "$pkgdir/usr/share/doc/$pkgname/README.md"
    install -Dm644 "config.example.toml" "$pkgdir/etc/vrc-mpris-osc/config.example.toml"
    install -Dm644 "LICENSE" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"

    # User-level systemd unit (started with `systemctl --user enable --now vrc-mpris-osc`)
    install -Dm644 "vrc-mpris-osc.service" "$pkgdir/usr/lib/systemd/user/$pkgname.service"
    sed -i "s#%h/.cargo/bin/vrc-mpris-osc#/usr/bin/vrc-mpris-osc#" \
        "$pkgdir/usr/lib/systemd/user/$pkgname.service"
}
