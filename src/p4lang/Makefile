SHELL = /bin/bash
.ONESHELL:
.SHELLFLAGS += -xe

# Optional retained source inputs; an empty setting uses the native downloader.
P4LANG_SOURCE_DIR ?=

# Verify before copying and again after copying to bind extraction to the bytes
# actually used. Only the working descriptor is repaired; cached inputs stay intact.
define prepare_local_p4lang_source
	python3 source_cache.py verify --source-dir "$(P4LANG_SOURCE_DIR)" --package $(1) --version $(2) --upstream-version $(3)
	cp "$(P4LANG_SOURCE_DIR)/$(1)_$(2).dsc" \
	   "$(P4LANG_SOURCE_DIR)/$(1)_$(3).orig.tar.gz" \
	   "$(P4LANG_SOURCE_DIR)/$(1)_$(2).debian.tar.xz" .
	python3 source_cache.py verify --source-dir . --package $(1) --version $(2) --upstream-version $(3)
	$(if $(filter p4lang-p4c,$(1)),patch --fuzz=0 --batch --forward --reject-file=- -p0 -i p4lang-p4c-source.dsc.patch,:)
	dpkg-source --require-strong-checksums -x "$(1)_$(2).dsc"
endef

define prepare_pi_source
	rm -rf p4lang-pi-$(P4LANG_PI_VERSION)
$(if $(strip $(P4LANG_SOURCE_DIR)),\
$(call prepare_local_p4lang_source,p4lang-pi,$(P4LANG_PI_VERSION_FULL),$(P4LANG_PI_VERSION)),\
dget -u http://download.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-pi_$(P4LANG_PI_VERSION_FULL).dsc)
endef

define prepare_bmv2_source
	rm -rf p4lang-bmv2-$(P4LANG_BMV2_VERSION)
$(if $(strip $(P4LANG_SOURCE_DIR)),\
$(call prepare_local_p4lang_source,p4lang-bmv2,$(P4LANG_BMV2_VERSION_FULL),$(P4LANG_BMV2_VERSION)),\
dget -u http://download.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-bmv2_$(P4LANG_BMV2_VERSION_FULL).dsc)
endef

define prepare_p4c_source
	rm -rf p4lang-p4c-$(P4LANG_P4C_VERSION)
$(if $(strip $(P4LANG_SOURCE_DIR)),\
$(call prepare_local_p4lang_source,p4lang-p4c,$(P4LANG_P4C_VERSION_FULL),$(P4LANG_P4C_VERSION)),\
dget -u http://download.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-p4c_$(P4LANG_P4C_VERSION_FULL).dsc)
endef

P4LANG_TARGET = p4lang-pi_$(P4LANG_PI_VERSION_FULL)_$(CONFIGURED_ARCH).deb

$(addprefix $(DEST)/, $(P4LANG_TARGET)): $(DEST)/% :
	$(prepare_pi_source)
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
	$(prepare_bmv2_source)
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
	$(prepare_p4c_source)
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

# Exercise the same source preparation as the package targets without compiling.
.PHONY: prepare-pi-source prepare-bmv2-source prepare-p4c-source
prepare-pi-source:
	$(prepare_pi_source)
prepare-bmv2-source:
	$(prepare_bmv2_source)
prepare-p4c-source:
	$(prepare_p4c_source)

