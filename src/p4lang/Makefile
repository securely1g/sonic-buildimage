SHELL = /bin/bash
.ONESHELL:
.SHELLFLAGS += -xe

# Optional verified local source set. Leave empty to use the upstream URL.
P4LANG_SOURCE_DIR ?=

# Args: package, full version, upstream version, DSC SHA256, orig SHA256,
# Debian archive SHA256. The original descriptor is verified before adaptation.
define prepare_local_p4lang_source
	case "$(1):$(2):$(3)" in \
	    p4lang-pi:0.1.3-2:0.1.3|p4lang-bmv2:1.15.0-9:1.15.0|p4lang-p4c:1.2.4.2-3:1.2.4.2) ;; \
	    *) echo "Unsupported pinned local P4 source version: $(1) $(2) ($(3))" >&2; exit 1 ;; \
	esac
	cp "$(P4LANG_SOURCE_DIR)/$(1)_$(2).dsc" \
	   "$(P4LANG_SOURCE_DIR)/$(1)_$(3).orig.tar.gz" \
	   "$(P4LANG_SOURCE_DIR)/$(1)_$(2).debian.tar.xz" .
	printf '%s  %s\n' \
	    "$(4)" "$(1)_$(2).dsc" \
	    "$(5)" "$(1)_$(3).orig.tar.gz" \
	    "$(6)" "$(1)_$(2).debian.tar.xz" | sha256sum -c -
	$(if $(filter p4lang-p4c,$(1)),patch --batch --forward --reject-file=- -p0 -i p4lang-p4c-regional-source.dsc.patch,:)
	dpkg-source --require-strong-checksums -x "$(1)_$(2).dsc"
endef

define prepare_pi_source
	rm -rf p4lang-pi-$(P4LANG_PI_VERSION)
$(if $(strip $(P4LANG_SOURCE_DIR)),\
$(call prepare_local_p4lang_source,p4lang-pi,$(P4LANG_PI_VERSION_FULL),$(P4LANG_PI_VERSION),9d5b60b6e403755896c8585eef20659a62fe2faccf612968beee2537fd72857e,9bb2b97f2caa7548b9cc798c3643b97315f3d8f8372d3689dea5e2a22e18a097,ba9af65a28fd1bbad2b928b681d8b5ea10076611c0e2e0dee1ba855f1216723c),\
dget -u http://download.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-pi_$(P4LANG_PI_VERSION_FULL).dsc)
endef

define prepare_bmv2_source
	rm -rf p4lang-bmv2-$(P4LANG_BMV2_VERSION)
$(if $(strip $(P4LANG_SOURCE_DIR)),\
$(call prepare_local_p4lang_source,p4lang-bmv2,$(P4LANG_BMV2_VERSION_FULL),$(P4LANG_BMV2_VERSION),2e9a8e6042f0c23148c4a1a11099db2cc87901f685a763d52cfadfad26a96fc1,083bb907e45597af8e29ef3b0a92d33d282ba23840ddd5cfb8372d2f030ab415,30941da6dde4cbbefaa9a12dfffa260115b3b63c6d56b3112b3baee953636179),\
dget -u http://download.opensuse.org/repositories/home:/p4lang/Debian_11/p4lang-bmv2_$(P4LANG_BMV2_VERSION_FULL).dsc)
endef

define prepare_p4c_source
	rm -rf p4lang-p4c-$(P4LANG_P4C_VERSION)
$(if $(strip $(P4LANG_SOURCE_DIR)),\
$(call prepare_local_p4lang_source,p4lang-p4c,$(P4LANG_P4C_VERSION_FULL),$(P4LANG_P4C_VERSION),2ee97ffd3f82d67e04ee149f12ef8c58377f9db37e76d4aaa54518178c7f0fb7,a41d616add63924a098edaab446809f543ffadfc2e40356f9b73986f324c668a,2a39bf04c8d084724014b71cdeb3b91dbe7a32bf1bd6cd499e5e132aa3e4cb40),\
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

# Verify and extract sources without invoking the package build.
.PHONY: prepare-pi-source prepare-bmv2-source prepare-p4c-source
prepare-pi-source:
	$(prepare_pi_source)
prepare-bmv2-source:
	$(prepare_bmv2_source)
prepare-p4c-source:
	$(prepare_p4c_source)
