SHELL = /bin/bash
.ONESHELL:
.SHELLFLAGS += -xe

P4LANG_TARGET = p4lang-pi_$(P4LANG_PI_VERSION_FULL)_$(CONFIGURED_ARCH).deb

# CI may select pinned official Git sources plus official Debian packaging.
# Equivalence with the OBS source archive has not been established.
P4LANG_PI_SOURCE_METHOD ?= obs
ifeq ($(P4LANG_PI_SOURCE_METHOD),github)
ifneq ($(P4LANG_PI_VERSION),0.1.3)
$(error GitHub PI sources are pinned to P4LANG_PI_VERSION=0.1.3)
endif
ifneq ($(P4LANG_PI_VERSION_FULL),0.1.3-2)
$(error GitHub PI packaging is pinned to P4LANG_PI_VERSION_FULL=0.1.3-2)
endif
ifeq ($(CROSS_BUILD_ENVIRON),y)
$(error P4LANG_PI_SOURCE_METHOD=github supports native builds only)
endif
else ifneq ($(P4LANG_PI_SOURCE_METHOD),obs)
$(error Unsupported P4LANG_PI_SOURCE_METHOD=$(P4LANG_PI_SOURCE_METHOD))
endif

$(addprefix $(DEST)/, $(P4LANG_TARGET)): $(DEST)/% :
ifeq ($(P4LANG_PI_SOURCE_METHOD),github)
	python3 pi_source.py --output p4lang-pi-$(P4LANG_PI_VERSION)
else
# 	# Remove any stale files
	rm -rf p4lang-pi-$(P4LANG_PI_VERSION)
	dget -u p4lang-pi_$(P4LANG_PI_VERSION_FULL).dsc http://download.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-pi_$(P4LANG_PI_VERSION_FULL).dsc
endif
	pushd p4lang-pi-$(P4LANG_PI_VERSION)
	patch -p1 -i ../p4lang-pi.patch/use-boost-1.83.patch
	patch -p1 -i ../p4lang-pi.patch/remove-protobuf-from-debian-dependency.patch
	patch -p1 -i ../p4lang-pi.patch/remove-init-file.patch
ifeq ($(CROSS_BUILD_ENVIRON), y)
	dpkg-buildpackage -us -uc -b -a$(CONFIGURED_ARCH) -Pcross,nocheck -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
else
	dpkg-buildpackage -us -uc -b -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
endif
	popd
	mv $* $(DEST)/

P4LANG_BMV2_TARGET = p4lang-bmv2_$(P4LANG_BMV2_VERSION_FULL)_$(CONFIGURED_ARCH).deb

$(addprefix $(DEST)/, $(P4LANG_BMV2_TARGET)): $(DEST)/% :
# 	# Remove any stale files
	rm -rf p4lang-bmv2-$(P4LANG_BMV2_VERSION)
	dget -u p4lang-bmv2_$(P4LANG_BMV2_VERSION_FULL).dsc http://download.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-bmv2_$(P4LANG_BMV2_VERSION_FULL).dsc
	pushd p4lang-bmv2-$(P4LANG_BMV2_VERSION)
	patch -p1 -i ../p4lang-bmv2.patch/use-boost-1.83.patch
	patch -p1 -i ../p4lang-bmv2.patch/remove-unnecessary-dependencies.patch
ifeq ($(CROSS_BUILD_ENVIRON), y)
	dpkg-buildpackage -us -uc -b -a$(CONFIGURED_ARCH) -Pcross,nocheck -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
else
	dpkg-buildpackage -us -uc -b -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
endif
	popd
	mv $* $(DEST)/

P4LANG_P4C_TARGET = p4lang-p4c_$(P4LANG_P4C_VERSION_FULL)_$(CONFIGURED_ARCH).deb
$(addprefix $(DEST)/, $(P4LANG_P4C_TARGET)): $(DEST)/% :
# 	# Remove any stale files
	rm -rf p4lang-p4c-$(P4LANG_P4C_VERSION)
	dget -u p4lang-p4c_$(P4LANG_P4C_VERSION_FULL).dsc http://download.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-p4c_$(P4LANG_P4C_VERSION_FULL).dsc
	pushd p4lang-p4c-$(P4LANG_P4C_VERSION)
	patch -p1 -i ../p4lang-p4c.patch/use-boost-1.83.patch
	patch -p1 -i ../p4lang-p4c.patch/fix-build-for-boost-1.83.patch
	patch -p1 -i ../p4lang-p4c.patch/update-thrift-dependency.patch
	patch -p1 -i ../p4lang-p4c.patch/remove-protobuf-from-debian-dependency.patch
	patch -p1 -i ../p4lang-p4c.patch/remove-scapy-from-debian-dependency.patch
ifeq ($(CROSS_BUILD_ENVIRON), y)
	dpkg-buildpackage -us -uc -b -a$(CONFIGURED_ARCH) -Pcross,nocheck -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
else
	dpkg-buildpackage -us -uc -b -j$(SONIC_CONFIG_MAKE_JOBS) --admindir $(SONIC_DPKG_ADMINDIR)
endif
	popd
	mv $* $(DEST)/

