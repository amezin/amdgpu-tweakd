pkgname=radeon-fan-control
pkgver=0.0.0
pkgrel=1
pkgdesc="Radeon fan control"
arch=('any')
depends=('python-gobject')
makedepends=('python-setuptools')
source=('radeon_fan_control.py' 'setup.py' 'radeon-fan-control.service')
md5sums=('SKIP' 'SKIP' 'SKIP')

build() {
    python setup.py build
}

package() {
    python setup.py install --prefix=/usr --root="${pkgdir}/" --optimize=1 --skip-build
}
