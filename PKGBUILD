# Maintainer: nemeshisu <christianpertot07@gmail.com>
pkgname=vrc-mpris-osc
pkgver=0.1.0
pkgrel=1
pkgdesc="Lightweight native MPRIS to VRChat OSC Chatbox bridge written in Rust"
arch=('x86_64')
url="https://github.com/nemeshisu0/vrc-mpris-osc"
license=('MIT')
depends=('gcc-libs')
makedepends=('cargo')
options=('!debug')
source=("$pkgname-$pkgver.tar.gz::https://github.com/nemeshisu0/$pkgname/archive/refs/tags/v$pkgver.tar.gz")
sha256sums=('8e9a3572e7a9336471fbc31c4dcba9abd6ce4978607521bfa3f895a928c67fb1')

prepare() {
  cd "$pkgname-$pkgver"
  export RUSTUP_TOOLCHAIN=stable
  cargo fetch --locked --target "$(rustc -vV | sed -n 's/host: //p')"
}

build() {
  cd "$pkgname-$pkgver"
  export RUSTUP_TOOLCHAIN=stable
  export CARGO_TARGET_DIR=target
  cargo build --frozen --release --all-targets
}

check() {
  cd "$pkgname-$pkgver"
  export RUSTUP_TOOLCHAIN=stable
  cargo test --frozen --all-targets
}

package() {
  cd "$pkgname-$pkgver"
  install -Dm755 "target/release/$pkgname" "$pkgdir/usr/bin/$pkgname"
  install -Dm644 "$pkgname.service" "$pkgdir/usr/lib/systemd/user/$pkgname.service"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}
