# Armada btop-cn RPM, built from the third-party Chinese btop fork
# dijiaozhibei-top/btop-cn at a pinned commit.
%global forgeurl https://github.com/dijiaozhibei-top/btop-cn
# %changelog is intentionally empty; don't derive SOURCE_DATE_EPOCH from it.
%global source_date_epoch_from_changelog 0
# The binary is a private copy under /usr/libexec/armada and nothing links
# against it, so the debug subpackages would only duplicate it.
%global debug_package %{nil}

Name:           btop-cn
# overwritten from BASE.env by build.sh
Version:        0
Release:        1%{?dist}.armada
Summary:        Chinese-localised btop, installed alongside Fedora's btop

License:        Apache-2.0
URL:            %{forgeurl}
Source0:        %{forgeurl}/archive/%{commit}.tar.gz

BuildRequires:  gcc-c++
BuildRequires:  make

%description
btop-cn is a third-party fork of btop that carries the Chinese translation in
the program itself. Armada compiles it from a pinned commit and installs only
/usr/libexec/armada/btop-cn, which the zbtop launcher runs.

This package ships no /usr/bin/btop and no desktop file: Fedora's btop remains
the English command, and both packages can be installed side by side.

%prep
%autosetup -n btop-cn-%{commit}

%build
%make_build STATIC=false GPU_SUPPORT=false

%install
install -Dpm0755 bin/btop %{buildroot}%{_libexecdir}/armada/btop-cn

%files
%license LICENSE
%doc README.md
%{_libexecdir}/armada/btop-cn

%changelog
