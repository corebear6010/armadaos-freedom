#!/usr/bin/bash
# Runs inside the builder container. See ../build-local.sh for the contract.
#
# Compiles the third-party btop-cn fork for the build host's own architecture
# and packages only /usr/libexec/armada/btop-cn. No cross build here.
set -euxo pipefail

source ./BASE.env

NAME=btop-cn

rm -rf out
mkdir -p out

export HOME=/tmp
dnf -y install rpm-build rpmdevtools gcc-c++ make curl
rpmdev-setuptree
cat >/etc/rpm/macros.armada <<EOF
%_buildhost armada-builder
%packager Armada
%vendor Armada
EOF
cp "${NAME}.spec" ~/rpmbuild/SPECS/
sed -i "s/^Version:.*/Version:        ${VERSION}/" ~/rpmbuild/SPECS/${NAME}.spec
curl -fL --retry 3 \
    -o ~/rpmbuild/SOURCES/${COMMIT}.tar.gz \
    "https://codeload.github.com/dijiaozhibei-top/btop-cn/tar.gz/${COMMIT}"
(cd ~/rpmbuild/SOURCES && echo "${SOURCE_SHA256}  ${COMMIT}.tar.gz" | sha256sum -c -)
rpmbuild -bb --define "commit ${COMMIT}" ~/rpmbuild/SPECS/${NAME}.spec
cp ~/rpmbuild/RPMS/*/${NAME}-[0-9]*.rpm /work/out/
